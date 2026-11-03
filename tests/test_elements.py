"""元素库测试：存储、反向索引、打分器三道门。"""

from __future__ import annotations

import json

import pytest

from elements.matcher import (
    Candidate,
    decide,
    match,
    prefix_ratio,
    rank,
    score,
    text_sim,
    WEIGHTS,
)
from elements.store import (
    ElementEntry,
    ElementError,
    ElementStore,
    Fingerprint,
    Geometry,
    make_entry,
    scan_refs,
)


# ------------------------------------------------------------ 造数据


def fp(
    tag="button",
    attrs=None,
    path=None,
    siblings=None,
    geo=None,
    role="button",
    name="提交订单",
) -> Fingerprint:
    return Fingerprint(
        role=role,
        name=name,
        tag=tag,
        attrs=attrs or {"id": "submit", "data-testid": "go"},
        path=path or ["form#f", "div.actions", "button"],
        siblings=siblings or ["取消", "保存"],
        geometry=Geometry(**geo) if geo else Geometry(x=100, y=200, w=80, h=30),
    )


def cand(**kw) -> Candidate:
    base = dict(
        role="button", name="提交订单", tag="button",
        attrs={"id": "submit", "data-testid": "go"},
        path=["form#f", "div.actions", "button"],
        siblings=["取消", "保存"], geometry={"x": 100, "y": 200, "w": 80, "h": 30},
    )
    base.update(kw)
    return Candidate(**base)


# ------------------------------------------------------------ store


def test_add_and_get(tmp_path):
    s = ElementStore(str(tmp_path / "el.json"))
    e = make_entry("btn", "button", "提交", css="#go")
    s.add(e)
    assert s.has("btn")
    assert s.get("btn").name == "提交"
    assert len(s) == 1


def test_duplicate_key_rejected(tmp_path):
    s = ElementStore(str(tmp_path / "el.json"))
    s.add(make_entry("btn", "button", "提交", css="#go"))
    with pytest.raises(ElementError, match="已存在"):
        s.add(make_entry("btn", "button", "别的", css="#go2"))


def test_overwrite_allowed(tmp_path):
    s = ElementStore(str(tmp_path / "el.json"))
    s.add(make_entry("btn", "button", "提交", css="#go"))
    s.add(make_entry("btn", "button", "改过", css="#go"), overwrite=True)
    assert s.get("btn").name == "改过"


def test_validate_requires_role_or_name(tmp_path):
    s = ElementStore(str(tmp_path / "el.json"))
    with pytest.raises(ElementError, match="至少一个非空"):
        s.add(ElementEntry(key="x", role=None, name=None, css="#a"))


def test_validate_requires_path_or_css(tmp_path):
    s = ElementStore(str(tmp_path / "el.json"))
    with pytest.raises(ElementError, match="path 与 css"):
        s.add(ElementEntry(key="x", role="button", name="a"))


def test_save_load_roundtrip(tmp_path):
    p = str(tmp_path / "el.json")
    s = ElementStore(p)
    e = ElementEntry(key="btn", role="button", name="提交", css="#go", fingerprint=fp())
    s.add(e)
    s.save()

    s2 = ElementStore(p)
    got = s2.get("btn")
    assert got.role == "button"
    assert got.fingerprint.tag == "button"
    assert got.fingerprint.geometry.x == 100
    assert got.fingerprint.path == ["form#f", "div.actions", "button"]


def test_save_is_atomic_no_tmp_left(tmp_path):
    p = tmp_path / "el.json"
    s = ElementStore(str(p))
    s.add(make_entry("btn", "button", "提交", css="#go"))
    s.save()
    assert p.exists()
    assert not (tmp_path / "el.json.tmp").exists()


def test_remove_blocked_when_referenced(tmp_path):
    s = ElementStore(str(tmp_path / "el.json"))
    s.add(make_entry("btn", "button", "提交", css="#go"))
    s.set_refs({"btn": {"flow1:n1"}})
    with pytest.raises(ElementError, match="仍被引用"):
        s.remove("btn")
    s.remove("btn", force=True)
    assert not s.has("btn")


def test_stale_detection(tmp_path):
    s = ElementStore(str(tmp_path / "el.json"))
    e = make_entry("btn", "button", "提交", css="#go")
    s.add(e)
    for _ in range(3):
        s.get("btn").miss()
    assert s.stale() == ["btn"]


def test_unused(tmp_path):
    s = ElementStore(str(tmp_path / "el.json"))
    s.add(make_entry("a", "button", "A", css="#a"))
    s.add(make_entry("b", "button", "B", css="#b"))
    s.set_refs({"a": {"f:n1"}})
    assert s.unused() == ["b"]


# ------------------------------------------------------------ scan_refs


def test_scan_refs_finds_element_refs():
    flows = [
        {"name": "f1", "nodes": [
            {"id": "n1", "args": {"target": "@el:submit"}},
            {"id": "n2", "args": {"target": "#plain"}},
        ]},
        {"name": "f2", "nodes": [
            {"id": "n9", "args": {"pairs": [{"label": "x", "value": "@el:submit"}]}},
        ]},
    ]
    refs = scan_refs(flows)
    assert refs["submit"] == {"f1:n1", "f2:n9"}


def test_scan_refs_nested():
    flows = [{"name": "f", "nodes": [{"id": "n", "args": {"a": {"b": ["@el:deep"]}}}]}]
    assert scan_refs(flows) == {"deep": {"f:n"}}


# ------------------------------------------------------------ 打分基元


@pytest.mark.parametrize("a,b,expect", [
    ("提交", "提交", 1.0),
    ("提交", "提交订单", 0.8),
    ("", "x", 0.0),
    (None, "x", 0.0),
])
def test_text_sim(a, b, expect):
    assert text_sim(a, b) == expect


def test_prefix_ratio():
    assert prefix_ratio(["a", "b", "c"], ["a", "b", "d"]) == pytest.approx(2 / 3)
    assert prefix_ratio([], ["a"]) == 0.0


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


# ------------------------------------------------------------ 打分


def test_exact_match_scores_high():
    s = score(cand(), fp())
    assert s.score > 0.95, f"完全一致应接近 1，实际 {s.score}"


def test_class_only_change_still_high():
    """只改 class 时分数仍然高（class 不在稳定属性里）。"""
    s = score(cand(attrs={"id": "submit", "data-testid": "go", "class": "btn-new"}), fp())
    assert s.score > 0.9


def test_moved_position_still_reasonable():
    """位置变了但语义/属性没变 → 仍然高分（几何只占 0.05）。"""
    s = score(cand(geometry={"x": 900, "y": 900, "w": 80, "h": 30}), fp())
    assert s.score > 0.85


def test_totally_different_low():
    s = score(
        cand(role="link", name="首页", tag="a", attrs={"href": "/"},
             path=["nav", "a"], siblings=[], geometry={"x": 0, "y": 0, "w": 10, "h": 10}),
        fp(),
    )
    assert s.score < 0.3


def test_parts_are_explainable():
    s = score(cand(), fp())
    assert set(s.parts) == set(WEIGHTS)
    assert all(0 <= v <= 1 for v in s.parts.values())


# ------------------------------------------------------------ 三道门


def test_gate1_low_score_reports():
    ranked = rank([cand(role="link", name="首页", tag="a", attrs={}, path=[], siblings=[], geometry={})], fp())
    d = decide(ranked)
    assert d.action == "report"
    assert "低于阈值" in d.reason


def test_gate2_ambiguous_reports():
    """两个几乎一样的候选 → 判歧义。"""
    c1 = cand(ref="r1")
    c2 = cand(ref="r2")
    d = decide(rank([c1, c2], fp()))
    assert d.action == "report"
    assert "歧义" in d.reason


def test_gate2_clear_winner_commits():
    c1 = cand(ref="r1")
    c2 = cand(role="link", name="取消", tag="a", attrs={}, path=["x"], siblings=[],
              geometry={"x": 0, "y": 0, "w": 1, "h": 1})
    d = decide(rank([c1, c2], fp()))
    assert d.action == "commit"


def test_gate3_verify_failure_reports():
    c1 = cand(ref="r1")
    c2 = cand(role="link", name="取消", tag="a", attrs={}, path=["x"], siblings=[],
              geometry={"x": 0, "y": 0, "w": 1, "h": 1})
    d = match([c1, c2], fp(), verify=lambda c: False)
    assert d.action == "report"
    assert "试跑" in d.reason


def test_gate3_verify_success_commits():
    c1 = cand(ref="r1")
    c2 = cand(role="link", name="取消", tag="a", attrs={}, path=["x"], siblings=[],
              geometry={"x": 0, "y": 0, "w": 1, "h": 1})
    d = match([c1, c2], fp(), verify=lambda c: c.ref == "r1")
    assert d.action == "commit"


def test_no_candidates_reports():
    d = decide([])
    assert d.action == "report"
    assert "没有候选" in d.reason


def test_role_and_name_passed_to_rank():
    """rank 的 role/name 覆盖档案里的空值。"""
    f = Fingerprint(tag="button", attrs={}, path=[], siblings=[], geometry=Geometry())
    ranked = rank([cand()], f, role="button", name="提交订单")
    assert ranked[0].parts["role"] > 0
    assert ranked[0].parts["name"] > 0
