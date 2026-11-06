"""元素交互配方节点：把「怎么点」写进数据，而不是每个字段写一堆节点。

元素库条目 hints.interaction 示例：

    "interaction": {
      "open": "click",                    # click | hover | none
      "choose": "first-option",           # first-option | {"index": n} | {"text": "..."} | null
      "option_selector": "div.options-item",
      "close": "click-blank",             # click-blank | click-finish | escape | none
      "close_target": "text=商品属性",
      "wait_ms": 450
    }

流程里只需：{"type": "web_el_action", "args": {"key": "qn_prop_fabric"}}
也可以不带 key、直接给 interaction + target（临时试规则用）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from engine.registry import Result, register

DEFAULT_OPTION = "div.options-item"


@register("web_el_action")
async def web_el_action(args: dict[str, Any], ctx: Any) -> Result:
    page = getattr(ctx, "page", None)
    if page is None:
        return Result.fail("web_el_action: 没有打开的页面")

    from nodes.web import _library_entry, _locate

    key = str(args.get("key") or "").strip().removeprefix("@el:")
    inter: dict[str, Any] = dict(args.get("interaction") or {})
    target = str(args.get("target") or "").strip()

    if key:
        entry = _library_entry(key, ctx)
        if entry is None:
            return Result.fail(f"web_el_action: 元素库里没有 @el:{key}")
        saved = (entry.get("hints") or {}).get("interaction") or {}
        inter = {**saved, **inter}          # 流程里给的覆盖库里的
        target = target or f"@el:{key}"
    if not target:
        return Result.fail("web_el_action: 需要 key 或 target")

    limit = int(args.get("limit_ms", 8000))
    done: list[str] = []

    # ① 打开
    loc = await _locate(page, target, None, ctx, limit)
    el = getattr(loc, "first", loc)
    op = str(inter.get("open") or "click")
    if op == "click":
        await el.click(timeout=limit)
        done.append("click")
    elif op == "hover":
        await el.hover(timeout=limit)
        done.append("hover")
    elif op != "none":
        return Result.fail(f"web_el_action: 未知 open={op!r}")

    wait = int(inter.get("wait_ms") or 400)
    if wait:
        await asyncio.sleep(wait / 1000)

    # ② 选项
    choose = inter.get("choose")
    opt_sel = str(inter.get("option_selector") or DEFAULT_OPTION)
    if choose:
        opts = page.locator(opt_sel)
        if isinstance(choose, dict) and choose.get("text"):
            picked = page.get_by_text(str(choose["text"]), exact=True).first
        elif isinstance(choose, dict) and "index" in choose:
            picked = opts.nth(int(choose["index"]))
        elif choose == "first-option":
            picked = opts.first
        else:
            return Result.fail(f"web_el_action: 未知 choose={choose!r}")
        await picked.click(timeout=limit)
        done.append(f"choose:{choose}")
        await asyncio.sleep(min(300, wait) / 1000)

    # ③ 关闭
    close = str(inter.get("close") or "none")
    if close == "click-blank":
        ct = str(inter.get("close_target") or "")
        if not ct:
            return Result.fail("web_el_action: close=click-blank 需要 close_target")
        await page.locator(ct).first.click(timeout=limit)
        done.append("click-blank")
    elif close == "click-finish":
        await page.get_by_text("完成", exact=True).first.click(timeout=limit)
        done.append("click-finish")
    elif close == "escape":
        await page.keyboard.press("Escape")
        done.append("escape")
    elif close != "none":
        return Result.fail(f"web_el_action: 未知 close={close!r}")

    return Result.done(f"交互完成: {' → '.join(done)}", data={"steps": done, "key": key})
