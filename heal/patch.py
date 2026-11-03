"""改元素库 + 改前快照 + 变更流水。**L1 只动这里，不动流程节点。**

三条硬约束（H13 / H14 / H07）：
  1. 写之前先落快照（`.versions/elements.<key>.snap-NNNN.json`，原子写）
  2. 校验不过就不写——先构造副本、validate 通过才 `store.add`，不留半修状态
  3. 每笔改动都进 `_meta/<flow>.changes.jsonl`，含 old→new 与 reason

为什么用 `store.add(overwrite=True)` 而不是直接改字段：`add` 会跑
`entry.validate()`，校验失败时原条目完全没被碰过，天然满足"要么全改要么不改"。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from elements.store import ElementEntry, ElementError, ElementStore
from engine.paths import FLOWS_DIR, META_DIR_NAME, PROJECT_ROOT, VERSIONS_DIR_NAME

#: 快照目录名（落在元素库文件所在目录，或项目根）
SNAP_DIR = VERSIONS_DIR_NAME
#: 变更流水目录（默认 flows/_meta）
META_DIR = str(FLOWS_DIR / META_DIR_NAME)

#: 允许从候选 dict 平铺进来的指纹字段
_FP_FIELDS = ("tag", "attrs", "path", "siblings", "geometry", "frame")
#: 允许覆盖的元素条目顶层字段
_TOP_FIELDS = ("role", "name", "css", "hints")

_UNSAFE = re.compile(r"[^\w.-]+")


def apply_to_element(
    store: ElementStore,
    key: str,
    new: dict[str, Any],
    reason: str,
    by: str = "L1",
) -> dict[str, Any]:
    """更新元素库条目，打 `_heal` 痕迹。返回 {ok, key, changes}。

    失败（key 不存在 / 新值校验不过 / 写盘失败）都返回 ok=False，不抛。
    """
    if store is None:
        return {"ok": False, "key": key, "changes": {}, "error": "没有元素库"}

    try:
        old_entry = store.get(key)
    except ElementError as exc:
        return {"ok": False, "key": key, "changes": {}, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 存储实现异常也转结构化结果
        return {"ok": False, "key": key, "changes": {}, "error": f"{type(exc).__name__}: {exc}"}

    old = old_entry.to_dict()
    merged = _deep_merge(old, _normalize(new))
    merged["key"] = key

    try:
        cand = ElementEntry.from_dict(merged)
        cand.validate()
    except (ElementError, ValueError, TypeError) as exc:
        return {"ok": False, "key": key, "changes": {}, "error": f"改后条目非法: {exc}"}

    ts = _now()
    cand.heal = {
        "ts": ts,
        "from": _old_target(old),
        "why": str(reason or ""),
        "by": str(by or "L1"),
    }
    cand.meta = dict(cand.meta or {})
    cand.meta["healed_at"] = ts
    try:
        cand.meta["heal_count"] = int(cand.meta.get("heal_count", 0)) + 1
    except (TypeError, ValueError):
        cand.meta["heal_count"] = 1

    try:
        store.add(cand, overwrite=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "key": key, "changes": {}, "error": f"写元素库失败: {exc}"}

    saved = _save(store)
    return {
        "ok": True,
        "key": key,
        "changes": _diff(old, cand.to_dict()),
        "entry": cand.to_dict(),
        "saved": saved,
    }


def restore_element(store: ElementStore, key: str, old: dict[str, Any], by: str = "rollback") -> dict[str, Any]:
    """回滚到改动前的条目（整体替换，不做合并，避免残留补丁加的属性）。"""
    if store is None or not isinstance(old, dict):
        return {"ok": False, "key": key, "error": "没有可回滚的旧条目"}

    try:
        entry = ElementEntry.from_dict({**old, "key": key})
        entry.validate()
        store.add(entry, overwrite=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "key": key, "error": f"回滚失败: {exc}"}

    saved = _save(store)
    return {"ok": True, "key": key, "saved": saved, "entry": entry.to_dict()}


def snapshot_element(
    store: ElementStore, key: str, snap_dir: str | os.PathLike[str] | None = None
) -> str:
    """改前快照。返回文件路径；失败返回 ""（调用方用内存里的旧值兜底）。"""
    if store is None:
        return ""
    try:
        entry = store.get(key)
    except Exception:  # noqa: BLE001
        return ""

    try:
        base = Path(store.path).parent if store.path else PROJECT_ROOT
        base = base / (snap_dir or SNAP_DIR)
        base.mkdir(parents=True, exist_ok=True)

        stem = f"elements.{_safe(key)}.snap"
        n = len(list(base.glob(f"{stem}-*.json"))) + 1
        path = base / f"{stem}-{n:04d}.json"
        payload = {
            "version": 1,
            "ts": _now(),
            "key": key,
            "source": store.path,
            "entry": entry.to_dict(),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # 原子写：断电不留半截文件
        return str(path)
    except Exception:  # noqa: BLE001 - 快照失败不能挡住修复
        return ""


def log_change(
    flow_name: str, rec: dict[str, Any], meta_dir: str | os.PathLike[str] | None = None
) -> None:
    """追加 `_meta/<flow>.changes.jsonl`。写不进去就算了，绝不抛。"""
    try:
        d = Path(meta_dir) if meta_dir is not None else Path(META_DIR)
        d.mkdir(parents=True, exist_ok=True)
        payload = {"ts": _now(), "flow": str(flow_name or "?")}
        payload.update(rec or {})
        path = d / f"{_safe(str(flow_name or 'flow'))}.changes.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 内部


def _normalize(new: dict[str, Any]) -> dict[str, Any]:
    """把 matcher.Candidate 风格 / 平铺字段折成元素条目结构。"""
    if not isinstance(new, dict):
        return {}

    out: dict[str, Any] = {}
    fp = dict(new.get("fingerprint") or {})

    for field in _TOP_FIELDS:
        if field in new and new[field]:
            out[field] = new[field]

    for field in _FP_FIELDS:
        if field not in new or new[field] is None:
            continue
        value = new[field]
        # path / siblings 允许显式清空（回滚、纠正脏数据用）
        if field in ("path", "siblings") or value:
            fp[field] = value

    # 候选里的 ref 就是可用 css
    if "css" not in out and new.get("ref"):
        out["css"] = str(new["ref"])

    # 候选里的 "_visible" 只是运行时标记，不落库
    attrs = fp.get("attrs")
    if isinstance(attrs, dict):
        fp["attrs"] = {k: str(v) for k, v in attrs.items() if not str(k).startswith("_")}

    if fp:
        out["fingerprint"] = fp
    return out


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict) and v:
                out.update(_flatten(v, f"{key}."))
            else:
                out[key] = v
        return out
    return {prefix.rstrip("."): value}


def _diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    a, b = _flatten(old), _flatten(new)
    out: dict[str, Any] = {}
    for key in sorted(set(a) | set(b)):
        if key.startswith(("heal.", "meta.")):
            continue
        if a.get(key) != b.get(key):
            out[key] = {"old": a.get(key), "new": b.get(key)}
    return out


def _save(store: ElementStore) -> bool:
    if not store.path:
        return False
    try:
        store.save()
        return True
    except Exception:  # noqa: BLE001 - 内存已改好，落盘失败只影响持久化
        return False


def _old_target(old: dict[str, Any]) -> str:
    return str(old.get("css") or old.get("role") or old.get("name") or "")


def _safe(name: str) -> str:
    return _UNSAFE.sub("_", str(name or "")) or "x"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
