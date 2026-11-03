"""补充模块测试：retry-from（S17）、子流程（S10-S12）、元素拾取器（E04）。"""

from __future__ import annotations

import json

import pytest

from engine.context import ExecutionContext
from engine.graph import Graph
from engine.runner import RunOptions, Runner, run_flow
from engine.schema import Edge, Flow, OnFail, edge, node

import nodes  # noqa: F401  触发节点注册


def e(from_: str, to: str, exit: str | None = None) -> Edge:
    return edge(from_, to, exit)


def flow(nodes_, edges_, **kw) -> Flow:
    return Flow(name="t", nodes=nodes_, edges=edges_, **kw)


# ============================================================ between（图）


def test_between_region():
    """A→B→C→D：between(A, C) 应为 {A,B,C}，不含 D。"""
    g = Graph(flow(
        [node("A", "noop"), node("B", "noop"), node("C", "noop"), node("D", "noop")],
        [e("A", "B"), e("B", "C"), e("C", "D")],
    ))
    assert g.between("A", "C") == {"A", "B", "C"}


def test_between_reverse_direction_empty():
    g = Graph(flow(
        [node("A", "noop"), node("B", "noop")],
        [e("A", "B")],
    ))
    assert g.between("B", "A") == set(), "逆向不构成区间"


# ============================================================ retry-from


@pytest.mark.asyncio
async def test_retry_from_recovers(monkeypatch):
    """B 第一次失败，回退到 A 重跑；第二次 B 成功 → 全流程 PASS。"""
    calls = {"b": 0}

    # 用一个可变的全局开关模拟「第二次就成功」
    from engine.registry import Result, register

    flaky_type = "flaky_retry_test"

    if flaky_type not in __import__("engine.registry", fromlist=["registry"]).registry.types():
        @register(flaky_type)
        async def _flaky(args, ctx):  # noqa: ANN001, ANN202
            calls["b"] += 1
            if calls["b"] < 2:
                return Result.fail("第一次故意失败")
            return Result.done("第二次成功")

    f = flow(
        [node("A", "noop"), node("B", flaky_type), node("C", "noop")],
        [e("A", "B"), e("B", "C")],
    )
    n_b = f.node("B")
    n_b.on_fail = OnFail(mode="back", rollback_to="A", attempts=2, backoff_ms=0)

    r = await run_flow(f)
    assert r["ok"] is True, f"应恢复成功，实际 failed={r['failed']}"
    assert calls["b"] == 2
    # A 和 B 各执行 2 次（回退重跑）
    assert r["scheduler"]["count"]["A"] == 2
    assert r["scheduler"]["count"]["B"] == 2
    assert r["scheduler"]["count"]["C"] == 1


@pytest.mark.asyncio
async def test_retry_from_exhausts_then_stop():
    from engine.registry import Result, register

    t = "always_fail_back"
    if t not in __import__("engine.registry", fromlist=["registry"]).registry.types():
        @register(t)
        async def _always(args, ctx):  # noqa: ANN001, ANN202
            return Result.fail("永远失败")

    f = flow(
        [node("A", "noop"), node("B", t), node("C", "noop")],
        [e("A", "B"), e("B", "C")],
    )
    f.node("B").on_fail = OnFail(mode="back", rollback_to="A", attempts=2, backoff_ms=0)

    r = await run_flow(f)
    assert r["ok"] is False
    assert r["stopped"] is True
    assert r["scheduler"]["count"]["B"] == 2, "attempts=2 → B 共尝试 2 次"
    assert "C" not in r["scheduler"]["count"]


@pytest.mark.asyncio
async def test_retry_from_multi_branch_degrades_to_stop():
    """并行分支里 back 不可用 → 降级为停（防止重置踩到别的分支）。"""
    from engine.registry import Result, register

    t = "fail_multi"
    if t not in __import__("engine.registry", fromlist=["registry"]).registry.types():
        @register(t)
        async def _f(args, ctx):  # noqa: ANN001, ANN202
            return Result.fail("失败")

    f = flow(
        [node("A", "noop"), node("B", t), node("B2", "noop"), node("C", "noop")],
        [e("A", "B"), e("A", "B2"), e("B", "C"), e("B2", "C")],
    )
    f.node("B").on_fail = OnFail(mode="back", rollback_to="A", attempts=1)

    r = await run_flow(f)
    assert r["ok"] is False
    assert r["stopped"] is True, "多分支 back 应回退成停"


@pytest.mark.asyncio
async def test_retry_from_bad_rollback_target_stops():
    f = flow([node("A", "noop"), node("B", "fail")], [e("A", "B")])
    f.node("B").on_fail = OnFail(mode="back", rollback_to="不存在的节点")
    r = await run_flow(f)
    assert r["ok"] is False
    assert r["stopped"] is True


# ============================================================ 子流程


@pytest.fixture()
def sub_flow_file(tmp_path, monkeypatch):
    """写一个子流程文件，并把 RPA_FLOWS 指过去。"""
    d = tmp_path / "flows"
    d.mkdir()
    f = flow(
        [node("s1", "set_var", name="greet", value="你好 {name}"), node("s2", "noop")],
        [e("s1", "s2")],
    )
    (d / "sub.json").write_text(json.dumps(f.to_json(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RPA_FLOWS", str(d))
    return d


@pytest.mark.asyncio
async def test_subflow_runs_and_isolates_vars(sub_flow_file):
    """子流程变量隔离：父流程的 x 传进去，子流程里改 greet 不影响父流程同名变量。"""
    f = flow(
        [
            node("p1", "set_var", name="name", value="发票"),
            node("p2", "subflow", flow="sub", input={"name": "{name}"}, save_as="sub_out"),
            node("p3", "noop"),
        ],
        [e("p1", "p2"), e("p2", "p3")],
    )
    ctx = ExecutionContext()
    r = await run_flow(f, ctx)
    assert r["ok"] is True, f"子流程应成功: {[x['error'] for x in r['nodes'] if x['error']]}"
    out = ctx.get_var("sub_out")
    assert out["greet"] == "你好 发票"
    # 子流程变量没有漏到父流程
    assert not ctx.has_var("greet")


@pytest.mark.asyncio
async def test_subflow_failure_bubbles_up(sub_flow_file):
    """子流程失败必须上浮（success=False 不能被吞成 True）。"""
    bad = flow([node("s1", "fail", error="子流程内部错误")], [])
    (sub_flow_file / "bad.json").write_text(
        json.dumps(bad.to_json(), ensure_ascii=False), encoding="utf-8"
    )

    f = flow([node("p1", "subflow", flow="bad"), node("p2", "noop")], [e("p1", "p2")])
    r = await run_flow(f)
    assert r["ok"] is False
    assert "子流程 bad 失败" in (r["nodes"][0]["error"] or "")
    assert "p2" not in [x["node_id"] for x in r["nodes"]], "失败后不应继续"


@pytest.mark.asyncio
async def test_subflow_missing_file_fails():
    f = flow([node("p1", "subflow", flow="不存在的流程")], [])
    r = await run_flow(f)
    assert r["ok"] is False
    assert "不存在" in (r["nodes"][0]["error"] or "")


@pytest.mark.asyncio
async def test_subflow_missing_flow_arg_fails():
    f = flow([node("p1", "subflow")], [])
    r = await run_flow(f)
    assert r["ok"] is False
    assert "缺 flow" in (r["nodes"][0]["error"] or "")
