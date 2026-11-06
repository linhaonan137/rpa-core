"""主循环：按波次执行 + 循环容器 + 子流程 + on_fail + 断点。

设计取舍：
  · **波次执行**（不是事件驱动）：每轮取出全部就绪节点并发跑，全部结束后统一
    通知 Scheduler。牺牲一点并行度，换来"调度状态只有一个写者"，不会出现
    多任务并发改 executed 集合导致的状态错乱。
  · **循环容器自己跑 body**：loop 节点内部用独立子 Scheduler 跑循环体，跑完
    再通知主 Scheduler 走 done 出口。这样主图里永远看不到回边。
  · **失败不抛异常**：节点返回 Result(ok=False)，由 on_fail 决定重试/回退/跳过。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import ExecutionContext
from .graph import LOOP_TYPES, Graph
from .registry import registry
from .scheduler import Scheduler
from .schema import Flow, Node


@dataclass
class RunOptions:
    breakpoints: set[str] = field(default_factory=set)
    step: bool = False
    start_at: str | None = None  # 从某节点开始（上游跳过）
    max_steps: int = 10_000
    max_rounds_per_loop: int = 10_000
    on_pause: Callable[[str, str], Any] | None = None  # (node_id, when) -> awaitable


@dataclass
class NodeLog:
    node_id: str
    type: str
    ok: bool
    ms: float
    error: str | None = None
    skipped: bool = False
    attempts: int = 1
    tolerated: bool = False  # 失败了但被 on_fail=skip 容忍，整体仍算 ok


class Runner:
    def __init__(
        self,
        flow: Flow,
        ctx: ExecutionContext,
        opts: RunOptions | None = None,
        *,
        graph: Graph | None = None,
    ) -> None:
        self.flow = flow
        self.ctx = ctx
        self.opts = opts or RunOptions()
        self.g = graph or Graph(flow)
        self.sch = Scheduler(self.g)
        self.logs: list[NodeLog] = []
        self.steps = 0
        self.stopped = False
        self._wave_len = 1  # 当前波次的节点数（on_fail=back 只在单分支时可用）
        self._requeue: list[str] = []  # retry-from 要重新开始跑的节点

    # ------------------------------------------------------------ 入口

    def _preflight(self) -> None:
        """装载期校验：节点类型名写错 / 把 MCP 工具名当节点名，在这里就报。

        不校验的代价：流程跑到第 N 步才 `KeyError: 未注册的节点类型`，
        而前面 N-1 步的点击/提交已经产生副作用（实测 `web_switch` 跑了 29 步才炸）。
        """
        import difflib

        from engine.registry import registry as _reg

        nodes = getattr(self.g, "nodes", {})
        pairs = nodes.items() if hasattr(nodes, "items") else [(n.id, n) for n in nodes]
        known = _reg.types()
        bad: list[str] = []
        for nid, node in pairs:
            if _reg.has(node.type):
                continue
            hint = difflib.get_close_matches(node.type, known, n=2, cutoff=0.6)
            tip = f"，是不是 {' / '.join(hint)}？" if hint else ""
            bad.append(f"{nid}({node.type}){tip}")
        if bad:
            # 抛 KeyError 而不是 RuntimeError：MCP/HTTP 已有「流程配置错误」分支会把它
            # 渲染成可读信息，而不是笼统的「执行器异常」
            raise KeyError(
                "流程里有未注册的节点类型（装载期校验）："
                + "；".join(bad[:5])
                + "。注意：MCP 工具名 ≠ 节点名（web_switch→web_tab、web_fill→web_type）。"
            )

    async def execute(self) -> dict[str, Any]:
        t0 = time.perf_counter()

        # 流程自带的 vars 作为默认值注入（调用方已注入的同名变量优先）
        for var in self.flow.vars:
            if not self.ctx.has_var(var.name):
                self.ctx.set_var(var.name, var.value, scope="global")

        # 流程内元素 manifest：交付时的快照，**只补全局库里没有的**。
        # 优先级：ctx.elements（同进程刚拾的）> elements.json（本机最新）> flow.elements（交付兜底）。
        # 这样换台机器（全局库为空）跑得起来，本机跑又不会被旧快照盖掉自愈修好的定义。
        if self.flow.elements and isinstance(self.ctx.elements, dict):
            try:
                from elements.store import ElementStore
                from engine.paths import ELEMENTS_PATH

                known = set(ElementStore(str(ELEMENTS_PATH)).keys())
            except Exception:  # noqa: BLE001
                known = set()
            for k, v in self.flow.elements.items():
                if k not in known:
                    self.ctx.elements[k] = v

        self._preflight()

        if self.opts.start_at:
            self.sch.bootstrap()
            self._skip_to(self.opts.start_at)
        else:
            self.sch.bootstrap()

        while (
            not self.sch.finished()
            and not self.stopped
            and not getattr(self.ctx, "should_continue", False)
        ):
            wave = self._drain()
            if not wave:
                break
            self._wave_len = len(wave)

            results = await asyncio.gather(
                *(self._exec_node(nid) for nid in wave), return_exceptions=True
            )

            for nid, res in zip(wave, results):
                if isinstance(res, BaseException):
                    # 节点内部抛错（if 的条件表达式、loop 的 resolve 等）必须落日志并停。
                    # 不记就等于"这一波没发生过"——ok 只由 logs 推导，流程会报绿，
                    # 而实际两条分支都没执行（实测 noop→if(变量拼错)→true:noop 返回 ok=True）。
                    res = _wrap_exc(res)
                    node_type = "?"
                    try:
                        node_type = self.g.nodes[nid].type
                    except Exception:  # noqa: BLE001
                        pass
                    self.logs.append(
                        NodeLog(
                            nid,
                            node_type,
                            False,
                            0.0,
                            str(getattr(res, "error", "") or res),
                            False,
                            1,
                            False,
                        )
                    )
                    self.ctx.should_stop = True
                if self.ctx.should_stop:
                    self.stopped = True
                if self._requeue:
                    # retry-from 已重置区间并安排了新起点；
                    # 这条失败链的后继不能再被通知（否则回退前又往前跑了）
                    self._requeue.clear()
                    break
                self.sch.complete(nid, getattr(res, "next", None))

        ok = all((lg.ok or lg.tolerated) for lg in self.logs) and not self.stopped
        return {
            "ok": ok,
            "stopped": self.stopped,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "steps": self.steps,
            "nodes": [lg.__dict__ for lg in self.logs],
            "failed": [lg.node_id for lg in self.logs if not lg.ok],
            "scheduler": self.sch.report(),
        }

    # ------------------------------------------------------------ 取一批

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            nid = self.sch.take()
            if nid is None:
                break
            out.append(nid)
        return out

    def _skip_to(self, target: str) -> None:
        """从某节点起跑：把能到达 target 的上游全部标记为已跳过。"""
        if target not in self.g.nodes:
            raise KeyError(f"start_at 指向不存在的节点: {target}")
        # 反向 BFS 求上游
        up: set[str] = set()
        stack = [target]
        while stack:
            cur = stack.pop()
            for p in self.g.pred.get(cur, []):
                if p not in up:
                    up.add(p)
                    stack.append(p)
        for nid in up:
            self.sch.done.add(nid)
        self.sch.ready = [target]

    # ------------------------------------------------------------ 执行单节点

    async def _exec_node(self, node_id: str) -> Any:
        self.steps += 1
        if self.steps > self.opts.max_steps:
            self.stopped = True
            return _stop_result("max_steps 超限")

        node = self.g.nodes[node_id]

        if node.off:
            self.logs.append(NodeLog(node_id, node.type, True, 0.0, skipped=True))
            return _skip_result("节点已禁用")

        await self._pause(node_id, "before")

        if node.type in LOOP_TYPES:
            res = await self._run_loop(node)
        elif node.type == "if":
            res = await self._run_if(node)
        else:
            res = await self._run_once(node)

        await self._pause(node_id, "after")
        return res

    async def _run_once(self, node: Node) -> Any:
        """执行单个节点，带 on_fail 重试。"""
        from .registry import Result

        attempts = 1
        limit = 1
        if node.on_fail and node.on_fail.mode == "retry":
            limit = max(1, node.on_fail.attempts)

        last: Result | None = None
        while attempts <= limit:
            r = await self._invoke(node)
            last = r
            if r.ok:
                break
            if attempts < limit:
                await asyncio.sleep((node.on_fail.backoff_ms if node.on_fail else 0) / 1000)
            attempts += 1

        assert last is not None

        # 先决定失败策略，再落日志（skip 要标 tolerated）
        tolerated = False
        if not last.ok:
            mode = node.on_fail.mode if node.on_fail else "stop"
            if mode == "skip":
                tolerated = True
                self.ctx.log("warn", f"{node.display} 失败，按 skip 继续", node=node.id)
            elif mode == "back":
                handled = self._retry_from(node)
                if handled:
                    # 区间日志已被 _retry_from 剔除；这里不记失败日志，
                    # 等回退重跑后由新一轮记录（否则整体 ok 会被这条旧失败拖成 False）
                    return Result.fail(last.error or "")
                self.ctx.log(
                    "warn",
                    f"{node.display} 的 retry-from 不可用（多分支或回退点不在路径上），改停",
                    node=node.id,
                )
                # then=skip：回退重试用尽后跳过这个节点继续走。
                # 原来这里无条件 stop —— 配了 then=skip 也是整单中断（批量任务全回滚）。
                if node.on_fail is not None and node.on_fail.then == "skip":
                    tolerated = True
                    self.ctx.log(
                        "warn", f"{node.display} 回退重试用尽，按 then=skip 继续", node=node.id
                    )
                else:
                    self.ctx.should_stop = True
            else:
                self.ctx.should_stop = True

        self.logs.append(
            NodeLog(
                node.id, node.type, last.ok, last.ms, last.error,
                last.skipped, min(attempts, limit), tolerated,
            )
        )

        if tolerated:
            return Result.skip(last.error or "")

        # 事后断言
        if last.ok and node.expect is not None:
            ok, why = await self._check_expect(node)
            if not ok:
                self.logs.append(NodeLog(node.id, "expect", False, 0.0, why))
                self.ctx.should_stop = True
                return Result.fail(f"postAssert 失败: {why}")

        return last

    async def _invoke(self, node: Node) -> Any:
        from .registry import Result, raw_fields, timer

        t = timer()
        try:
            fn = registry.get(node.type)  # 未注册类型也要转成失败，不能穿透
            # 代码/模板/正则类字段不做变量替换（否则 {a:1} 会被当占位符）
            raw = raw_fields(node.type)
            if raw:
                args = {
                    k: (v if k in raw else self.ctx.resolve(v))
                    for k, v in node.args.items()
                }
            else:
                args = self.ctx.resolve(node.args)
            with t:
                r = await fn(args, self.ctx)
        except Exception as exc:  # noqa: BLE001 - 节点异常统一转失败
            r = Result.fail(f"{type(exc).__name__}: {exc}")
        if r.ms == 0.0:
            r.ms = round(t.ms, 1)
        return r

    # ------------------------------------------------------------ 分支

    async def _run_if(self, node: Node) -> Any:
        from .registry import Result

        fn = registry.get("if")
        r = await fn(self.ctx.resolve(node.args), self.ctx)
        self.logs.append(NodeLog(node.id, "if", r.ok, r.ms, r.error))
        if not r.ok:
            self.ctx.should_stop = True
            return r
        return Result(ok=True, next="true" if r.data else "false", data=r.data)

    # ------------------------------------------------------------ 循环

    async def _run_loop(self, node: Node) -> Any:
        from .registry import Result

        fn = registry.get(node.type)
        # 循环节点只负责算"跑几次 + 每轮的变量"，body 由 Runner 跑
        plan = await fn(self.ctx.resolve(node.args), self.ctx)
        if not plan.ok:
            self.logs.append(NodeLog(node.id, node.type, False, plan.ms, plan.error))
            self.ctx.should_stop = True
            return plan

        body = self.g.loop_body(node.id)
        rounds = int(plan.data or 0)
        total_ms = plan.ms

        for i in range(min(rounds, self.opts.max_rounds_per_loop)):
            # 步数预算：每轮都传完整的 max_steps 会让 limit_steps 完全失效
            # （实测 limit_steps=1 跑 5 轮循环照样跑满 —— 因为每轮都是独立预算）
            if self.steps >= self.opts.max_steps:
                self.stopped = True
                self.ctx.should_stop = True
                self.logs.append(NodeLog(node.id, node.type, False, 0.0, "max_steps 超限（循环中）"))
                break
            remaining = max(1, self.opts.max_steps - self.steps)
            self.ctx.push_scope()
            self.ctx.push_loop({"kind": node.type, "index": i, "rounds": rounds})
            self.ctx._set_reserved("_index", i)
            try:
                sub = Runner(
                    self.flow,
                    self.ctx,
                    RunOptions(max_steps=remaining),  # 用剩余预算，不是完整预算
                    graph=_SubGraph(self.g, body),
                )
                sub_result = await sub.execute()
                total_ms += sub_result["ms"]
                self.steps += sub_result["steps"]  # 子流程的步数要并回主计数
                self.logs.extend(sub.logs)
                if not sub_result["ok"]:
                    self.ctx.should_stop = True
                    break
            finally:
                self.ctx.pop_loop()
                self.ctx.pop_scope()

            if self.ctx.should_continue:
                # continue：结束本轮，进下一轮。原来只置标志没人读，continue 之后的
                # 步骤照跑 —— “今天已经处理过就跳过”会变成再处理一遍（重复提交）。
                self.ctx.should_continue = False
                continue
            if self.ctx.should_break:
                self.ctx.should_break = False
                break

        # 循环结束走 done 出口（body 的回边不会被主图看到）
        return Result(ok=True, next="done", data=rounds, ms=round(total_ms, 1))

    # ------------------------------------------------------------ retry-from

    def _retry_from(self, node: Node) -> bool:
        """on_fail.mode=back：回退到 rollback_to 重跑区间。

        返回 True 表示已安排重跑；False 表示条件不满足（调用方改停）。

        两个限制（安全取舍，写进验收）：
          1. 只在**单分支波次**时可用——并行分支里重置区间会踩到别的分支正在跑的节点
          2. 回退点必须在失败节点的上游路径上（between 非空）
        重试次数由 on_fail.attempts 控制，超过后按 on_fail.then 处理（默认 stop）。
        """
        assert node.on_fail is not None
        target = node.on_fail.rollback_to
        if not target or target not in self.g.nodes:
            return False
        if self._wave_len > 1:
            return False

        region = self.g.between(target, node.id)
        if node.id not in region:  # 回退点不在失败节点的上游
            return False

        # 次数控制：直接数这个节点被开始了多少次（含本次），attempts = 总尝试次数
        limit = max(1, node.on_fail.attempts)
        tried = self.sch.count.get(node.id, 0)
        if tried >= limit:
            then = node.on_fail.then or "stop"
            if then == "skip":
                return False  # 让调用方走 tolerated
            self.ctx.should_stop = True
            return False

        # 重置区间状态：这些节点当作没跑过
        for nid in region:
            self.sch.done.discard(nid)
            self.sch.running.discard(nid)
            self.sch.waiting.pop(nid, None)
        self.logs = [lg for lg in self.logs if lg.node_id not in region]

        # 从回退点重新开始：入就绪队列；_requeue 是"跳过本轮 complete"的标志
        self.sch.ready.append(target)
        self._requeue.append(target)
        self.ctx.log("warn", f"{node.display} 失败，回退到 {target} 重跑（第 {tried}/{limit} 次尝试）",
                     node=node.id)
        return True

    # ------------------------------------------------------------ 断言 / 断点

    async def _check_expect(self, node: Node) -> tuple[bool, str]:
        exp = node.expect
        assert exp is not None
        page = self.ctx.page
        if page is None:
            # 不能"没页面就算通过"：桌面流程 / attach 失败 / 页面被关时，
            # 「提交成功」这类断言会静默放行 —— 恰恰是它该拦住的那类错误结果。
            return False, "expect 需要页面，但当前没有可用页面（断言未执行，按失败计）"

        deadline = time.perf_counter() + exp.limit_ms / 1000
        last = ""
        while True:
            try:
                if exp.url and exp.url not in (page.url or ""):
                    last = f"URL 不含 {exp.url}"
                elif exp.text and exp.text not in (await page.content()):
                    last = f"页面不含文本 {exp.text}"
                elif exp.target:
                    # 支持 @el: 引用（原来直接返回"需要元素库支持"，写了 @el: 的断言必失败）
                    from nodes.web import _locate

                    loc = await _locate(page, exp.target, None, self.ctx, 5000)
                    n = await loc.count()
                    state = exp.state or "visible"
                    if state == "hidden":
                        # hidden：不存在或不可见都算通过（schema 早就支持，以前没实现）
                        last = "" if (n == 0 or not await loc.first.is_visible()) else f"元素仍然可见: {exp.target}"
                    elif n == 0:
                        last = f"元素不存在: {exp.target}"
                    elif state == "visible" and not await loc.first.is_visible():
                        last = f"元素不可见: {exp.target}"
                else:
                    return True, ""
                if not last:
                    return True, ""
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            if time.perf_counter() >= deadline:
                return False, last
            await asyncio.sleep(0.2)

    async def _pause(self, node_id: str, when: str) -> None:
        if not self.opts.on_pause:
            return
        if self.opts.step or node_id in self.opts.breakpoints:
            r = self.opts.on_pause(node_id, when)
            if asyncio.iscoroutine(r):
                await r


# ---------------------------------------------------------------- 子图


class _SubGraph:
    """把主图裁剪成只含 body 的子图，供循环/子流程复用 Runner 跑。"""

    def __init__(self, base: Graph, keep: set[str]) -> None:
        self.flow = base.flow
        self.nodes = {k: v for k, v in base.nodes.items() if k in keep}
        self.edges = [e for e in base.edges if e.from_ in keep and e.to in keep]
        self.succ = {k: [(t, x) for t, x in base.succ.get(k, []) if t in keep] for k in keep}
        self.pred = {k: [p for p in base.pred.get(k, []) if p in keep] for k in keep}
        self.isolated = []
        self.start = [k for k in keep if not self.pred.get(k)]
        if not self.start:
            self.start = list(keep)[:1]

    def successors(self, node_id: str, exit_name: str | None = "__any__") -> list[str]:
        return Graph.successors(self, node_id, exit_name)  # type: ignore[arg-type]

    def join_preds(self, node_id: str) -> list[str]:
        return Graph.join_preds(self, node_id)  # type: ignore[arg-type]

    def loop_body(self, loop_id: str) -> set[str]:
        return Graph.loop_body(self, loop_id)  # type: ignore[arg-type]

    def any_reachable(self, sources: Any, targets: Any, blocked: Any = ()) -> set[str]:
        return Graph.any_reachable(self, sources, targets, blocked)  # type: ignore[arg-type]

    def reachable(self, sources: Any, target: str, blocked: Any = ()) -> bool:
        return Graph.reachable(self, sources, target, blocked)  # type: ignore[arg-type]

    def stats(self) -> dict[str, Any]:
        return {"nodes": len(self.nodes), "edges": len(self.edges)}


# ---------------------------------------------------------------- 工具


def _wrap_exc(exc: BaseException) -> Any:
    from .registry import Result

    r = Result.fail(f"{type(exc).__name__}: {exc}")
    return r


def _stop_result(msg: str) -> Any:
    from .registry import Result

    return Result(ok=False, error=msg)


def _skip_result(msg: str) -> Any:
    from .registry import Result

    return Result.skip(msg)


async def run_flow(flow: Flow, ctx: ExecutionContext | None = None, opts: RunOptions | None = None) -> dict[str, Any]:
    """便捷入口：跑一个流程，返回报告。"""
    ctx = ctx or ExecutionContext()
    return await Runner(flow, ctx, opts).execute()
