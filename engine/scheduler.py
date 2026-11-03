"""调度决策：join 等待集 + 死路重算 + 就绪队列。

这是整个引擎最容易写错的地方，也是 V01 要验证的核心。

算法（每完成一个节点执行一次）：

    complete(n):
        done += {n}; running -= {n}
        for 每个后继 s（按 exit 过滤）:
            if s 已 done 或 running: 跳过
            need = join_preds(s) - done
            if not need: ready += s
            else: waiting[s] = need
        死路重算:
            active = running ∪ ready ∪ {n}
            for s, need in waiting:
                need -= {p | p 不可从 active 到达}
                if not need: ready += s

关键点：
  · active 必须含**刚完成的 n**，否则 n 触发的新分支会被判成死路
    （roots 不含已执行节点的话，循环中途会误判成死路）
  · waiting 用 set，同一前驱被通知两次天然幂等
  · "不可达"的判定 blocked 传 done - active，不能让已完成节点继续展开
"""

from __future__ import annotations

from collections import deque

from .graph import Graph


class Scheduler:
    def __init__(self, graph: Graph) -> None:
        self.g = graph
        self.done: set[str] = set()  # 已终结（成功/失败/跳过都算）
        self.running: set[str] = set()  # 正在执行
        self.ready: list[str] = []  # 可以开始的
        self.waiting: dict[str, set[str]] = {}  # 等前驱的节点
        self.order: list[str] = []  # 实际开始顺序（测试用）
        self.count: dict[str, int] = {}  # 每个节点被开始的次数（测重复执行）

    # ------------------------------------------------------------ 启动

    def bootstrap(self) -> list[str]:
        for s in self.g.start:
            self._mark_ready(s)
        return list(self.ready)

    def _mark_ready(self, node_id: str) -> None:
        if node_id in self.done or node_id in self.running:
            return
        if node_id in self.ready:
            return
        if node_id in self.waiting:
            return
        self.ready.append(node_id)

    # ------------------------------------------------------------ 取任务

    def take(self) -> str | None:
        """取一个就绪节点并标记为 running。"""
        while self.ready:
            nid = self.ready.pop(0)
            if nid in self.done or nid in self.running:
                continue
            self.running.add(nid)
            self.order.append(nid)
            self.count[nid] = self.count.get(nid, 0) + 1
            return nid
        return None

    # ------------------------------------------------------------ 完成

    def complete(self, node_id: str, exit_name: str | None = None) -> list[str]:
        """节点 n 执行完（成功/失败/跳过），返回新就绪的节点。"""
        self.running.discard(node_id)
        self.done.add(node_id)
        self.waiting.pop(node_id, None)

        newly: list[str] = []

        # 1) 按出口找后继
        nexts = self.g.successors(node_id, exit_name)
        for nxt in nexts:
            if nxt in self.done or nxt in self.running:
                continue
            need = {p for p in self.g.join_preds(nxt) if p not in self.done}
            if not need:
                if nxt not in self.ready and nxt not in self.waiting:
                    self.ready.append(nxt)
                    newly.append(nxt)
            else:
                cur = self.waiting.get(nxt)
                if cur is None:
                    self.waiting[nxt] = need
                else:
                    cur.intersection_update(need | {node_id})
                    cur.discard(node_id)
                    if not cur:
                        self.waiting.pop(nxt, None)
                        if nxt not in self.ready:
                            self.ready.append(nxt)
                            newly.append(nxt)

        # 2) 死路重算
        newly.extend(self._prune_dead(just_done=node_id))
        return newly

    # ------------------------------------------------------------ 死路

    def _prune_dead(self, just_done: str) -> list[str]:
        """剔除等待集里"永远不会到达"的前驱，必要时放行节点。"""
        if not self.waiting:
            return []

        active: set[str] = set(self.running) | set(self.ready) | {just_done}
        blocked = {n for n in self.done if n not in active}

        # 一次 BFS 求出所有可达目标，避免对每个前驱各跑一次
        targets = {p for need in self.waiting.values() for p in need}
        if not targets:
            return []
        hit = self.g.any_reachable(active, targets, blocked=blocked)

        newly: list[str] = []
        for nid in list(self.waiting):
            need = self.waiting[nid]
            # 已完成的前驱（正常路径已删，这里兜底）
            dead = {p for p in need if p in self.done or p not in hit}
            if dead:
                need -= dead
            if not need:
                del self.waiting[nid]
                if nid not in self.done and nid not in self.running and nid not in self.ready:
                    self.ready.append(nid)
                    newly.append(nid)
        return newly

    # ------------------------------------------------------------ 状态

    def finished(self) -> bool:
        return not self.ready and not self.running

    def status(self) -> dict[str, object]:
        return {
            "done": sorted(self.done),
            "running": sorted(self.running),
            "ready": list(self.ready),
            "waiting": {k: sorted(v) for k, v in self.waiting.items()},
        }

    def report(self) -> dict[str, object]:
        """给测试/日志用的执行报告。"""
        return {
            "order": list(self.order),
            "count": dict(self.count),
            "done": sorted(self.done),
            "waiting_left": {k: sorted(v) for k, v in self.waiting.items()},
            "ready_left": list(self.ready),
        }


def run_all(graph: Graph, exits: dict[str, str | None] | None = None) -> dict[str, object]:
    """模拟执行：按就绪队列跑完所有节点。

    exits 用来指定分支节点走哪个出口，例如 {"n_if": "true"}。
    没指定的分支节点走普通边（exit=None）。
    这是给测试用的最小执行器，不含真实节点动作。
    """
    exits = exits or {}
    sch = Scheduler(graph)
    sch.bootstrap()
    guard = 0
    while True:
        nid = sch.take()
        if nid is None:
            break
        guard += 1
        if guard > 10_000:
            raise RuntimeError("调度死循环：guard 超限")
        sch.complete(nid, exits.get(nid))
    return sch.report()
