"""V01：调度算法验证 —— join 集合 + 死路重算。

三种图形：
  ① 菱形汇合：A → B/C → D（D 只执行 1 次）
  ② 分支不回汇：if → true:B → D；false:C → E（走 true 时 C/E 不执行）
  ③ 分支后汇合：if → true:B1,B2 → D；false:C → D
     （走 true 时必须剔除 C，否则 D 永久阻塞 —— 这就是死路重算的用途）
  ④ 循环体边界：body 不能跨出 done 分支
"""

from __future__ import annotations

import pytest

from engine.graph import Graph
from engine.scheduler import Scheduler, run_all
from engine.schema import Edge, Flow, edge, node


def build(nodes: list, edges: list) -> Graph:
    return Graph(Flow(name="t", nodes=nodes, edges=edges))


def e(from_: str, to: str, exit: str | None = None) -> Edge:
    return edge(from_, to, exit)


# ------------------------------------------------------------ ① 菱形汇合


def test_diamond_joins_once():
    g = build(
        [node("A", "open"), node("B", "click"), node("C", "click"), node("D", "close")],
        [e("A", "B"), e("A", "C"), e("B", "D"), e("C", "D")],
    )
    r = run_all(g)
    assert r["count"]["A"] == 1
    assert r["count"]["B"] == 1
    assert r["count"]["C"] == 1
    assert r["count"]["D"] == 1, f"D 应只执行 1 次，实际 {r['count'].get('D')}"
    assert r["waiting_left"] == {}
    assert r["ready_left"] == []


def test_diamond_d_waits_for_both():
    """D 必须在 B、C 都完成之后才 ready。"""
    g = build(
        [node("A", "open"), node("B", "click"), node("C", "click"), node("D", "close")],
        [e("A", "B"), e("A", "C"), e("B", "D"), e("C", "D")],
    )
    sch = Scheduler(g)
    sch.bootstrap()
    assert sch.take() == "A"
    sch.complete("A")
    # B、C 都 ready，D 还没
    assert set(sch.ready) == {"B", "C"}
    assert "D" not in sch.ready

    sch.take()  # B（或 C）
    sch.complete(sch.order[-1])
    # 只剩一个前驱，D 仍不该 ready
    assert "D" not in sch.ready

    sch.take()
    sch.complete(sch.order[-1])
    assert "D" in sch.ready


# ------------------------------------------------------------ ② 分支不回汇


def test_branch_no_merge():
    """if 走 true，false 分支的 C/E 完全不执行。"""
    g = build(
        [
            node("A", "open"),
            node("if1", "if"),
            node("B", "click"),
            node("C", "click"),
            node("D", "close"),
            node("E", "close"),
        ],
        [e("A", "if1"), e("if1", "B", "true"), e("if1", "C", "false"), e("B", "D"), e("C", "E")],
    )
    r = run_all(g, exits={"if1": "true"})
    assert r["count"]["B"] == 1
    assert r["count"]["D"] == 1
    assert "C" not in r["count"], "false 分支不该执行"
    assert "E" not in r["count"]


# ------------------------------------------------------------ ③ 分支后汇合（V01 核心）


def test_branch_then_merge_prunes_dead_predecessor():
    """if 走 true 时，D 的 false 前驱 C 必须被剔除，否则 D 永久阻塞。"""
    g = build(
        [
            node("A", "open"),
            node("if1", "if"),
            node("B1", "click"),
            node("B2", "click"),
            node("C", "click"),
            node("D", "close"),
        ],
        [
            e("A", "if1"),
            e("if1", "B1", "true"),
            e("if1", "B2", "true"),
            e("if1", "C", "false"),
            e("B1", "D"),
            e("B2", "D"),
            e("C", "D"),
        ],
    )
    r = run_all(g, exits={"if1": "true"})
    assert r["count"]["B1"] == 1
    assert r["count"]["B2"] == 1
    assert r["count"]["D"] == 1, f"D 应执行 1 次，实际 {r['count'].get('D')!r}"
    assert "C" not in r["count"]
    assert r["waiting_left"] == {}, f"不应有残留等待: {r['waiting_left']}"


def test_branch_false_path_also_works():
    g = build(
        [
            node("A", "open"),
            node("if1", "if"),
            node("B", "click"),
            node("C", "click"),
            node("D", "close"),
        ],
        [e("A", "if1"), e("if1", "B", "true"), e("if1", "C", "false"), e("B", "D"), e("C", "D")],
    )
    r = run_all(g, exits={"if1": "false"})
    assert r["count"]["C"] == 1
    assert r["count"]["D"] == 1
    assert "B" not in r["count"]


# ------------------------------------------------------------ ④ 循环体边界


def test_loop_body_stops_at_done():
    g = build(
        [
            node("A", "open"),
            node("L", "loop"),
            node("B", "click"),
            node("C", "click"),
            node("Z", "close"),
        ],
        [
            e("A", "L"),
            e("L", "B", "loop"),
            e("B", "C"),
            e("C", "L"),  # 回边
            e("L", "Z", "done"),
        ],
    )
    body = g.loop_body("L")
    assert body == {"B", "C"}, f"body 应为 B/C，实际 {body}"
    assert "Z" not in body, "done 侧不能算进循环体"


def test_loop_join_preds_excludes_back_edge():
    g = build(
        [
            node("A", "open"),
            node("L", "loop"),
            node("B", "click"),
            node("Z", "close"),
        ],
        [e("A", "L"), e("L", "B", "loop"), e("B", "L"), e("L", "Z", "done")],
    )
    # L 的前驱：A（外部）+ B（回边）；join 只该算 A
    assert g.join_preds("L") == ["A"]


# ------------------------------------------------------------ 图解析


def test_start_and_isolated():
    g = build(
        [node("A", "open"), node("B", "click"), node("X", "note_hidden")],
        [e("A", "B")],
    )
    assert g.start == ["A"]
    assert g.isolated == ["X"]


def test_visual_nodes_filtered():
    flow = Flow(
        name="t",
        nodes=[node("A", "open"), node("N", "note"), node("G", "group")],
        edges=[],
    )
    g = Graph(flow)
    assert set(g.nodes) == {"A"}


def test_cycle_graph_has_start():
    """while 自环也要有起点，不能抛。"""
    g = build(
        [node("A", "open"), node("W", "while")],
        [e("A", "W"), e("W", "W")],
    )
    assert g.start == ["A"]


def test_stats():
    g = build(
        [node("A", "open"), node("if1", "if"), node("L", "loop")],
        [e("A", "if1"), e("if1", "L", "true")],
    )
    s = g.stats()
    assert s["nodes"] == 3
    assert s["branch_nodes"] == ["if1"]
    assert s["loop_nodes"] == ["L"]


# ------------------------------------------------------------ 深层汇合


def test_three_way_merge_all_paths():
    """三路汇合，全部走完时 D 只执行 1 次。"""
    g = build(
        [node(f"N{i}", "click") for i in range(3)] + [node("D", "close")],
        [
            e("N0", "D"),
            e("N1", "D"),
            e("N2", "D"),
        ],
    )
    # 三个节点都没前驱 → 都是 start
    r = run_all(g)
    assert r["count"]["D"] == 1
    assert r["waiting_left"] == {}
