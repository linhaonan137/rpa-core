"""执行图：前驱后继、起始节点、join 等待集、死路可达性、循环体边界。

三个设计决定（都在堵常见的坑）：

1. **前驱后继都含全部边**（普通/分支/循环/错误），不再像朴素实现那样
   `adjacency` 不含分支边但 `reverse_adjacency` 含，导致三处合并逻辑不同步。

2. **join 等待集用 set 不用计数**：同一前驱被通知两次时 set 天然幂等
   （用 int 计数的话循环里会多等或少等）。

3. **循环体收集有 done 边界**：从 loop 出口 BFS，遇到 done 分支可达的节点即停，
   不会跨出循环圈把下游算进 body。
"""

from __future__ import annotations

from collections import deque
from typing import Iterable

from .schema import Edge, Flow, Node

#: 带分支出口的节点类型
BRANCH_TYPES = {"if", "loop", "foreach", "map_each", "while", "switch"}
#: 循环类节点（有 loop / done 两个出口）
LOOP_TYPES = {"loop", "foreach", "map_each", "while"}
#: 纯视觉节点，不参与执行
VISUAL_TYPES = {"note", "group"}


class GraphError(Exception):
    pass


class Graph:
    """把 Flow 解析成可查询的执行图。"""

    def __init__(self, flow: Flow) -> None:
        self.flow = flow

        # 过滤视觉节点
        self.nodes: dict[str, Node] = {
            n.id: n for n in flow.nodes if n.type not in VISUAL_TYPES
        }
        self.edges: list[Edge] = [
            e for e in flow.edges if e.from_ in self.nodes and e.to in self.nodes
        ]

        # node_id -> [(目标, exit)]
        self.succ: dict[str, list[tuple[str, str | None]]] = {i: [] for i in self.nodes}
        # node_id -> [前驱]（含所有边，去重保序）
        self.pred: dict[str, list[str]] = {i: [] for i in self.nodes}

        for e in self.edges:
            self.succ[e.from_].append((e.to, e.exit))
            if e.from_ not in self.pred[e.to]:
                self.pred[e.to].append(e.from_)

        self.start: list[str] = []
        self.isolated: list[str] = []
        self._resolve_start()

    # ------------------------------------------------------------ 解析

    def _resolve_start(self) -> None:
        """起始 = 无入边的节点；全图无入边时按"无普通入边"再判一次。"""
        no_in = [i for i in self.nodes if not self.pred[i]]
        out_any = [i for i in self.nodes if self.succ[i]]

        if no_in:
            self.start = no_in
        elif out_any:
            # 兜底：整个图有环（如 while 自环），取 topological 上的最小者
            self.start = [out_any[0]]
        else:
            self.start = list(self.nodes)

        # 孤立 = 无入边且无出边（且不是唯一节点）
        if len(self.nodes) > 1:
            self.isolated = [i for i in self.nodes if not self.pred[i] and not self.succ[i]]
            self.start = [i for i in self.start if i not in self.isolated]
            if not self.start:
                raise GraphError(f"没有起始节点，孤立节点: {self.isolated}")

    # ------------------------------------------------------------ 查询

    def successors(self, node_id: str, exit_name: str | None = "__any__") -> list[str]:
        """exit_name='__any__' 取全部；None 取普通边；其他取指定出口。"""
        pairs = self.succ.get(node_id, [])
        if exit_name == "__any__":
            return [t for t, _ in pairs]
        return [t for t, ex in pairs if ex == exit_name]

    def join_preds(self, node_id: str) -> list[str]:
        """汇合点需要等的前驱。

        排除"循环回边"：前驱在 node 的循环体内、node 本身是循环节点时，
        那条边是下一轮的入口，不该纳入本轮的等待集。
        """
        preds = self.pred.get(node_id, [])
        if len(preds) <= 1:
            return list(preds)
        if self.nodes[node_id].type not in LOOP_TYPES:
            return list(preds)

        body = self.loop_body(node_id)
        return [p for p in preds if p not in body]

    # ------------------------------------------------------------ 可达性

    def reachable(self, sources: Iterable[str], target: str, blocked: Iterable[str] = ()) -> bool:
        """从 sources 出发能否到达 target。blocked 里的节点不展开（已执行完的）。

        sources 本身即使被 blocked，也作为起点参与（它就是"现在正在跑的"）。
        """
        blocked_set = set(blocked)
        seen: set[str] = set()
        q: deque[str] = deque(sources)

        while q:
            cur = q.popleft()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            # 目标节点前驱已完成的边仍要展开，否则会漏
            if cur in blocked_set and cur != target:
                continue
            for nxt, _ in self.succ.get(cur, []):
                if nxt not in seen:
                    q.append(nxt)
        return False

    def forward_from(self, node_id: str, blocked: Iterable[str] = ()) -> set[str]:
        """从 node_id 正向可达的所有节点（含自身）。"""
        out: set[str] = {node_id}
        q: deque[str] = deque([node_id])
        blocked_set = set(blocked)
        while q:
            cur = q.popleft()
            if cur in blocked_set:
                continue
            for nxt, _ in self.succ.get(cur, []):
                if nxt not in out:
                    out.add(nxt)
                    q.append(nxt)
        return out

    def backward_from(self, node_id: str) -> set[str]:
        """能到达 node_id 的所有节点（含自身，沿前驱反向走）。"""
        out: set[str] = {node_id}
        q: deque[str] = deque([node_id])
        while q:
            cur = q.popleft()
            for p in self.pred.get(cur, []):
                if p not in out:
                    out.add(p)
                    q.append(p)
        return out

    def between(self, a: str, b: str) -> set[str]:
        """a 正向可达 ∩ 能到 b —— retry-from 需要重置的区间。

        只在 a 能到 b 时才有意义；调方要自己保证（否则返回空集）。
        """
        if a not in self.nodes or b not in self.nodes:
            return set()
        return self.forward_from(a) & self.backward_from(b)

    def any_reachable(self, sources: Iterable[str], targets: Iterable[str], blocked: Iterable[str] = ()) -> set[str]:
        """一次 BFS 求 sources 能到达的所有 targets。"""
        blocked_set = set(blocked)
        want = set(targets)
        hit: set[str] = set()
        seen: set[str] = set()
        q: deque[str] = deque(sources)

        while q:
            cur = q.popleft()
            if cur in want:
                hit.add(cur)
            if cur in seen:
                continue
            seen.add(cur)
            if cur in blocked_set:
                continue
            for nxt, _ in self.succ.get(cur, []):
                if nxt not in seen:
                    q.append(nxt)
        return hit

    # ------------------------------------------------------------ 循环体

    def loop_body(self, loop_id: str) -> set[str]:
        """循环体 = 从 loop 出口可达、且不经过 done 出口的节点集合。"""
        starts = self.successors(loop_id, "loop")
        if not starts:
            return set()

        done_roots = set(self.successors(loop_id, "done"))
        excluded: set[str] = set(done_roots)
        q: deque[str] = deque(done_roots)
        while q:
            cur = q.popleft()
            for nxt, _ in self.succ.get(cur, []):
                if nxt not in excluded:
                    excluded.add(nxt)
                    q.append(nxt)

        body: set[str] = set()
        q = deque(starts)
        while q:
            cur = q.popleft()
            if cur in body or cur == loop_id:
                continue
            body.add(cur)
            for nxt, ex in self.succ.get(cur, []):
                if nxt == loop_id:  # 回到循环节点，不再展开
                    continue
                if nxt in excluded:  # 跨出 done 边界，停
                    continue
                if nxt not in body:
                    q.append(nxt)
        return body

    # ------------------------------------------------------------ 诊断

    def stats(self) -> dict[str, object]:
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "start": self.start,
            "isolated": self.isolated,
            # 条件分支（不含循环）与循环分开统计，互不重叠
            "branch_nodes": [
                i for i, n in self.nodes.items()
                if n.type in BRANCH_TYPES and n.type not in LOOP_TYPES
            ],
            "loop_nodes": [i for i, n in self.nodes.items() if n.type in LOOP_TYPES],
        }
