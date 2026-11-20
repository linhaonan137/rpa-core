"""元素库测试：存储、反向索引、路径校验。"""

from __future__ import annotations

import json

import pytest

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
