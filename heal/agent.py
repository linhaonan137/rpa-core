"""自愈编排：跑一次 → 分层 → 修补 → 断点续跑验证 → 保留或回滚。

五层落位（PLAN 6.2）：
  L0  引擎自己做（三级定位 / reload / 拟人重试），本模块不管；
      只在 rules.retry_with_reload 里提供"重试前 reload"的参数工具
  L1  改元素库（本模块主力，**不调 LLM**）
  L1.5 只诊断，出结论后回 L1 或上 L2
  L2  改流程节点（要 LLM，且必须在 L1/L1.5 之后）
  L3  报人

三条不可让步的约束：
  1. 改元素库 = 改前快照 + 断点续跑成功才算提交，失败立刻整体回滚
  2. L1 路径上没有 llm 变量可用（签名里不传，代码里也不 import）
  3. 修不动必须返回 action="report" 且 reason 说清为什么，不许静默

所有失败都折成 HealResult，不往外抛（除非编程错误，也会兜成 report + detail.error）。
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from engine import middleware
from engine.runner import RunOptions, run_flow

from elements.matcher import Candidate, explain
from elements.store import ElementEntry, ElementStore

from .candidates import candidates_as_dicts, collect, pick
from .context import FailureContext, build_failure
from .patch import (
    META_DIR,
    SNAP_DIR,
    apply_to_element,
    log_change,
    restore_element,
    snapshot_element,
)
from .rules import classify, diagnose

#: L2 的 LLM 回调：callable(prompt) -> str（也接受 async 版本）
LLM = Callable[[str], Any]


@dataclass
class HealResult:
    """一轮自愈的结论。

    action 取值：
      · "retry"         没改任何东西，重跑就好了（L0）
      · "patch_element" 改了元素库，且续跑验证通过（L1）
      · "patch_flow"    改了流程节点参数（L2；目前只改内存，落盘由调用方决定）
      · "report"        修不动，报人
    """

    ok: bool
    layer: str
    action: str
    reason: str
    retried: bool = False
    patched: bool = False
    rounds: int = 0
    node_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 主入口


async def heal_once(
    flow: Any,
    flow_name: str,
    ctx: Any,
    *,
    element_store: ElementStore | None = None,
    page: Any = None,
    llm: LLM | None = None,
    verify: Callable[[Candidate], Any] | None = None,
    max_rounds: int = 2,
    meta_dir: str | Path | None = None,
    snap_dir: str | Path | None = None,
    snapshot_dir: str | None = None,
) -> HealResult:
    """完整一轮自愈：① 跑一次 ② 判层 ③④⑤⑥ 按层处理，每层都写 changes.jsonl。

    snapshot_dir 给了才落现场快照（不给就不写盘，测试/无浏览器场景友好）。
    """
    rounds_limit = max(1, int(max_rounds or 1))
    name = str(flow_name or getattr(flow, "name", "") or "?")
    meta = str(Path(meta_dir)) if meta_dir is not None else META_DIR
    snap = str(Path(snap_dir)) if snap_dir is not None else SNAP_DIR

    try:
        return await _heal(
            flow, name, ctx,
            element_store=element_store, page=page, llm=llm, verify=verify,
            max_rounds=rounds_limit, meta_dir=meta, snap_dir=snap,
            snapshot_dir=snapshot_dir,
        )
    except Exception as exc:  # noqa: BLE001 - 自愈自身出错也要给结构化结论
        return HealResult(
            ok=False, layer="L3", action="report",
            reason=f"自愈内部错误：{type(exc).__name__}: {exc}",
            detail={"error": f"{type(exc).__name__}: {exc}"},
        )


async def _heal(
    flow: Any,
    flow_name: str,
    ctx: Any,
    *,
    element_store: ElementStore | None,
    page: Any,
    llm: LLM | None,
    verify: Callable[[Candidate], Any] | None,
    max_rounds: int,
    meta_dir: str,
    snap_dir: str,
    snapshot_dir: str | None,
) -> HealResult:
    first = await _run(flow, ctx, None)
    if first.get("ok"):
        return HealResult(True, "L0", "retry", "首次执行成功，无需自愈", rounds=0)

    fc = build_failure(first, flow, ctx, page)
    if fc is None:
        return HealResult(
            False, "L3", "report",
            "执行失败但找不到失败节点（可能被 on_fail=skip 容忍或日志缺失）",
            detail={"failed": list(first.get("failed") or [])},
        )

    if snapshot_dir and page is not None:
        fc.snapshot_path = await _snapshot(ctx, page, fc, snapshot_dir)
    await _refresh_candidates(fc, page, element_store)

    tried: set[str] = set()
    rounds = 0
    last_layer = "L1"
    last_reason = "无法自愈"

    while rounds < max_rounds:
        rounds += 1
        layer = classify(fc)
        entry = _entry_of(element_store, fc.element_key)

        if layer == "L1.5":
            layer, why = diagnose(fc, pick(fc, entry))
            last_reason = why

        # L1 需要元素库条目；没有就不硬修
        if layer in ("L1", "L1.5") and entry is None:
            last_reason = "失败节点没有可用的元素库条目（L1 不适用）"
            layer = "L2"

        if layer == "L1" and "L1" in tried:
            layer = "L2"

        last_layer = layer
        fc.layer = layer

        if layer == "L3":
            reason = (
                "页面是登录页/验证页或会话已失效，改选择器/改流程都无效，必须人工处理"
                if _auth_reason(fc)
                else "自愈判断该失败属于业务逻辑变化，只能报人"
            )
            _log(flow_name, fc, layer="L3", action="report", reason=reason, meta_dir=meta_dir)
            return HealResult(
                False, "L3", "report", reason, rounds=rounds, node_id=fc.node_id,
                detail={"error": fc.error, "url": fc.url},
            )

        if layer == "L1":
            tried.add("L1")
            out = await _attempt_l1(
                flow, flow_name, ctx, fc, element_store, page, verify, meta_dir, snap_dir
            )
            last_reason = out["reason"]
            if out["ok"]:
                return HealResult(
                    True, "L1", "patch_element", out["reason"],
                    retried=True, patched=True, rounds=rounds, node_id=fc.node_id,
                    detail=out.get("detail", {}),
                )
            fc = _next_fc(out, flow, ctx, page) or fc
            await _refresh_candidates(fc, page, element_store)
            continue

        # ---------------- L2 ----------------
        reason = (
            "L2 需要 LLM 提供新定位，但本次没有可用的 llm 回调"
            if llm is None
            else "L2 已经尝试过，仍失败"
        )
        if llm is None or "L2" in tried:
            full = f"{reason}；上一轮：{last_reason}"
            if rounds >= max_rounds:
                full = f"达到最大轮次 {max_rounds}，仍无法修复：{last_reason}"
            _log(flow_name, fc, layer="L2", action="report", reason=full, meta_dir=meta_dir)
            return HealResult(
                False, "L2", "report", full, rounds=rounds, node_id=fc.node_id,
                detail={"error": fc.error, "protocol": _protocol(fc)},
            )

        tried.add("L2")
        out = await _attempt_l2(flow, flow_name, ctx, fc, llm, meta_dir)
        last_reason = out["reason"]
        if out["ok"]:
            return HealResult(
                True, "L2", "patch_flow", out["reason"],
                retried=True, patched=True, rounds=rounds, node_id=fc.node_id,
                detail=out.get("detail", {}),
            )
        fc = _next_fc(out, flow, ctx, page) or fc
        await _refresh_candidates(fc, page, element_store)

    reason = f"达到最大轮次 {max_rounds}，仍无法修复：{last_reason}"
    _log(flow_name, fc, layer=last_layer, action="report", reason=reason, meta_dir=meta_dir)
    return HealResult(
        False, last_layer, "report", reason, rounds=rounds, node_id=fc.node_id,
        detail={"error": fc.error, "protocol": _protocol(fc)},
    )


# ---------------------------------------------------------------- L1


async def _attempt_l1(
    flow: Any,
    flow_name: str,
    ctx: Any,
    fc: FailureContext,
    store: ElementStore | None,
    page: Any,
    verify: Callable[[Candidate], Any] | None,
    meta_dir: str,
    snap_dir: str,
) -> dict[str, Any]:
    """改元素库 → 从失败节点续跑 → 成功保留 / 失败回滚。"""
    key = fc.element_key
    entry = _entry_of(store, key)
    if entry is None or key is None:
        return {"ok": False, "reason": "没有元素库条目，L1 无从下手", "resume": None}

    # matcher 的 verify 是同步调用；async 版本由这里自己 await
    sync_verify = None if (verify is not None and inspect.iscoroutinefunction(verify)) else verify
    decision = pick(fc, entry, verify=sync_verify)
    if not decision.ok:
        return {
            "ok": False,
            "reason": f"L1 候选不可信：{decision.reason}",
            "resume": None,
            "detail": {"decision": decision.reason},
        }

    assert decision.best is not None
    best = decision.best.candidate

    if verify is not None and inspect.iscoroutinefunction(verify):
        try:
            passed = bool(await verify(best))
        except Exception as exc:  # noqa: BLE001
            passed = False
            reason = f"试跑动作异常：{exc}"
        else:
            reason = "试跑动作失败"
        if not passed:
            return {"ok": False, "reason": f"{reason}（不动元素库）", "resume": None}

    reason = f"L1 候选命中 {explain(decision.best)}"
    new = _patch_payload(best, entry)
    old = entry.to_dict()

    snap = snapshot_element(store, key, snap_dir) if store is not None else ""
    applied = apply_to_element(store, key, new, reason, by="L1") if store is not None else {
        "ok": False, "error": "没有元素库"
    }
    if not applied.get("ok"):
        return {
            "ok": False,
            "reason": f"写元素库失败：{applied.get('error')}",
            "resume": None,
            "detail": {"patch": applied},
        }

    _sync_ctx(ctx, store, key)
    _log(
        flow_name, fc, layer="L1", action="patch_element", reason=reason,
        extra={"changes": applied.get("changes", {}), "snap": snap, "verified": "pending"},
        meta_dir=meta_dir,
    )

    _reset(ctx)
    trial = await _run(flow, ctx, fc.node_id)

    if trial.get("ok"):
        _log(
            flow_name, fc, layer="L1", action="patch_element", reason=reason,
            extra={"verified": "pass", "snap": snap}, meta_dir=meta_dir,
        )
        return {
            "ok": True,
            "reason": f"{reason}；续跑通过",
            "resume": trial,
            "detail": {"changes": applied.get("changes", {}), "snap": snap, "verified": "pass"},
        }

    # 回滚：整体替换回旧条目，避免补丁加的属性残留
    rolled = restore_element(store, key, old)
    _sync_ctx(ctx, store, key)
    err = _error_of(trial)
    _log(
        flow_name, fc, layer="L1", action="patch_element",
        reason=f"L1 改库后试跑失败，已回滚：{err}",
        extra={
            "verified": "fail",
            "rolled_back": bool(rolled.get("ok")),
            "snap": snap,
            "error": err,
        },
        meta_dir=meta_dir,
    )
    return {
        "ok": False,
        "reason": f"L1 改库后试跑仍失败（已回滚）：{err}",
        "resume": trial,
        "detail": {"rolled_back": bool(rolled.get("ok")), "snap": snap},
    }


# ---------------------------------------------------------------- L2


async def _attempt_l2(
    flow: Any,
    flow_name: str,
    ctx: Any,
    fc: FailureContext,
    llm: LLM,
    meta_dir: str,
) -> dict[str, Any]:
    """让 LLM 给一个新 target，改单个节点参数后整点续跑。"""
    node = _node_of(flow, fc.node_id)
    if node is None:
        return {"ok": False, "reason": f"流程里找不到节点 {fc.node_id}", "resume": None}

    new_target = await _ask_llm(llm, fc)
    if not new_target:
        return {"ok": False, "reason": "L2 没能从 LLM 得到可用的新 target", "resume": None}

    old_target = node.args.get("target")
    node.args["target"] = new_target
    _log(
        flow_name, fc, layer="L2", action="patch_flow",
        reason=f"LLM 给出新 target: {new_target}",
        extra={"field": "target", "old": old_target, "new": new_target, "verified": "pending"},
        meta_dir=meta_dir,
    )

    _reset(ctx)
    trial = await _run(flow, ctx, fc.node_id)

    if trial.get("ok"):
        _log(
            flow_name, fc, layer="L2", action="patch_flow",
            reason=f"LLM 新 target 续跑通过: {new_target}",
            extra={"field": "target", "old": old_target, "new": new_target, "verified": "pass"},
            meta_dir=meta_dir,
        )
        return {
            "ok": True,
            "reason": f"L2 改了节点 {fc.node_id}.target：{old_target} → {new_target}（仅内存，需调用方落盘）",
            "resume": trial,
            "detail": {"field": "target", "old": old_target, "new": new_target},
        }

    node.args["target"] = old_target
    err = _error_of(trial)
    _log(
        flow_name, fc, layer="L2", action="patch_flow",
        reason=f"L2 新 target 试跑失败，已改回：{err}",
        extra={
            "field": "target", "old": old_target, "new": new_target,
            "verified": "fail", "rolled_back": True, "error": err,
        },
        meta_dir=meta_dir,
    )
    return {
        "ok": False,
        "reason": f"L2 改流程后试跑仍失败（已改回）：{err}",
        "resume": trial,
        "detail": {"rolled_back": True},
    }


def build_l2_prompt(fc: FailureContext) -> str:
    """给 LLM 的最小输入：错误 + 原 target + 候选 + 节点类型。"""
    cands = [
        {"role": c.get("role"), "name": c.get("name"), "css": c.get("css")}
        for c in (fc.candidates or [])[:5]
    ]
    return (
        "一个浏览器自动化流程的节点失败了，请给出一个更稳的定位表达式。\n"
        f"节点: {fc.node_id} ({fc.node_type})\n"
        f"原参数: {json.dumps(fc.args, ensure_ascii=False)}\n"
        f"错误: {fc.error}\n"
        f"页面: {fc.url or '未知'}\n"
        f"候选: {json.dumps(cands, ensure_ascii=False)}\n"
        '只输出 JSON：{"target": "<css 或 role=xxx[name=yyy] 或 text=zzz>"}'
    )


# ---------------------------------------------------------------- 工具


async def _run(flow: Any, ctx: Any, start_at: str | None) -> dict[str, Any]:
    """跑一次；start_at 给了就从失败节点续跑（Runner 会跳过上游）。"""
    opts = RunOptions(start_at=start_at) if start_at else RunOptions()
    return await run_flow(flow, ctx, opts)


async def _ask_llm(llm: LLM, fc: FailureContext) -> str | None:
    try:
        out = llm(build_l2_prompt(fc))
        if inspect.isawaitable(out):
            out = await out
    except Exception:  # noqa: BLE001 - LLM 挂了就当没给
        return None
    return _parse_target(str(out or ""))


def _parse_target(text: str) -> str | None:
    """从 LLM 输出里抠出 target：优先 JSON，其次单行纯文本。"""
    raw = (text or "").strip()
    if not raw:
        return None

    for blob in re.findall(r"\{[^{}]*\}", raw):
        try:
            obj = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            got = obj.get("target") or obj.get("selector") or obj.get("css")
            if isinstance(got, str) and got.strip():
                return got.strip()

    line = raw.splitlines()[0].strip().strip("`\"'")
    if not line or len(line) > 300 or line.startswith(("{", "[", "```")):
        return None
    return line


def _patch_payload(best: Candidate, entry: ElementEntry) -> dict[str, Any]:
    fp = entry.fingerprint
    attrs = {k: v for k, v in fp.attrs.items() if not str(k).startswith("_")}
    attrs.update({k: v for k, v in best.attrs.items() if not str(k).startswith("_")})
    geo = {k: int(best.geometry.get(k, 0) or 0) for k in ("x", "y", "w", "h")} if best.geometry else {}
    return {
        "role": best.role or entry.role,
        "name": best.name or entry.name,
        "css": best.ref or entry.css,
        "fingerprint": {
            "tag": best.tag or fp.tag,
            "attrs": attrs,
            "path": list(best.path) or list(fp.path),
            "siblings": list(best.siblings) or list(fp.siblings),
            "geometry": geo,
        },
    }


def _entry_of(store: ElementStore | None, key: str | None) -> ElementEntry | None:
    if store is None or not key:
        return None
    try:
        return store.get(key)
    except Exception:  # noqa: BLE001 - key 不存在就是没有
        return None


def _sync_ctx(ctx: Any, store: ElementStore | None, key: str | None) -> None:
    """把元素库的新值同步进 ctx.elements，否则续跑还是用旧选择器。"""
    els = getattr(ctx, "elements", None)
    if not isinstance(els, dict) or store is None or not key:
        return
    entry = _entry_of(store, key)
    if entry is not None:
        els[key] = entry.to_dict()


def _reset(ctx: Any) -> None:
    """失败后 Runner 会把 should_stop 置真；续跑前必须清掉。"""
    for flag in ("should_stop", "should_break", "should_continue"):
        try:
            setattr(ctx, flag, False)
        except Exception:  # noqa: BLE001
            pass


def _node_of(flow: Any, node_id: str) -> Any:
    getter = getattr(flow, "node", None)
    if not callable(getter):
        return None
    try:
        return getter(node_id)
    except (KeyError, ValueError):
        return None


def _auth_reason(fc: FailureContext) -> bool:
    url = (fc.url or "").lower()
    err = (fc.error or "").lower()
    return any(t in url for t in ("login", "signin", "captcha", "verify")) or any(
        t in err for t in ("未登录", "会话", "login", "session")
    )


def _protocol(fc: FailureContext) -> dict[str, Any]:
    """写进 changes.jsonl / detail 的失败摘要（不含超长文本）。"""
    return {
        "node": fc.node_id,
        "type": fc.node_type,
        "error": (fc.error or "")[:300],
        "url": fc.url,
        "candidates": len(fc.candidates or []),
        "element": fc.element_key,
    }


def _error_of(result: dict[str, Any] | None) -> str:
    for node in reversed(((result or {}).get("nodes") or [])):
        if isinstance(node, dict) and not node.get("ok"):
            return str(node.get("error") or "未知错误")
    return "未知错误"


def _next_fc(
    out: dict[str, Any], flow: Any, ctx: Any, page: Any
) -> FailureContext | None:
    resume = out.get("resume")
    if not isinstance(resume, dict):
        return None
    return build_failure(resume, flow, ctx, page)


async def _refresh_candidates(
    fc: FailureContext, page: Any, store: ElementStore | None
) -> None:
    if page is None:
        return
    entry = _entry_of(store, fc.element_key)
    try:
        cands = await collect(page, entry)
    except Exception:  # noqa: BLE001
        return
    if cands:
        fc.candidates = candidates_as_dicts(cands, entry.fingerprint if entry else None)


async def _snapshot(ctx: Any, page: Any, fc: FailureContext, save_dir: str) -> str | None:
    try:
        info = await middleware.snapshot(ctx, page, fc.node_id, fc.error, save_dir=save_dir)
    except Exception:  # noqa: BLE001 - 快照失败不影响自愈
        return None
    return info.get("web_shot") or info.get("meta") or info.get("dir") or None


def _log(
    flow_name: str,
    fc: FailureContext,
    *,
    layer: str,
    action: str,
    reason: str,
    meta_dir: str,
    extra: dict[str, Any] | None = None,
) -> None:
    rec: dict[str, Any] = {
        "node": fc.node_id,
        "node_type": fc.node_type,
        "layer": layer,
        "action": action,
        "by": layer,
        "reason": reason,
        "url": fc.url,
        "snapshot": fc.snapshot_path,
        "element": fc.element_key,
    }
    rec.update(extra or {})
    log_change(flow_name, rec, meta_dir=meta_dir)
