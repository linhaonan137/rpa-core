"""元素拾取节点：把 elements/capture.py 的能力暴露给流程和 MCP。

用法（在流程里）：
    {"type": "web_pick", "args": {"selector": "#submit", "key": "submit_order"}}

拾取结果写进 elements.json（RPA_ELEMENTS 可改路径），后续节点用 `@el:submit_order` 引用。
"""

from __future__ import annotations

import os
from typing import Any

from elements.capture import capture
from elements.store import ElementError, ElementStore
from engine.context import ExecutionContext
from engine.paths import ELEMENTS_PATH
from engine.registry import Result, register

def _ambiguous(matched: int, key: str, desc: str) -> Result:
    """歧义拾取：拒绝落库，而不是静默取第 1 个。"""
    return Result.fail(
        f"web_pick: 选择器匹配 {matched} 个，拒绝拾取（会拾错）。"
        f"请加限定：label=字段名 / 容器 :has() / nth= / 或显式 allow_first=true",
        data={"key": key, "matched": matched, "ambiguous": True, "selector": desc},
    )


def _flat(entry: Any) -> dict[str, Any]:
    """ElementEntry -> resolve_fingerprint 要的扁平结构（与 web._library_entry 同口径）。"""
    d = entry.to_dict()
    fp = d.get("fingerprint") or {}
    return {
        "role": d.get("role"), "name": d.get("name"), "css": d.get("css"),
        "attrs": fp.get("attrs") or {}, "path": fp.get("path") or [],
        "siblings": fp.get("siblings") or [], "geometry": fp.get("geometry") or {},
        "tag": fp.get("tag") or "", "frame": fp.get("frame") or [],
        "label": (d.get("hints") or {}).get("label") or "",
        "hints": d.get("hints") or {},
    }


async def _verify(page: Any, ctx: ExecutionContext, entry: Any, key: str) -> tuple[bool, dict[str, Any]]:
    """拾取后立刻回读：库里这条现在能不能唯一定位到。"""
    from elements.resolve import resolve_fingerprint
    from nodes.web import _root

    try:
        loc, diag = await resolve_fingerprint(
            _root(page, ctx), _flat(entry), key=key, limit_ms=3000
        )
        if loc is None:
            return False, {"layer": None, "tried": len(diag.get("tried") or [])}
        try:
            n = await loc.count()
        except Exception:  # noqa: BLE001
            n = 1
        return True, {"layer": diag.get("layer"), "candidates": n,
                      "tried": len(diag.get("tried") or [])}
    except Exception as exc:  # noqa: BLE001
        return False, {"error": f"{type(exc).__name__}"}


def _store_path(args: dict[str, Any]) -> str:
    return str(args.get("store") or os.getenv("RPA_ELEMENTS") or ELEMENTS_PATH)


@register("web_pick")
async def web_pick(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {selector?, target?, role?, name?, exact?, key, store?, frame_chain?, limit_ms?}

    selector：任意 Playwright 选择器（css / text= / xpath=）
    target：  走统一定位入口的写法（`@el:名字` / 纯 css）
    role + name：**按可访问性语义拾取，最准**。exact 默认 True ——
              Playwright 的 `role=..[name=..]` 选择器只支持子串匹配，
              会撞到「发布商品时没有所需类目怎么办？」这类长文本隐藏元素。
    key：     元素在库里的名字，之后用 @el:<key> 引用
    """
    if ctx.page is None:
        return Result.fail("web_pick: 没有打开的页面")
    selector = args.get("selector")
    target = args.get("target")
    role = (args.get("role") or "").strip()
    name = (args.get("name") or "").strip()
    exact = bool(args.get("exact", True))
    key = args.get("key")
    allow_first = bool(args.get("allow_first", False))
    anchor_label = str(args.get("anchor_label") or "").strip()
    desc = str(selector or target or anchor_label and f"anchor_label={anchor_label}"
               or (f"role={role}[name={name}]" if role else ""))
    if not selector and not target and not anchor_label and not (role and name):
        return Result.fail("web_pick: 缺 selector / target / role+name")
    if not key:
        return Result.fail("web_pick: 缺 key")

    path = _store_path(args)
    store = ElementStore(path)
    matched = 0
    try:
        if anchor_label:
            # 同构表单字段：先按精确文本找 label，再往上找含 input 的最近容器。
            # 单个 CSS 做不到（:has-text 是子串、会匹配祖先，实测 14 个候选）。
            lab = ctx.page.get_by_text(anchor_label, exact=True)
            if await lab.count() == 0:
                return Result.fail(f"web_pick: 找不到字段名 {anchor_label!r}")
            box = lab.locator("xpath=ancestor::*[.//input][1]")
            loc = box.locator("input")
            matched = await loc.count()
            if matched == 0:
                return Result.fail(f"web_pick: 字段 {anchor_label!r} 的容器里没有 input")
            if matched > 1 and not allow_first:
                return _ambiguous(matched, str(key), desc)
            entry = await capture(
                ctx.page, None, str(key), store=store, save=False,
                frame_chain=args.get("frame_chain"),
                limit_ms=int(args.get("limit_ms", 10_000)),
                locator=loc.first,
            )
        elif role and name:
            # 精确优先（避免子串撞长文本），没有再放宽
            loc = ctx.page.get_by_role(role, name=name, exact=exact)
            if exact and await loc.count() == 0:
                loc = ctx.page.get_by_role(role, name=name, exact=False)
            matched = await loc.count()
            if matched == 0:
                return Result.fail(f"web_pick: 没有 {role}[name={name!r}] 的元素")
            if matched > 1 and not allow_first:
                return _ambiguous(matched, str(key), desc)
            entry = await capture(
                ctx.page, None, str(key), store=store, save=False,
                frame_chain=args.get("frame_chain"),
                limit_ms=int(args.get("limit_ms", 10_000)),
                locator=loc.first,
            )
        elif target:
            from nodes.web import _locate

            loc = await _locate(
                ctx.page, str(target), args.get("hints"), ctx,
                int(args.get("limit_ms", 10_000)),
            )
            first = getattr(loc, "first", loc)
            try:
                matched = await loc.count()
            except Exception:  # noqa: BLE001
                matched = 0
            if matched > 1 and not allow_first:
                return _ambiguous(matched, str(key), desc)
            entry = await capture(
                ctx.page,
                None,
                str(key),
                store=store,
                save=False,
                frame_chain=args.get("frame_chain"),
                limit_ms=int(args.get("limit_ms", 10_000)),
                locator=first,
            )
        else:
            try:
                matched = await ctx.page.locator(str(selector)).count()
            except Exception:  # noqa: BLE001
                matched = 0
            if matched > 1 and not allow_first:
                return _ambiguous(matched, str(key), desc)
            entry = await capture(
                ctx.page,
                str(selector),
                str(key),
                store=store,
                save=False,
                frame_chain=args.get("frame_chain"),
                limit_ms=int(args.get("limit_ms", 10_000)),
            )
    except ElementError as exc:
        return Result.fail(f"web_pick: {exc}")
    except Exception as exc:  # noqa: BLE001 - 抓取失败统一转结果
        return Result.fail(f"web_pick: {type(exc).__name__}: {exc}")

    # 交互配方（open/choose/close）随元素一起存，供 web_el_action 读取
    interaction = args.get("interaction")
    if isinstance(interaction, dict) and interaction:
        entry.hints["interaction"] = dict(interaction)

    # 拾取后回读校验：定位不到 / 命中多个就不许入库（止住脏条目）
    ok, quality = await _verify(ctx.page, ctx, entry, str(key))
    if not ok:
        return Result.fail(
            f"web_pick: 回读校验失败 {quality} —— 这条指纹现在定位不到，未写入元素库",
            data={"key": key, "quality": quality, "verification_failed": True},
        )

    import time as _time

    entry.meta["quality"] = {**quality, "verified_at": _time.strftime("%Y-%m-%dT%H:%M:%S")}
    store.add(entry, overwrite=True)
    store.save()

    where = f"{entry.role or '?'}/{entry.name or '?'}"
    # 同时写进内存，同一进程里后续节点立刻能用 @el:<key>
    if isinstance(ctx.elements, dict):
        # 存**全量**（to_dict 带六维指纹），定位器才能用上 attrs/path/siblings/geometry
        ctx.elements[str(key)] = entry.to_dict()
    hint = "" if matched <= 1 else "；⚠ 显式 allow_first=true，已取第 1 个"
    return Result.done(
        f"已拾取 @el:{key} → {where}（校验 {quality.get('layer')}，候选 {quality.get('candidates')}）{hint}（存 {path}）",
        data={"key": key, "role": entry.role, "name": entry.name,
              "matched": matched, "store": path, "quality": quality,
              "anchor": (entry.hints or {}).get("anchor")},
    )


@register("web_pick_list")
async def web_pick_list(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {store?} —— 列出元素库内容（调试用）"""
    path = _store_path(args)
    store = ElementStore(path)
    items = [
        {
            "key": e.key,
            "role": e.role,
            "name": e.name,
            "hits": e.hits,
            "misses": e.misses,
            "stale": e.is_stale(),
        }
        for e in store.all()
    ]
    return Result.done(f"元素库 {len(items)} 条（{path}）", data=items)
