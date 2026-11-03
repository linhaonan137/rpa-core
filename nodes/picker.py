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

#: 默认元素库位置（由 engine.paths 决定，不依赖 cwd）
DEFAULT_STORE = str(ELEMENTS_PATH)


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
    if not selector and not target and not (role and name):
        return Result.fail("web_pick: 缺 selector / target / role+name")
    if not key:
        return Result.fail("web_pick: 缺 key")

    path = _store_path(args)
    store = ElementStore(path)
    try:
        if role and name:
            # 精确优先（避免子串撞长文本），没有再放宽
            loc = ctx.page.get_by_role(role, name=name, exact=exact)
            if exact and await loc.count() == 0:
                loc = ctx.page.get_by_role(role, name=name, exact=False)
            if await loc.count() == 0:
                return Result.fail(f"web_pick: 没有 {role}[name={name!r}] 的元素")
            entry = await capture(
                ctx.page, None, str(key), store=store,
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
            entry = await capture(
                ctx.page,
                None,
                str(key),
                store=store,
                frame_chain=args.get("frame_chain"),
                limit_ms=int(args.get("limit_ms", 10_000)),
                locator=first,
            )
        else:
            entry = await capture(
                ctx.page,
                str(selector),
                str(key),
                store=store,
                frame_chain=args.get("frame_chain"),
                limit_ms=int(args.get("limit_ms", 10_000)),
            )
    except ElementError as exc:
        return Result.fail(f"web_pick: {exc}")
    except Exception as exc:  # noqa: BLE001 - 抓取失败统一转结果
        return Result.fail(f"web_pick: {type(exc).__name__}: {exc}")

    where = f"{entry.role or '?'}/{entry.name or '?'}"
    # 同时写进内存，同一进程里后续节点立刻能用 @el:<key>
    if isinstance(ctx.elements, dict):
        # 存**全量**（to_dict 带六维指纹），定位器才能用上 attrs/path/siblings/geometry
        ctx.elements[str(key)] = entry.to_dict()
    return Result.done(
        f"已拾取 @el:{key} → {where}（存 {path}）",
        data={"key": key, "role": entry.role, "name": entry.name, "store": path},
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
