"""主循环测试：串行/并行/分支/循环/on_fail/断点/从节点起跑。"""

from __future__ import annotations

import pytest

import nodes  # noqa: F401  触发节点注册
from engine.context import ExecutionContext
from engine.runner import RunOptions, Runner, run_flow
from engine.schema import Edge, Flow, OnFail, edge, node


def e(from_: str, to: str, exit: str | None = None) -> Edge:
    return edge(from_, to, exit)


def flow(nodes_: list, edges_: list, **kwargs) -> Flow:
    return Flow(name="t", nodes=nodes_, edges=edges_, **kwargs)


# ------------------------------------------------------------ 串行


@pytest.mark.asyncio
async def test_serial_two_nodes():
    f = flow([node("n1", "noop"), node("n2", "noop")], [e("n1", "n2")])
    r = await run_flow(f)
    assert r["ok"] is True
    assert [x["node_id"] for x in r["nodes"]] == ["n1", "n2"]


@pytest.mark.asyncio
async def test_failure_stops_by_default():
    f = flow([node("n1", "fail"), node("n2", "noop")], [e("n1", "n2")])
    r = await run_flow(f)
    assert r["ok"] is False
    assert "n1" in r["failed"]
    # 默认停：n2 不该执行
    assert [x["node_id"] for x in r["nodes"]] == ["n1"]


# ------------------------------------------------------------ on_fail


@pytest.mark.asyncio
async def test_on_fail_skip_continues():
    f = flow(
        [node("n1", "fail", on_fail=OnFail(mode="skip")), node("n2", "noop")],
        [e("n1", "n2")],
    )
    r = await run_flow(f)
    assert r["ok"] is True
    assert [x["node_id"] for x in r["nodes"]] == ["n1", "n2"]


@pytest.mark.asyncio
async def test_on_fail_retry_exhausts():
    """fail 节点重试 2 次，最终仍失败。"""
    f = flow(
        [node("n1", "fail", on_fail=OnFail(mode="retry", attempts=3, backoff_ms=0))],
        [],
    )
    r = await run_flow(f)
    assert r["ok"] is False
    lg = r["nodes"][0]
    assert lg["attempts"] == 3


@pytest.mark.asyncio
async def test_on_fail_stop():
    f = flow(
        [node("n1", "fail", on_fail=OnFail(mode="stop")), node("n2", "noop")],
        [e("n1", "n2")],
    )
    r = await run_flow(f)
    assert r["ok"] is False
    assert r["stopped"] is True


# ------------------------------------------------------------ 并行


@pytest.mark.asyncio
async def test_parallel_two_branches():
    f = flow(
        [node("a", "noop"), node("b", "noop"), node("c", "noop"), node("d", "noop")],
        [e("a", "b"), e("a", "c"), e("b", "d"), e("c", "d")],
    )
    r = await run_flow(f)
    assert r["ok"] is True
    # d 只执行一次
    assert r["scheduler"]["count"]["d"] == 1


# ------------------------------------------------------------ 分支


@pytest.mark.asyncio
async def test_if_true_branch():
    f = flow(
        [
            node("set", "set_var", name="x", value=5),
            node("if1", "if", left="{x}", op=">", right=3),
            node("yes", "noop"),
            node("no", "noop"),
        ],
        [
            e("set", "if1"),
            e("if1", "yes", "true"),
            e("if1", "no", "false"),
        ],
    )
    r = await run_flow(f)
    ids = [x["node_id"] for x in r["nodes"]]
    assert "yes" in ids
    assert "no" not in ids


@pytest.mark.asyncio
async def test_if_false_branch():
    f = flow(
        [
            node("set", "set_var", name="x", value=1),
            node("if1", "if", left="{x}", op="==", right=99),
            node("yes", "noop"),
            node("no", "noop"),
        ],
        [e("set", "if1"), e("if1", "yes", "true"), e("if1", "no", "false")],
    )
    r = await run_flow(f)
    ids = [x["node_id"] for x in r["nodes"]]
    assert "no" in ids
    assert "yes" not in ids


# ------------------------------------------------------------ 循环


@pytest.mark.asyncio
async def test_loop_runs_body_n_times():
    f = flow(
        [node("L", "loop", kind="count", count=3), node("b", "noop"), node("z", "noop")],
        [e("L", "b", "loop"), e("b", "L"), e("L", "z", "done")],
    )
    r = await run_flow(f)
    assert r["ok"] is True
    ids = [x["node_id"] for x in r["nodes"]]
    assert ids.count("b") == 3, f"body 应跑 3 次，实际 {ids.count('b')}"
    assert ids.count("z") == 1


@pytest.mark.asyncio
async def test_loop_zero_rounds():
    f = flow(
        [node("L", "loop", kind="count", count=0), node("b", "noop"), node("z", "noop")],
        [e("L", "b", "loop"), e("b", "L"), e("L", "z", "done")],
    )
    r = await run_flow(f)
    ids = [x["node_id"] for x in r["nodes"]]
    assert "b" not in ids
    assert "z" in ids


@pytest.mark.asyncio
async def test_foreach_runs_per_item():
    f = flow(
        [
            node("set", "set_var", name="rows", value=[1, 2, 3, 4]),
            node("F", "foreach", items="{rows}"),
            node("b", "noop"),
            node("z", "noop"),
        ],
        [e("set", "F"), e("F", "b", "loop"), e("b", "F"), e("F", "z", "done")],
    )
    r = await run_flow(f)
    ids = [x["node_id"] for x in r["nodes"]]
    assert ids.count("b") == 4


# ------------------------------------------------------------ 断言


@pytest.mark.asyncio
async def test_expect_without_page_passes():
    """没有浏览器时 expect 直接放过（不误判失败）。"""
    f = flow([node("n1", "noop", expect={"text": "永远不会出现"})], [])
    r = await run_flow(f)
    assert r["ok"] is True


# ------------------------------------------------------------ 断点 / 起跑


@pytest.mark.asyncio
async def test_breakpoint_callback_fires():
    seen: list[tuple[str, str]] = []

    def on_pause(nid: str, when: str) -> None:
        seen.append((nid, when))

    f = flow([node("n1", "noop"), node("n2", "noop")], [e("n1", "n2")])
    r = await Runner(f, ExecutionContext(), RunOptions(step=True, on_pause=on_pause)).execute()
    assert r["ok"] is True
    assert ("n1", "before") in seen
    assert ("n2", "after") in seen


@pytest.mark.asyncio
async def test_start_at_skips_upstream():
    f = flow(
        [node("n1", "fail"), node("n2", "noop"), node("n3", "noop")],
        [e("n1", "n2"), e("n2", "n3")],
    )
    r = await Runner(f, ExecutionContext(), RunOptions(start_at="n2")).execute()
    assert r["ok"] is True
    ids = [x["node_id"] for x in r["nodes"]]
    assert "n1" not in ids, "上游应被跳过"
    assert ids == ["n2", "n3"]


@pytest.mark.asyncio
async def test_disabled_node_skipped():
    n = node("n1", "fail")
    n.off = True
    f = flow([n, node("n2", "noop")], [e("n1", "n2")])
    r = await run_flow(f)
    assert r["ok"] is True
    assert r["nodes"][0]["skipped"] is True


@pytest.mark.asyncio
async def test_unknown_node_type_raises():
    f = flow([node("n1", "不存在的类型")], [])
    r = await run_flow(f)
    assert r["ok"] is False
    assert "未注册" in (r["nodes"][0]["error"] or "")
