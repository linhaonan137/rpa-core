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


# ------------------------------------------------------------ 删除落盘语义
#
# save() 是"合并写"（多进程共享 elements.json，不能整份覆盖），
# 但合并天然表达不了"消失"：内存删掉的条目，磁盘上还在，会原样合并回来。
# 实测 element_drop 报告成功、库里条目还在，元素库只增不减。
# 修法：墓碑（deleted 表）+ 文件锁 + 显式复活记录。


def _mk(key: str, css: str = "#x", name: str = "N"):
    return ElementEntry(key=key, css=css, role="button", name=name)


def test_remove_persists(tmp_path):
    p = str(tmp_path / "el.json")
    s = ElementStore(p)
    s.add(_mk("k1"))
    s.save()
    assert ElementStore(p).has("k1")

    d = ElementStore(p)
    d.remove("k1", force=True)
    d.save()
    assert not ElementStore(p).has("k1"), "删除必须落盘"


def test_stale_copy_does_not_resurrect(tmp_path):
    """持旧副本的实例写回（比如更新 hits）不能把删掉的条目复活。"""
    p = str(tmp_path / "el.json")
    s = ElementStore(p)
    s.add(_mk("k2"))
    s.save()

    stale = ElementStore(p)          # 删除发生前载入的副本
    rm = ElementStore(p)
    rm.remove("k2", force=True)
    rm.save()

    stale.get("k2").hit()            # 旧副本写回统计
    stale.save()
    assert not ElementStore(p).has("k2"), "旧副本不该复活已删条目"


def test_readd_same_second_clears_tombstone(tmp_path):
    """墓碑按秒记，同一秒内删除后重新拾取必须能成功（靠显式复活，不靠时间戳）。"""
    p = str(tmp_path / "el.json")
    s = ElementStore(p)
    s.add(_mk("k3"))
    s.save()

    rm = ElementStore(p)
    rm.remove("k3", force=True)
    rm.save()

    add = ElementStore(p)
    add.add(_mk("k3", css="#k3b", name="K3b"))
    add.save()
    assert ElementStore(p).has("k3"), "重新拾取应覆盖墓碑"


def test_concurrent_add_not_lost(tmp_path):
    """两个实例各自新增，互不覆盖（合并写的初衷，不能被删除逻辑破坏）。"""
    p = str(tmp_path / "el.json")
    a = ElementStore(p)
    b = ElementStore(p)
    a.add(_mk("ka"))
    a.save()
    b.add(_mk("kb"))
    b.save()
    final = ElementStore(p)
    assert final.has("ka") and final.has("kb")


def test_expired_tombstone_is_gc(tmp_path):
    """墓碑过期要清掉，否则每删一次留一条，文件无限变脏。"""
    import json as _json
    import time as _time

    from elements.store import TOMBSTONE_TTL_DAYS

    p = str(tmp_path / "el.json")
    old_ts = _time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        _time.localtime(_time.time() - (TOMBSTONE_TTL_DAYS + 5) * 86400),
    )
    fresh_ts = _time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(p, "w", encoding="utf-8") as f:
        _json.dump({"version": 2, "elements": {},
                    "deleted": {"old": old_ts, "recent": fresh_ts}}, f)

    s = ElementStore(p)
    s.add(_mk("keepme"))
    s.save()

    saved = _json.load(open(p, encoding="utf-8"))["deleted"]
    assert "old" not in saved, "超过 TTL 的墓碑应被清掉"
    assert "recent" in saved, "未过期的墓碑必须保留（还要挡旧副本复活）"
