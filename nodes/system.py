"""系统与控制类节点：变量、条件、循环容器、占位。

这一批是引擎自举必需的 —— 没有它们就不能测主循环。
业务节点（网页/数据/桌面…）由各 Agent 在 nodes/ 下另建文件。
"""

from __future__ import annotations

from typing import Any

from engine.context import ExecutionContext
from engine.registry import Result, register


@register("noop")
async def noop(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """什么都不做。测试与占位用。"""
    return Result.done("noop")


@register("fail")
async def fail(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """故意失败。测试 on_fail 用。"""
    return Result.fail(args.get("error", "故意失败"))


@register("set_var")
async def set_var(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {name, value, scope?}"""
    name = args.get("name")
    if not name:
        return Result.fail("set_var 缺 name")
    ctx.set_var(name, ctx.resolve(args.get("value")), scope=args.get("scope", "local"))
    return Result.done(f"{name} 已设置")


@register("if")
async def if_node(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {left, op, right} → data=True/False，由 Runner 转成 exit。

    op: == != > >= < <= in not_in contains is_true
    """
    left = ctx.resolve(args.get("left"))
    op = args.get("op", "is_true")
    right = ctx.resolve(args.get("right"))

    try:
        if op == "is_true":
            val = bool(left)
        elif op == "==":
            val = left == right
        elif op == "!=":
            val = left != right
        elif op == ">":
            val = float(left) > float(right)  # type: ignore[arg-type]
        elif op == ">=":
            val = float(left) >= float(right)  # type: ignore[arg-type]
        elif op == "<":
            val = float(left) < float(right)  # type: ignore[arg-type]
        elif op == "<=":
            val = float(left) <= float(right)  # type: ignore[arg-type]
        elif op == "in":
            val = left in right  # type: ignore[operator]
        elif op == "not_in":
            val = left not in right  # type: ignore[operator]
        elif op == "contains":
            val = right in left  # type: ignore[operator]
        else:
            return Result.fail(f"不支持的 op: {op}")
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"if 求值失败: {exc}")

    return Result.done(f"if → {val}", data=val)


@register("loop")
async def loop(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind: count|range|times, count?, from?, to?, step?} → data=轮数"""
    kind = args.get("kind", "count")

    try:
        if kind == "range":
            start = int(args.get("from", 0) or 0)
            end = int(args.get("to", 0) or 0)
            step = int(args.get("step", 1) or 1)
            if step == 0:
                return Result.fail("loop.step 不能为 0")
            n = len(range(start, end, step))
        else:  # count / times
            n = int(args.get("count", args.get("times", 0)) or 0)
    except (TypeError, ValueError) as exc:
        return Result.fail(f"loop 参数错误: {exc}")

    if n < 0:
        return Result.fail(f"loop 轮数为负: {n}")
    return Result.done(f"loop {n} 轮", data=n)


@register("foreach")
async def foreach(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {items, item_as?, index_as?} → data=轮数"""
    items = ctx.resolve(args.get("items"))
    if items is None:
        return Result.fail("foreach 缺 items")
    if isinstance(items, (str, bytes)):
        return Result.fail("foreach.items 必须是列表/字典，不是字符串")
    try:
        n = len(items)
    except TypeError:
        return Result.fail(f"foreach.items 不可遍历: {type(items).__name__}")
    return Result.done(f"foreach {n} 项", data=n)


@register("map_each")
async def map_each(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {items: dict} → data=轮数"""
    items = ctx.resolve(args.get("items"))
    if not isinstance(items, dict):
        return Result.fail("map_each.items 必须是字典")
    return Result.done(f"map_each {len(items)} 项", data=len(items))


@register("while")
async def while_loop(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {max} → data=上限轮数（真实条件由 body 内 if + break 控制）"""
    try:
        n = int(args.get("max", 100) or 100)
    except (TypeError, ValueError):
        return Result.fail("while.max 必须是整数")
    return Result.done(f"while 上限 {n}", data=n)


@register("break")
async def break_loop(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    if not ctx.in_loop:
        return Result.fail("break 不在循环内")
    ctx.should_break = True
    return Result.done("break")


@register("continue")
async def continue_loop(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    if not ctx.in_loop:
        return Result.fail("continue 不在循环内")
    ctx.should_continue = True
    return Result.done("continue")


@register("stop")
async def stop_flow(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """强制停止整个流程。"""
    ctx.should_stop = True
    return Result.done(args.get("reason", "手动停止"))


@register("sys_cmd")
async def sys_cmd(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """跑系统命令（逃生舱）：args: {cmd, cwd?, timeout_ms=60000, save_as?}

    返回 {code, stdout, stderr}；非零退出码按失败上报（但数据仍然给，便于诊断）。
    """
    import subprocess

    cmd = str(args.get("cmd") or "").strip()
    if not cmd:
        return Result.fail("sys_cmd 缺 cmd")
    timeout_ms = int(args.get("timeout_ms", 60000) or 60000)

    from engine.paths import PROJECT_ROOT, resolve_path

    # cwd 默认项目根：原来 None = 进程启动目录，从别的目录起 MCP 时相对路径产物会乱落
    cwd = str(resolve_path(args["cwd"])) if args.get("cwd") else str(PROJECT_ROOT)

    try:
        r = subprocess.run(  # noqa: S602 - 逃生舱，设计如此
            cmd,
            shell=True,
            capture_output=True,   # 不指定 text：Windows 命令输出常是 GBK，utf-8 强解会乱码
            timeout=timeout_ms / 1000,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return Result.fail(f"命令超时（{timeout_ms}ms）：{cmd[:80]}")
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"命令执行失败：{type(exc).__name__}: {exc}")

    def _dec(b: bytes) -> str:
        if not b:
            return ""
        for enc in ("utf-8", "gbk"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")

    data = {
        "code": r.returncode,
        "stdout": _dec(r.stdout)[-4000:],
        "stderr": _dec(r.stderr)[-2000:],
    }
    if args.get("save_as"):
        ctx.set_var(str(args["save_as"]), data, scope=args.get("scope", "local"))

    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-200:]
        return Result.fail(f"命令退出码 {r.returncode}：{tail}", data=data)
    return Result.done(f"命令完成（exit 0，输出 {len(data['stdout'])} 字符）", data=data)


@register("sys_py")
async def sys_py(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """跑 Python 代码（逃生舱）：args: {code, save_as?}

    脚本里给 `result` 赋值即作为输出；直接 print 的内容也会被捕获。
    可用的方式：print() 输出即结果；环境变量 RPA_LLM_KEY 等都在。
    """
    import subprocess
    import sys as _sys

    code = str(args.get("code") or "")
    if not code.strip():
        return Result.fail("sys_py 缺 code")
    timeout_ms = int(args.get("timeout_ms", 30000) or 30000)

    from engine.paths import PROJECT_ROOT

    # 为什么用子进程而不是 exec：
    #   ① exec 是同步的，直接跑会卡死事件循环（MCP 服务一起卡）
    #   ② 丢线程池 + redirect_stdout 更糟——redirect_stdout 是进程级的，
    #     超时后 __exit__ 不执行，MCP 的 stdout 就泄漏成 StringIO，全部响应发不出去
    #   ③ 子进程超时可以真杀掉（timeout= 会 kill）
    try:
        r = subprocess.run(
            [_sys.executable, "-c", code],
            capture_output=True,
            timeout=timeout_ms / 1000,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return Result.fail(f"Python 执行超时（{timeout_ms}ms）")
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"Python 执行失败：{type(exc).__name__}: {exc}")

    def _dec(b: bytes) -> str:
        if not b:
            return ""
        for enc in ("utf-8", "gbk"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")

    out = _dec(r.stdout)
    err = _dec(r.stderr)
    if args.get("save_as"):
        ctx.set_var(str(args["save_as"]), out, scope=args.get("scope", "local"))

    if r.returncode != 0:
        return Result.fail(f"Python 退出码 {r.returncode}：{err[-300:]}", data={"stdout": out, "stderr": err})
    return Result.done(f"Python 已执行（输出 {len(out)} 字符）", data=out)


@register("sleep")
async def sleep_node(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {ms}"""
    import asyncio

    ms = int(args.get("ms", 0) or 0)
    if ms > 0:
        await asyncio.sleep(ms / 1000)
    return Result.done(f"sleep {ms}ms")


@register("subflow")
async def subflow_node(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """跑另一个流程文件：args: {flow, input?, save_as?}

    变量隔离（S23）：子流程有独立的变量栈，父流程的变量**不会**自动可见，
    要传就用 input 显式传；子流程的结果通过返回的 data（全部变量）拿。

    失败必须上浮 —— 子流程的 success=False 不能被吞成 True。
    """
    import json as _json
    import os as _os
    from pathlib import Path

    from engine.context import ExecutionContext as _EC
    from engine.paths import FLOWS_DIR
    from engine.runner import Runner as _Runner
    from engine.runner import RunOptions as _RO
    from engine.schema import Flow as _Flow

    name = args.get("flow")
    if not name:
        return Result.fail("subflow 缺 flow")

    flow_dir = Path(_os.getenv("RPA_FLOWS") or FLOWS_DIR)
    path = flow_dir / f"{name}.json"
    if not path.exists():
        return Result.fail(f"子流程文件不存在: {path}")
    try:
        sub_flow = _Flow(**_json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"子流程 {name} 校验失败: {exc}")

    inputs = ctx.resolve(args.get("input") or {}) or {}
    if not isinstance(inputs, dict):
        return Result.fail("subflow.input 必须是对象")

    # 独立变量栈；page / elements / logger 共享
    sub_ctx = _EC(
        page=ctx.page,
        run_id=f"{ctx.run_id}:{name}",
        elements=ctx.elements,
        logger=getattr(ctx, "_logger", None),
    )
    for k, v in inputs.items():
        sub_ctx.set_var(str(k), v, scope="global")
    for var in sub_flow.vars:
        if not sub_ctx.has_var(var.name):
            sub_ctx.set_var(var.name, var.value, scope="global")

    r = await _Runner(sub_flow, sub_ctx, _RO()).execute()

    if not r["ok"]:
        failed = ", ".join(r["failed"]) or "未知节点"
        last_err = (r["nodes"][-1].get("error") or "") if r["nodes"] else ""
        return Result.fail(
            f"子流程 {name} 失败于 [{failed}]" + (f": {last_err}" if last_err else ""),
        )

    # 子流程的 stop 信号不外传（父流程继续走）；结果变量存全部子变量
    save_as = args.get("save_as")
    all_vars = sub_ctx.all_vars()
    if save_as:
        ctx.set_var(str(save_as), all_vars, scope=args.get("save_scope", "local"))
    return Result.done(f"子流程 {name} 完成", data=all_vars)
