"""自愈测试：判层 / 改元素库（快照·应用·回滚·日志）/ heal_once 编排。

全部 mock：不启浏览器、不调真 LLM、不写项目目录（一律落 tmp_path）。
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

import heal.candidates as cand_mod
from elements.matcher import Candidate
from elements.store import ElementEntry, ElementStore, Fingerprint, Geometry
from engine.context import ExecutionContext
from engine.schema import Flow, edge, node

from heal import agent as agent_mod
from heal.agent import heal_once
from heal.candidates import candidates_as_dicts, collect, pick
from heal.context import FailureContext, build_failure
from heal.patch import apply_to_element, log_change, restore_element, snapshot_element
from heal.rules import classify, diagnose, retry_with_reload


# ---------------------------------------------------------------- 造数据


def fp_submit() -> Fingerprint:
    return Fingerprint(
        role="button",
        name="提交订单",
        tag="button",
        attrs={"id": "submit", "data-testid": "go"},
        path=["form#f", "div.actions", "button"],
        siblings=["取消", "保存"],
        geometry=Geometry(x=100, y=200, w=80, h=30),
    )


def new_store(tmp_path) -> ElementStore:
    store = ElementStore(str(tmp_path / "elements.json"))
    store.add(
        ElementEntry(
            key="submit",
            role="button",
            name="提交订单",
            css="#submit",
            fingerprint=fp_submit(),
        )
    )
    store.save()
    return store


def cand_submit(ref: str = "button.submit-btn") -> Candidate:
    return Candidate(
        role="button",
        name="提交订单",
        tag="button",
        attrs={"id": "submit", "data-testid": "go"},
        path=["form#f", "div.actions", "button"],
        siblings=["取消", "保存"],
        geometry={"x": 100, "y": 200, "w": 80, "h": 30},
        ref=ref,
    )


def cand_cancel() -> Candidate:
    return Candidate(
        role="link",
        name="取消",
        tag="a",
        attrs={},
        path=["nav"],
        siblings=[],
        geometry={"x": 0, "y": 0, "w": 1, "h": 1},
        ref="a.cancel",
    )


def fc(**kw) -> FailureContext:
    base = dict(
        flow_name="demo",
        node_id="n5",
        node_type="web_click",
        args={"target": "@el:submit"},
        error="Timeout 30000ms waiting for selector",
        url="https://shop.test/order",
        candidates=[],
        snapshot_path=None,
        layer="L1",
    )
    base.update(kw)
    return FailureContext(**base)


def row(name="提交订单", score=0.95, **kw):
    d = {"role": "button", "name": name, "css": "button.x", "visible": True, "score": score}
    d.update(kw)
    return d


def demo_flow() -> Flow:
    return Flow(
        name="demo",
        nodes=[node("n1", "noop"), node("n5", "web_click", target="@el:submit")],
        edges=[edge("n1", "n5")],
    )


def fail_result(node_id: str = "n5") -> dict:
    return {
        "ok": False,
        "stopped": False,
        "ms": 30.0,
        "steps": 2,
        "nodes": [
            {"node_id": "n1", "type": "web_open", "ok": True, "ms": 10.0,
             "error": None, "skipped": False, "attempts": 1, "tolerated": False},
            {"node_id": node_id, "type": "web_click", "ok": False, "ms": 30000.0,
             "error": "Timeout 30000ms waiting for selector",
             "skipped": False, "attempts": 2, "tolerated": False},
        ],
        "failed": [node_id],
        "scheduler": {},
    }


def ok_result() -> dict:
    res = fail_result()
    res["ok"] = True
    res["failed"] = []
    res["nodes"][1].update({"ok": True, "error": None, "ms": 120.0})
    return res


def install_runner(monkeypatch, results: list[dict]) -> list[str | None]:
    """替换 heal.agent.run_flow：按顺序返回给定结果，记录 start_at。"""
    calls: list[str | None] = []

    async def fake_run(flow, ctx, opts=None):
        calls.append(getattr(opts, "start_at", None))
        idx = min(len(calls) - 1, len(results) - 1)
        return copy.deepcopy(results[idx])

    monkeypatch.setattr(agent_mod, "run_flow", fake_run)
    return calls


def install_collect(monkeypatch, cands: list[Candidate]) -> None:
    async def fake_collect(page, entry=None, limit_ms=5000):
        return list(cands)

    monkeypatch.setattr(agent_mod, "collect", fake_collect)


def read_changes(path) -> list[dict]:
    p = path / "demo.changes.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------- classify


def test_classify_login_url_reports_l3():
    assert classify(fc(url="https://shop.test/login?next=/order")) == "L3"


def test_classify_captcha_element_reports_l3():
    got = classify(fc(candidates=[row(name="请输入验证码", score=0.1)]))
    assert got == "L3"


def test_classify_no_candidates_is_l2():
    assert classify(fc(candidates=[])) == "L2"


def test_classify_unique_high_is_l1():
    assert classify(fc(candidates=[row(score=0.96), row(name="取消", score=0.30)])) == "L1"


def test_classify_ambiguous_high_is_l15():
    assert classify(fc(candidates=[row(score=0.96), row(score=0.93)])) == "L1.5"


def test_classify_all_low_is_l15():
    assert classify(fc(candidates=[row(name="首页", score=0.20)])) == "L1.5"


@pytest.mark.parametrize("score_,expect", [(0.75, "L1"), (0.679, "L1.5")])
def test_classify_threshold_boundary(score_, expect):
    assert classify(fc(candidates=[row(score=score_)])) == expect


def test_classify_ignores_input_order():
    cands = [row(name="取消", score=0.31), row(score=0.97)]
    assert classify(fc(candidates=cands)) == "L1"


def test_classify_unscored_single_is_l1():
    """外部只塞了裸候选：只有一个就当唯一命中，多个宁可诊断。"""
    bare = {"role": "button", "name": "提交订单", "css": "button.x"}
    assert classify(fc(candidates=[bare])) == "L1"
    assert classify(fc(candidates=[bare, dict(bare)])) == "L1.5"


def test_retry_with_reload_does_not_mutate():
    src = {"target": "@el:submit"}
    out = retry_with_reload(src)
    assert out["reload"] is True
    assert "reload" not in src


def test_diagnose_high_but_ambiguous_goes_back_l1():
    entry = ElementEntry(key="submit", role="button", name="提交订单", css="#submit", fingerprint=fp_submit())
    ctx = fc(candidates=candidates_as_dicts([cand_submit(), cand_submit("button.alt")], fp_submit()))
    layer, why = diagnose(ctx, pick(ctx, entry))
    assert layer == "L1" and "歧义" in why


# ---------------------------------------------------------------- patch


def test_patch_applies_and_marks_heal(tmp_path):
    store = new_store(tmp_path)
    res = apply_to_element(store, "submit", {"css": "button.submit-btn"}, "候选命中 role=button")

    assert res["ok"] is True
    assert res["changes"]["css"] == {"old": "#submit", "new": "button.submit-btn"}
    assert res["saved"] is True

    entry = store.get("submit")
    assert entry.css == "button.submit-btn"
    assert entry.heal["by"] == "L1"
    assert entry.heal["from"] == "#submit"
    assert entry.meta["heal_count"] == 1


def test_patch_persists_to_disk(tmp_path):
    store = new_store(tmp_path)
    apply_to_element(store, "submit", {"css": "button.new"}, "换选择器")

    reloaded = ElementStore(str(tmp_path / "elements.json"))
    assert reloaded.get("submit").css == "button.new"


def test_patch_snapshot_then_apply(tmp_path):
    store = new_store(tmp_path)
    snap = snapshot_element(store, "submit")

    assert snap, "快照必须落盘"
    data = json.loads(open(snap, encoding="utf-8").read())
    assert data["entry"]["css"] == "#submit"
    assert data["key"] == "submit"

    second = snapshot_element(store, "submit")
    assert second != snap, "第二次快照要换编号，不能覆盖"


def test_patch_restore_rolls_back_exactly(tmp_path):
    store = new_store(tmp_path)
    old = store.get("submit").to_dict()

    apply_to_element(store, "submit", {"css": "button.new", "attrs": {"class": "btn-new"}}, "试改")
    assert store.get("submit").fingerprint.attrs.get("class") == "btn-new"

    res = restore_element(store, "submit", old)
    assert res["ok"] is True

    entry = store.get("submit")
    assert entry.css == "#submit"
    assert entry.fingerprint.attrs == {"id": "submit", "data-testid": "go"}
    assert entry.heal is None


def test_patch_rejects_invalid_entry_without_half_write(tmp_path):
    store = ElementStore(str(tmp_path / "el2.json"))
    store.add(
        ElementEntry(
            key="x",
            role="button",
            name="X",
            fingerprint=Fingerprint(role="button", name="X", tag="button", path=["div.x"]),
        )
    )

    res = apply_to_element(store, "x", {"path": []}, "清空 path")

    assert res["ok"] is False
    assert "非法" in res["error"]
    assert store.get("x").fingerprint.path == ["div.x"], "校验失败不能动原条目"


def test_patch_unknown_key_returns_error(tmp_path):
    store = new_store(tmp_path)
    res = apply_to_element(store, "nope", {"css": "#x"}, "瞎改")
    assert res["ok"] is False
    assert "不存在" in res["error"]


def test_patch_log_appends_jsonl(tmp_path):
    meta = tmp_path / "meta"
    log_change("demo", {"node": "n5", "action": "patch_element"}, meta_dir=str(meta))
    log_change("demo", {"node": "n5", "verified": "pass"}, meta_dir=str(meta))

    lines = (meta / "demo.changes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(x) for x in lines]
    assert all(r["flow"] == "demo" and r["ts"] for r in records)
    assert records[1]["verified"] == "pass"


# ---------------------------------------------------------------- 候选 / 决策


@pytest.mark.asyncio
async def test_collect_prefers_visible(monkeypatch):
    rows = [
        {"role": "button", "name": "提交订单", "css": "button.hidden", "visible": False, "score": 0.9},
        {"role": "button", "name": "提交订单", "css": "button.real", "visible": True, "score": 0.8},
    ]

    async def fake(page, hints, limit=5):
        return rows

    monkeypatch.setattr(cand_mod.middleware, "candidates", fake)
    out = await collect(SimpleNamespace())

    assert [c.ref for c in out] == ["button.real"]


@pytest.mark.asyncio
async def test_collect_without_page_is_empty():
    assert await collect(None) == []


def test_pick_commits_on_clear_winner():
    entry = ElementEntry(key="submit", role="button", name="提交订单", css="#submit", fingerprint=fp_submit())
    ctx = fc(candidates=candidates_as_dicts([cand_submit(), cand_cancel()], fp_submit()))
    assert pick(ctx, entry).ok is True


def test_pick_reports_ambiguity():
    entry = ElementEntry(key="submit", role="button", name="提交订单", css="#submit", fingerprint=fp_submit())
    ctx = fc(candidates=candidates_as_dicts([cand_submit(), cand_submit("button.alt")], fp_submit()))
    d = pick(ctx, entry)
    assert d.ok is False and "歧义" in d.reason


def test_pick_without_entry_reports():
    assert pick(fc(candidates=[row()]), None).ok is False


# ---------------------------------------------------------------- build_failure


def test_build_failure_returns_none_when_ok():
    assert build_failure(ok_result(), demo_flow()) is None


def test_build_failure_extracts_node_and_element():
    page = SimpleNamespace(url="https://shop.test/order")
    ctx = ExecutionContext()
    got = build_failure(fail_result(), demo_flow(), ctx, page)

    assert got is not None
    assert got.node_id == "n5"
    assert got.node_type == "web_click"
    assert got.element_key == "submit"
    assert got.url == "https://shop.test/order"
    assert "Timeout" in got.error


def test_build_failure_expect_log_uses_node_type():
    res = fail_result()
    res["nodes"][1] = {"node_id": "n5", "type": "expect", "ok": False,
                       "ms": 0.0, "error": "postAssert 失败: 元素不存在"}
    got = build_failure(res, demo_flow())
    assert got.node_type == "web_click"


# ---------------------------------------------------------------- heal_once


@pytest.mark.asyncio
async def test_heal_once_first_run_ok(monkeypatch):
    calls = install_runner(monkeypatch, [ok_result()])
    r = await heal_once(demo_flow(), "demo", ExecutionContext())

    assert r.ok is True
    assert r.action == "retry"
    assert r.rounds == 0
    assert calls == [None]


@pytest.mark.asyncio
async def test_heal_once_l1_patch_and_resume(monkeypatch, tmp_path):
    store = new_store(tmp_path)
    calls = install_runner(monkeypatch, [fail_result(), ok_result()])
    install_collect(monkeypatch, [cand_submit(), cand_cancel()])
    meta = tmp_path / "meta"
    ctx = ExecutionContext()

    r = await heal_once(
        demo_flow(), "demo", ctx,
        element_store=store,
        page=SimpleNamespace(url="https://shop.test/order"),
        meta_dir=str(meta),
        snap_dir=str(tmp_path / ".versions"),
    )

    assert r.ok is True and r.layer == "L1"
    assert r.action == "patch_element"
    assert r.patched is True and r.retried is True
    assert calls == [None, "n5"], "必须从失败节点续跑，不能从头"

    entry = store.get("submit")
    assert entry.css == "button.submit-btn"
    assert entry.heal["by"] == "L1"
    assert ctx.elements["submit"]["css"] == "button.submit-btn", "ctx 也要换新值"

    recs = read_changes(meta)
    assert any(x.get("verified") == "pass" for x in recs)
    assert any(x.get("action") == "patch_element" for x in recs)
    assert list((tmp_path / ".versions").glob("elements.submit.snap-*.json")), "改前必须落快照"


@pytest.mark.asyncio
async def test_heal_once_l1_trial_fail_rolls_back(monkeypatch, tmp_path):
    store = new_store(tmp_path)
    calls = install_runner(monkeypatch, [fail_result(), fail_result(), fail_result()])
    install_collect(monkeypatch, [cand_submit(), cand_cancel()])
    meta = tmp_path / "meta"

    r = await heal_once(
        demo_flow(), "demo", ExecutionContext(),
        element_store=store,
        page=SimpleNamespace(url="https://shop.test/order"),
        max_rounds=2,
        meta_dir=str(meta),
        snap_dir=str(tmp_path / ".versions"),
    )

    assert r.ok is False
    assert r.action == "report"
    assert r.patched is False
    assert calls[0] is None and calls[1] == "n5"

    entry = store.get("submit")
    assert entry.css == "#submit", "试跑失败必须回滚"
    assert entry.heal is None
    assert entry.fingerprint.attrs == {"id": "submit", "data-testid": "go"}

    recs = read_changes(meta)
    assert any(x.get("verified") == "fail" and x.get("rolled_back") is True for x in recs)


@pytest.mark.asyncio
async def test_heal_once_l3_reports_without_retry(monkeypatch, tmp_path):
    store = new_store(tmp_path)
    calls = install_runner(monkeypatch, [fail_result()])
    install_collect(monkeypatch, [cand_submit()])

    r = await heal_once(
        demo_flow(), "demo", ExecutionContext(),
        element_store=store,
        page=SimpleNamespace(url="https://shop.test/login?next=/order"),
        meta_dir=str(tmp_path / "meta"),
    )

    assert r.ok is False
    assert r.layer == "L3"
    assert r.action == "report"
    assert "登录" in r.reason
    assert calls == [None], "L3 不该再试跑"
    assert store.get("submit").css == "#submit"


@pytest.mark.asyncio
async def test_heal_once_stops_at_max_rounds(monkeypatch, tmp_path):
    """L2 反复失败 → 到轮次上限必须报人，且把节点改回原值。"""
    flow = demo_flow()
    calls = install_runner(monkeypatch, [fail_result(), fail_result(), fail_result()])
    install_collect(monkeypatch, [cand_submit()])
    meta = tmp_path / "meta"

    r = await heal_once(
        flow, "demo", ExecutionContext(),
        element_store=None,  # 没有元素库 → L2
        page=SimpleNamespace(url="https://shop.test/order"),
        llm=lambda prompt: '{"target": "button.pay"}',
        max_rounds=2,
        meta_dir=str(meta),
    )

    assert r.ok is False
    assert r.action == "report"
    assert r.rounds == 2
    assert "最大轮次" in r.reason
    assert flow.node("n5").args["target"] == "@el:submit", "L2 失败要改回原 target"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_heal_once_no_element_ref_reports(monkeypatch, tmp_path):
    calls = install_runner(monkeypatch, [fail_result()])
    install_collect(monkeypatch, [cand_submit()])

    r = await heal_once(
        demo_flow(), "demo", ExecutionContext(),
        element_store=None,
        page=SimpleNamespace(url="https://shop.test/order"),
        meta_dir=str(tmp_path / "meta"),
    )

    assert r.ok is False
    assert r.action == "report"
    assert "元素库" in r.reason
    assert "LLM" in r.reason
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_heal_once_l2_llm_success(monkeypatch, tmp_path):
    flow = demo_flow()
    calls = install_runner(monkeypatch, [fail_result(), ok_result()])
    install_collect(monkeypatch, [cand_submit()])

    r = await heal_once(
        flow, "demo", ExecutionContext(),
        element_store=None,
        page=SimpleNamespace(url="https://shop.test/order"),
        llm=lambda prompt: '{"target": "button.pay"}',
        meta_dir=str(tmp_path / "meta"),
    )

    assert r.ok is True and r.layer == "L2"
    assert r.action == "patch_flow"
    assert flow.node("n5").args["target"] == "button.pay"
    assert calls == [None, "n5"]


@pytest.mark.asyncio
async def test_heal_once_async_verify_rejects_patch(monkeypatch, tmp_path):
    store = new_store(tmp_path)
    install_runner(monkeypatch, [fail_result(), fail_result()])
    install_collect(monkeypatch, [cand_submit(), cand_cancel()])

    async def reject(cand):
        return False

    r = await heal_once(
        demo_flow(), "demo", ExecutionContext(),
        element_store=store,
        page=SimpleNamespace(url="https://shop.test/order"),
        verify=reject,
        max_rounds=1,
        meta_dir=str(tmp_path / "meta"),
    )

    assert r.ok is False
    assert r.patched is False
    assert store.get("submit").css == "#submit", "试跑动作不过门就不许改库"
