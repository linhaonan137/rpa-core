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
# 浮层：Fusion / ARIA 两种口径，够覆盖淘宝 + 常见组件库
OVERLAY_SEL = ".next-overlay-wrapper.opened, [role=dialog], [role=listbox]"


async def _count(page: Any, sel: str) -> int:
    try:
        return await page.locator(sel).count()
    except Exception:  # noqa: BLE001 - 假页面 / 选择器不支持时按 0 处理
        return 0


def _record_observation(key: str, observed: dict[str, Any],
                        close_method: str | None, auto_choose: bool) -> None:
    """把本次实际观测写回元素库；两次一致自动从 default 升为 measured。"""
    if not key:
        return
    try:
        import os

        from elements.store import ElementStore
        from engine.paths import ELEMENTS_PATH

        st = ElementStore(os.getenv("RPA_ELEMENTS") or str(ELEMENTS_PATH))
        if not st.has(key):
            return
        e = st.get(key)
        it = dict((e.hints or {}).get("interaction") or {})
        it["observed"] = observed
        if close_method:
            it["close"] = close_method
        if auto_choose and not it.get("choose"):
            it["choose"] = "first-option"
        it["observations"] = int(it.get("observations") or 0) + 1
        if it["observations"] >= 2 and it.get("source") == "default":
            it["source"] = "measured"
            it["note"] = (str(it.get("note") or "") + " [自动观测 2 次一致，升级为实测]").strip()
        e.hints["interaction"] = it
        e.meta["interaction_source"] = it.get("source", "")
        e.meta["updated_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
        st.add(e, overwrite=True)
        st.save()   # store 内部是合并写，不会覆盖别的进程新拾的元素
    except Exception:  # noqa: BLE001 - 观测回写失败不该让业务动作失败
        pass


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

    # 有些控件需要 iframe 进出（主图素材选择器），通用节点做不了，明确拒绝而不是乱点
    if inter.get("requires") == "iframe_in_out":
        return Result.fail(
            f"web_el_action: @el:{key} 需要 iframe 进出，请用主图专用流程节点；"
            f"配方仅作记录：{inter.get('note', '')}"
        )
    if inter.get("readonly"):
        return Result.done("只读元素，未操作", data={"steps": [], "key": key, "readonly": True})

    limit = int(args.get("limit_ms", 8000))
    done: list[str] = []

    # 非实测配方 = 默认兜底策略：运行时自己观测浮层，按实际结果决定动作。
    # 不要求人去确认那 12 条 —— 跑一次自动记一次，两次一致就升级为实测。
    auto = bool(inter.get("auto", inter.get("source") != "measured"))
    opt_sel = str(inter.get("option_selector") or DEFAULT_OPTION)
    observed: dict[str, Any] = {}
    ov0 = await _count(page, OVERLAY_SEL) if auto else 0
    if auto:
        observed["overlay_before"] = ov0

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

    # ② 输入值（React 受控框必须走真实键盘，不能用 fill）
    text = inter.get("text")
    if text is not None and str(text) != "":
        await page.keyboard.type(str(text), delay=int(inter.get("delay_ms") or 60))
        done.append("type")

    # ③ 选项（auto 模式下：没写 choose 但检测到候选项，就自动选第一个）
    choose = inter.get("choose")
    auto_choose = False
    if auto:
        ov1 = await _count(page, OVERLAY_SEL)
        opts_n = await _count(page, opt_sel)
        observed.update({"overlay_after_open": ov1, "options": opts_n})
        if opts_n > 0 and not choose:
            choose, auto_choose = "first-option", True
            observed["auto_choose"] = True
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

    # ④ 关闭（auto 模式下：默认配方没写 close 却弹了浮层，就自动试三种关法）
    close = str(inter.get("close") or "none")
    close_method: str | None = None
    if auto and ov0 < observed.get("overlay_after_open", 0) and close == "none":
        close = "auto"
    if close == "auto":
        for name in (["click-blank"] if inter.get("close_target") else []) + ["escape", "click-finish"]:
            try:
                if name == "click-blank":
                    await page.locator(str(inter["close_target"])).first.click(timeout=2000)
                elif name == "escape":
                    await page.keyboard.press("Escape")
                else:
                    btn = page.get_by_text("完成", exact=True).first
                    if await btn.count() == 0:
                        continue
                    await btn.click(timeout=2000)
                await asyncio.sleep(0.15)
                if await _count(page, OVERLAY_SEL) <= ov0:
                    close_method = name
                    done.append("auto-close:" + name)
                    break
            except Exception:  # noqa: BLE001
                continue
        if not close_method:
            done.append("auto-close:failed")
        observed["close_method"] = close_method or "failed"
    elif close == "click-blank":
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

    if auto:
        observed["overlay_after_close"] = await _count(page, OVERLAY_SEL)
        _record_observation(key, observed, close_method, auto_choose)

    # ⑤ 收尾按键（例：总库存是 disabled 自动汇总，Tab 失焦才更新）
    after = str(inter.get("after") or "").strip()
    if after:
        await page.keyboard.press(after)
        done.append("after:" + after)

    return Result.done(
        f"交互完成: {' → '.join(done)}",
        data={"steps": done, "key": key, "observed": observed or None},
    )
