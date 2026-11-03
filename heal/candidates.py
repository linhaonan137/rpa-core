"""候选收集与三道门决策：把页面扫出来的东西折成 matcher.Candidate。

分工：
  · `collect` 只负责"页面 → Candidate 列表"，自己不含规则；扫页面复用
    engine.middleware.candidates（失败路径上不抛，已带相似度排序）
  · `pick` 只负责"Candidate + 元素库档案 → Decision"，打分复用
    elements.matcher.match（三道门，第三道门是试跑）

两条 false-heal 防护：
  1. 同名元素里**优先可见的**：不可见的同名节点（移动端副本、隐藏模板）
     是"两个都像"歧义的头号来源。
  2. 分数全丢时按数量保守处理，绝不"随便挑一个"。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Sequence

from engine import middleware

from elements.matcher import Candidate, Decision, match, score
from elements.store import ElementEntry

#: 一次扫页面取几个候选
DEFAULT_LIMIT = 5

_TAG_RE = re.compile(r"^([a-zA-Z][\w-]*)")
_ID_RE = re.compile(r"#([\w-]+)")
_TESTID_RE = re.compile(r"\[data-testid=[\"']?([^\"'\]]+)")


async def collect(
    page: Any,
    entry: ElementEntry | None = None,
    limit_ms: int = 5000,
) -> list[Candidate]:
    """扫页面，返回按相似度排序的 matcher.Candidate 列表。

    page 为 None / 已关闭 / 超时 → 返回 []（不抛）。
    有可见候选时丢弃不可见候选，避免歧义。
    """
    if page is None:
        return []

    hints = _hints_of(entry)
    try:
        coro = middleware.candidates(page, hints, limit=DEFAULT_LIMIT)
        if limit_ms and limit_ms > 0:
            rows = await asyncio.wait_for(coro, timeout=limit_ms / 1000)
        else:
            rows = await coro
    except asyncio.TimeoutError:
        return []
    except Exception:  # noqa: BLE001 - 失败路径上再抛会盖住原始错误
        return []

    if not isinstance(rows, (list, tuple)):
        return []

    dicts = [r for r in rows if isinstance(r, dict)]
    visible = [r for r in dicts if r.get("visible", True)]
    chosen = visible or dicts  # 全不可见时至少留现场，别返回空

    out: list[Candidate] = []
    seen: set[str] = set()
    for row in chosen:
        cand = to_candidate(row)
        key = cand.ref or f"{cand.role}|{cand.name}|{cand.tag}"
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


def pick(
    fc: Any,
    entry: ElementEntry | None,
    verify: Any = None,
) -> Decision:
    """用 elements.matcher.match 走三道门。

    verify 必须是**同步**回调（matcher 直接调用它）；异步试跑请留给
    agent 的"断点续跑"验证，或在调用方自行 await 后再传入结论。
    任何时候都返回 Decision，不抛。
    """
    if entry is None:
        return Decision("report", "没有元素库条目，无法比对候选")

    raw = list(getattr(fc, "candidates", None) or [])
    candidates = [to_candidate(c) for c in raw]
    if not candidates:
        return Decision("report", "没有候选")

    fp = entry.fingerprint
    return match(candidates, fp, role=entry.role, name=entry.name, verify=verify)


def to_candidate(row: Any) -> Candidate:
    """把 middleware.candidates 的一行（或已转好的 Candidate）转成 Candidate。"""
    if isinstance(row, Candidate):
        return row

    if not isinstance(row, dict):
        return Candidate(tag="", ref=str(row))

    css = str(row.get("css") or row.get("selector") or "")
    role = row.get("role")
    name = row.get("name")

    cand = Candidate(
        role=str(role) if role else None,
        name=str(name) if name else None,
        tag=_tag_of(css, row.get("tag")),
        attrs=_attrs_of(css, row.get("attrs")),
        path=_path_of(css, row.get("path")),
        siblings=[str(s) for s in (row.get("siblings") or [])],
        geometry=_geometry_of(row.get("geometry")),
        ref=str(row.get("ref") or css),
    )
    # 可见性不参与打分，但留着给调用方判断（_stable_attrs 会把它过滤掉）
    cand.attrs.setdefault("_visible", "1" if row.get("visible", True) else "0")
    return cand


# ---------------------------------------------------------------- 解析 css


def _tag_of(css: str, explicit: Any = None) -> str:
    if explicit:
        return str(explicit).lower()
    last = css.split(">")[-1].strip() if css else ""
    m = _TAG_RE.match(last)
    return m.group(1).lower() if m else ""


def _attrs_of(css: str, explicit: Any = None) -> dict[str, str]:
    if isinstance(explicit, dict):
        return {str(k): str(v) for k, v in explicit.items()}
    attrs: dict[str, str] = {}
    head = css.split(">")[0] if css else css
    mid = _ID_RE.search(head)
    if mid:
        attrs["id"] = mid.group(1)
    testid = _TESTID_RE.search(css or "")
    if testid:
        attrs["data-testid"] = testid.group(1)
    return attrs


def _path_of(css: str, explicit: Any = None) -> list[str]:
    if isinstance(explicit, (list, tuple)):
        return [str(p) for p in explicit]
    if not css:
        return []
    return [seg.strip() for seg in css.split(">") if seg.strip()]


def _geometry_of(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k in ("x", "y", "w", "h"):
        try:
            out[k] = int(raw.get(k, 0) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


def _hints_of(entry: ElementEntry | None) -> dict[str, Any]:
    """元素库档案 → middleware.candidates 认识的 hints 形状。"""
    if entry is None:
        return {}

    hints: dict[str, Any] = {}
    if entry.role:
        hints["role"] = entry.role
    name = entry.name or entry.fingerprint.attrs.get("_text")
    if name:
        hints["name"] = name
    css = entry.css
    if not css and entry.fingerprint.path:
        css = entry.fingerprint.path[-1]  # 只有父链时用末段标签兜底
    if css:
        hints["css"] = css
    for key, value in (entry.hints or {}).items():
        if value and key in ("text", "ariaLabel", "aria_label", "placeholder", "title", "testid"):
            hints.setdefault(key, value)
    return hints


def candidates_as_dicts(
    cands: Sequence[Candidate], fp: Any = None
) -> list[dict[str, Any]]:
    """Candidate → 可写 JSON 的 dict（FailureContext.candidates 要能落盘）。

    给 fp 时顺手补上打分——**分数必须来自 elements.matcher**，这样
    rules.classify 的阈值（0.75 / 0.10）和 match() 的三道门是同一套口径，
    不会出现"判层说唯一高分、pick 又说歧义"的自相矛盾。
    """
    out: list[dict[str, Any]] = []
    for c in cands:
        item = {
            "role": c.role,
            "name": c.name,
            "tag": c.tag,
            "attrs": dict(c.attrs),
            "path": list(c.path),
            "siblings": list(c.siblings),
            "geometry": dict(c.geometry),
            "css": c.ref,
            "visible": c.attrs.get("_visible", "1") == "1",
        }
        if fp is not None:
            try:
                item["score"] = score(c, fp).score
            except Exception:  # noqa: BLE001 - 打分失败就别给分数，classify 会走保守分支
                pass
        out.append(item)
    return out
