"""网页操作节点（Playwright）。

约定（全部是静默失败的防护）：
  1. 元素引用叫 `target`，不是 selector；特征提示叫 `hints`，不是 selectorHints
  2. 超时叫 `limit_ms`；存结果叫 `save_as`；行为分支叫 `kind`
  3. 任何失败都返回 Result.fail，绝不把异常抛给 Runner
  4. 没开页面就动手 → 明确报「页面未打开」，不静默通过

元素定位统一走 `_locate()`：
  target 支持 `@el:名字`（元素库）、`css=`、`xpath=`、`text=`、纯 CSS、纯文本
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from engine.context import ContextError, ExecutionContext
from engine.registry import Result, register

# ---------------------------------------------------------------- 常量

DEFAULT_LIMIT_MS = 30_000

#: web_wait 的 kind → Playwright 的 state
_STATES = {
    "visible": "visible",
    "attached": "attached",
    "exist": "attached",
    "hidden": "hidden",
    "detached": "detached",
}


# ---------------------------------------------------------------- 内部异常


class _NodeError(Exception):
    """节点内部错误，由 _guard 统一转成 Result.fail。"""


class _LocateError(_NodeError):
    """定位失败。"""


# ---------------------------------------------------------------- 通用小工具


def _guard(fn):  # type: ignore[no-untyped-def]
    """把任何异常收成 Result.fail —— 节点不许穿透异常。"""

    async def wrapper(args: dict[str, Any], ctx: ExecutionContext) -> Result:
        name = fn.__name__
        try:
            return await fn(args, ctx)
        except ContextError as exc:
            return Result.fail(f"{name}: 变量错误: {exc}")
        except _NodeError as exc:
            return Result.fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，日志里能看到原始类型
            label = type(exc).__name__
            if "timeout" in label.lower():
                return Result.fail(f"{name} 超时: {exc}")
            return Result.fail(f"{name} 失败: {label}: {exc}")

    wrapper.__name__ = fn.__name__
    wrapper.__qualname__ = fn.__qualname__
    wrapper.__doc__ = fn.__doc__
    wrapper.__module__ = fn.__module__
    return wrapper


def _text(ctx: ExecutionContext, args: dict[str, Any], key: str, default: Any = None) -> Any:
    """取一个字符串参数并做变量替换。"""
    raw = args.get(key, default)
    if raw is None:
        return default
    val = ctx.resolve(raw)
    if val is None:
        return default
    return val if isinstance(val, str) else str(val)


def _need(ctx: ExecutionContext, args: dict[str, Any], key: str, node: str) -> str:
    val = _text(ctx, args, key, "")
    if not val:
        raise _NodeError(f"{node} 缺 {key}")
    return val


def _ms(ctx: ExecutionContext, args: dict[str, Any], key: str, default: int) -> int:
    raw = args.get(key, default)
    if raw is None:
        return default
    val = ctx.resolve(raw)
    try:
        return max(0, int(float(val)))
    except (TypeError, ValueError):
        raise _NodeError(f"{key} 必须是数字，得到 {val!r}") from None


def _flag(args: dict[str, Any], key: str, default: bool = False) -> bool:
    val = args.get(key, default)
    if isinstance(val, str):
        return val.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(val)


def _save(ctx: ExecutionContext, args: dict[str, Any], value: Any) -> None:
    name = args.get("save_as")
    if name:
        ctx.set_var(str(name), value)


#: JS 里只认「像变量名」的占位符，`{a:1}` 这类对象字面量原样保留
_JS_TOKEN = re.compile(r"(?<!\\)\{([A-Za-z_\u4e00-\u9fa5][\w\u4e00-\u9fa5]*(?:\[[^\]]*\])*)\}")


def _script(ctx: ExecutionContext, args: dict[str, Any], key: str) -> str:
    """脚本类参数的变量替换：只替换 {变量}，不碰 JS 花括号语法。"""
    raw = args.get(key)
    if raw is None:
        return ""
    text = str(raw)

    def _sub(m: re.Match[str]) -> str:
        val = ctx.resolve(m.group(0))
        if isinstance(val, str):
            return val
        return "" if val is None else str(val)

    return _JS_TOKEN.sub(_sub, text)


def _page(ctx: ExecutionContext) -> Any:
    page = ctx.page
    if page is None:
        raise _NodeError("页面未打开，请先执行 web_open / web_attach")
    return page


def _safe_list(value: Any) -> list[Any]:
    try:
        return list(value)
    except TypeError:
        return []


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _root(page: Any, ctx: ExecutionContext | None) -> Any:
    """按 ctx.frames 的 iframe 链拿到定位根节点。"""
    root = page
    frames = _safe_list(getattr(ctx, "frames", None) or [])
    for sel in frames:
        root = root.frame_locator(sel)
    return root


# ---------------------------------------------------------------- 元素定位


def _from_hints(hints: Any) -> str | None:
    """把特征提示折成一个选择器表达式。"""
    if not hints:
        return None
    if isinstance(hints, str):
        return hints.strip() or None
    if isinstance(hints, (list, tuple)):
        for item in hints:
            got = _from_hints(item)
            if got:
                return got
        return None
    if isinstance(hints, dict):
        for key in ("target", "selector", "css"):
            if hints.get(key):
                return str(hints[key])
        for key in ("xpath", "text", "role", "label", "placeholder", "testid"):
            if hints.get(key):
                return f"{key}={hints[key]}"
    return None


def _bump_miss(key: str, ctx: ExecutionContext | None) -> None:
    """定位失败时给元素库记一次 miss（连续 3 次 → is_stale，提示重新拾取）。

    只记 miss 不记 hit：hit 会在循环里高频触发，每次都落盘不值得；
    miss 才是"这个元素该修了"的信号。
    """
    try:
        import os as _os

        from engine.paths import ELEMENTS_PATH

        path = _os.getenv("RPA_ELEMENTS") or str(ELEMENTS_PATH)
        from elements.store import ElementStore

        store = ElementStore(path)
        if store.has(key):
            store.get(key).miss()
            store.save()
    except Exception:  # noqa: BLE001 - 统计失败不影响主流程
        pass


def _library_entry(name: str, ctx: ExecutionContext | None) -> dict[str, Any] | None:
    """`@el:名字` → 元素库条目（dict，含**完整六维指纹**）。

    两级来源：① ctx.elements（同进程 web_pick 刚写入的）② elements.json。
    返回的 dict 直接喂给 elements.resolve.resolve_fingerprint。
    """
    import os as _os

    lib = getattr(ctx, "elements", None) or {}
    entry: Any = lib.get(name)

    if entry is not None and isinstance(entry, dict) and "fingerprint" in entry:
        # web_pick 现在存的是 to_dict() 全量
        fp = entry.get("fingerprint") or {}
        return {
            "role": entry.get("role"),
            "name": entry.get("name"),
            "css": entry.get("css"),
            "attrs": fp.get("attrs") or {},
            "path": fp.get("path") or [],
            "siblings": fp.get("siblings") or [],
            "geometry": fp.get("geometry") or {},
            "tag": fp.get("tag") or "",
            "frame": fp.get("frame") or [],
            "label": (entry.get("hints") or {}).get("label") or "",
            "hints": entry.get("hints") or {},
        }

    if entry is None:
        from engine.paths import ELEMENTS_PATH

        path = _os.getenv("RPA_ELEMENTS") or str(ELEMENTS_PATH)
        if _os.path.exists(path):
            try:
                from elements.store import ElementStore

                store = ElementStore(path)
                if store.has(name):
                    e = store.get(name)
                    return {
                        "role": e.role,
                        "name": e.name,
                        "css": e.css,
                        "attrs": e.fingerprint.attrs,
                        "path": e.fingerprint.path,
                        "siblings": e.fingerprint.siblings,
                        "geometry": {
                            "x": e.fingerprint.geometry.x,
                            "y": e.fingerprint.geometry.y,
                        },
                        "tag": e.fingerprint.tag,
                        "frame": e.fingerprint.frame,
                        "label": (e.hints or {}).get("label") or "",
                        "hints": e.hints or {},
                    }
            except Exception:  # noqa: BLE001 - 库文件坏了按"没找到"处理
                return None

    if entry is None:
        return None
    if isinstance(entry, str):
        return {"css": entry}
    if isinstance(entry, dict):
        return {
            "role": entry.get("role"),
            "name": entry.get("name"),
            "css": entry.get("css"),
            "attrs": entry.get("attrs") or {},
            "path": entry.get("path") or [],
            "siblings": entry.get("siblings") or [],
            "geometry": entry.get("geometry") or {},
            "tag": entry.get("tag") or "",
            "hints": entry.get("hints") or {},
        }
    return None


def _default_headless() -> bool:
    """全局默认：RPA_HEADLESS=false 时有头（能看见窗口，也过风控）。

    优先级：节点 args.headless > 环境变量 RPA_HEADLESS > 默认 true（服务器安全）
    """
    v = os.getenv("RPA_HEADLESS", "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def _looks_like_text(expr: str) -> bool:
    """判断一个 target 是"可见文本"还是"CSS 选择器"。

    真实站点抓到的坑：`li.next a`（后代选择器）含空格，被当成文本
    `get_by_text("li.next a")` 永远匹配不到 → 报"元素不存在"。

    判断顺序：
      1. 含 CSS 元字符（# . [ ] : > ~ + , *）→ 一定是 CSS
      2. 含中文 → 文本
      3. 含空格 → 文本（如 "Submit order"）
      4. 其余 → CSS（单个标签名如 button）
    """
    if any(ch in expr for ch in "#.[]:>*+~,"):
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in expr):
        return True
    return " " in expr


def _build(root: Any, expr: str) -> Any:
    raw = expr.strip()
    low = raw.lower()

    if low.startswith("css="):
        return root.locator(raw[4:].strip())
    if low.startswith("xpath="):
        return root.locator(raw)
    if low.startswith("text="):
        return root.locator(raw)
    if low.startswith("id="):
        return root.locator("#" + raw[3:].strip())
    if low.startswith(("//", "(//")):
        return root.locator("xpath=" + raw)
    if low.startswith(("role=", "label=", "placeholder=", "testid=")):
        return root.locator(raw)

    if _looks_like_text(raw):
        getter = getattr(root, "get_by_text", None)
        if getter is not None:
            return getter(raw)
        return root.locator(f"text={raw}")
    return root.locator(raw)


async def _locate(
    page: Any,
    target: Any = None,
    hints: Any = None,
    ctx: ExecutionContext | None = None,
    limit_ms: int = DEFAULT_LIMIT_MS,
) -> Any:
    """统一的元素定位入口。target 支持 @el: / css= / xpath= / text= / 纯 CSS / 纯文本。

    返回 locator（可能是多元素集合，动作用 .first）。
    """
    root = _root(page, ctx)
    expr: str | None = None

    if target:
        text = str(target).strip()
        if text.lower().startswith("@el:"):
            key = text[4:].strip()
            entry = _library_entry(key, ctx)
            if entry is None:
                raise _LocateError(f"元素库中没有 @el:{key}（内存与 elements.json 均未找到）")
            # 六维指纹定位：role+name → attrs → css → path → siblings → geometry
            # （以前只拼 role+name 一个选择器，其余 5 维存着没用，同名元素会静默点错）
            from elements.resolve import AmbiguousElement, resolve_fingerprint

            # 元素级 frame 链：这个元素在 iframe 里 → 从 iframe 内部开始找
            # （_root 只处理流程级 ctx.frames，元素自己的位置是存进指纹的）
            el_root = root
            for fsel in (entry.get("frame") or []):
                sel_expr = fsel.get("selector") if isinstance(fsel, dict) else str(fsel)
                if sel_expr:
                    el_root = el_root.frame_locator(sel_expr)

            try:
                loc, diag = await resolve_fingerprint(el_root, entry, key=key, limit_ms=limit_ms)
            except AmbiguousElement as exc:
                raise _LocateError(str(exc)) from exc
            if loc is None:
                _bump_miss(key, ctx)
                tried = diag.get("tried") or []
                raise _LocateError(
                    f"@el:{key} 六维定位全部失败（页面可能已变）\n"
                    f"  逐层结果: {tried}\n"
                    f"  建议：重新 web_pick，或用 css/text= 直接定位"
                )
            return loc
        expr = text

    if not expr:
        expr = _from_hints(hints)

    if not expr:
        raise _LocateError("缺少元素引用（target 或 hints）")

    try:
        return _build(root, expr)
    except _LocateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _LocateError(f"选择器无效: {expr} ({exc})") from exc


async def _pick(loc: Any, label: str, limit_ms: int = DEFAULT_LIMIT_MS) -> Any:
    """确认存在并返回首元素。

    两个真实站点抓到的坑：
      ① SPA 慢渲染时 DOM 里还没有元素 → 必须 wait_for(attached) 而不是立即 count
      ② **多个元素匹配时 wait_for 会抛 strict mode violation**
         （.btn_inventory 匹配 6 个 → 报"元素不存在"）
         → 先取 .first 再等，严格模式就不会触发
    """
    target = getattr(loc, "first", loc)
    try:
        await target.wait_for(state="attached", timeout=limit_ms)
    except Exception as exc:  # noqa: BLE001 - 超时/页面关闭统一转定位失败
        raise _LocateError(f"元素不存在或等待超时: {label}（{limit_ms}ms）") from exc
    try:
        count = await loc.count()
    except Exception as exc:  # noqa: BLE001
        raise _LocateError(f"定位失败: {label} ({exc})") from exc
    if not count:
        raise _LocateError(f"元素不存在: {label}")
    return target


# ---------------------------------------------------------------- 浏览器/页面


async def _launch(headless: bool = True) -> tuple[Any, Any]:
    """启动 Playwright 与浏览器。返回 (playwright, browser)。测试可替换。

    优先用系统 Chrome（真浏览器，反检测更友好），失败降级到内置 chromium；
    启动参数带反检测标志（见 engine/stealth.py 的 X14）。
    """
    from playwright.async_api import async_playwright

    from engine import stealth

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(**stealth.launch_kwargs(headless, channel="chrome"))
    except Exception:  # noqa: BLE001 - 没有系统 Chrome 就用内置
        browser = await pw.chromium.launch(**stealth.launch_kwargs(headless, channel=None))
    return pw, browser


async def _ensure_page(
    ctx: ExecutionContext, *, new_tab: bool = False, headless: bool | None = None
) -> Any:
    """拿到可用页面：已有就复用，没有就新开一个（必要时起浏览器）。"""
    if headless is None:
        headless = _default_headless()
    page = ctx.page

    if page is not None and not new_tab:
        return page

    browser = getattr(ctx, "browser", None)
    if browser is None:
        pw, browser = await _launch(headless=headless)
        setattr(ctx, "_playwright", pw)
        setattr(ctx, "browser", browser)

    context = getattr(ctx, "browser_context", None)
    if context is None:
        contexts = _safe_list(getattr(browser, "contexts", None) or [])
        context = contexts[0] if contexts else await browser.new_context()
        setattr(ctx, "browser_context", context)
        try:
            from engine import stealth
            await stealth.apply(context, ctx)  # 接管指纹浏览器时自动跳过
        except Exception:  # noqa: BLE001 - 注入失败不中断
            pass

    if page is not None:
        page = await context.new_page()
    else:
        pages = _safe_list(getattr(context, "pages", None) or [])
        page = pages[0] if pages else await context.new_page()

    ctx.page = page
    return page


def _all_pages(ctx: ExecutionContext) -> list[Any]:
    """当前页 + 浏览器里所有页，去重保序。"""
    out: list[Any] = []
    seen: set[int] = set()

    def _add(p: Any) -> None:
        if p is None or id(p) in seen:
            return
        seen.add(id(p))
        out.append(p)

    _add(ctx.page)
    browser = getattr(ctx, "browser", None)
    if browser is not None:
        for context in _safe_list(getattr(browser, "contexts", None) or []):
            for p in _safe_list(getattr(context, "pages", None) or []):
                _add(p)
    # 当前页所属的 context 也要看，否则新开的标签页永远发现不了
    for context in (
        getattr(ctx, "browser_context", None),
        getattr(ctx.page, "context", None) if ctx.page is not None else None,
    ):
        if context is None:
            continue
        for p in _safe_list(getattr(context, "pages", None) or []):
            _add(p)
    return out


async def _title_of(page: Any) -> str:
    try:
        title = await _maybe_await(page.title())
        return title or ""
    except Exception:  # noqa: BLE001 - 取标题失败不该中断切页
        return ""


def _match(value: str, pattern: str, mode: str) -> bool:
    if not pattern:
        return True
    if mode == "exact":
        return value == pattern
    if mode == "regex":
        import re

        try:
            return re.search(pattern, value) is not None
        except re.error:
            return False
    return pattern in value


# ---------------------------------------------------------------- 路径


def _out_path(ctx: ExecutionContext, args: dict[str, Any], default_name: str) -> str:
    """path 当目录用（自动建），带后缀则当完整文件路径。默认落在 out/。

    **相对路径基于项目根，不跟随 cwd**（MCP 场景实测：截图落到了客户端工作目录）。
    """
    from engine.paths import OUT_DIR, resolve_path

    raw = _text(ctx, args, "path", "") or str(OUT_DIR)
    p = resolve_path(raw)
    if p.suffix:
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    p.mkdir(parents=True, exist_ok=True)
    name = _text(ctx, args, "name", "") or default_name
    return str(p / name)


def _stamp_name(prefix: str = "shot", suffix: str = "png") -> str:
    return f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.{suffix}"


# ================================================================ 页面

@register("web_open")
@_guard
async def web_open(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {url, wait_until=load, kind=current_tab|new_tab, limit_ms}"""
    url = _need(ctx, args, "url", "web_open")
    kind = args.get("kind", "current_tab")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    # until 别名转换（AGENT-PLAN 约定 load/dom/idle；Playwright 用全名）
    wu = args.get("wait_until") or args.get("until") or "load"
    wu = {"dom": "domcontentloaded", "idle": "networkidle", "none": "commit"}.get(wu, wu)
    page = await _ensure_page(
        ctx, new_tab=(kind == "new_tab"), headless=_flag(args, "headless", _default_headless())
    )
    await page.goto(url, wait_until=wu, timeout=limit)
    return Result.done(f"已打开 {url}", data={"url": url})


async def _attach_cdp(ctx: ExecutionContext, cdp_url: str = "http://127.0.0.1:9222") -> Any:
    """接管一个已经在跑的浏览器（O09 共享 CDP）。

    - connect_over_cdp 不注入自动化标志（引擎只是"连上去"），保留用户登录态
    - 挂 _ant_owned = True，stealth.apply 见到它就跳过
    - 页面挑最后一个（最近使用的标签）
    - **CDP 是本地回环连接，绝不能走系统代理**（真实环境抓到的 bug：代理会 502）
    """
    import os

    # 把回环地址加进 NO_PROXY，否则 HTTP_PROXY 会劫持 CDP 连接
    no_proxy = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
    if "127.0.0.1" not in no_proxy:
        merged = (no_proxy + "," if no_proxy else "") + "127.0.0.1,localhost"
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged

    from playwright.async_api import async_playwright

    if getattr(ctx, "browser", None) is not None:
        return ctx.page  # 已接管/已启动，不重复连

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        await pw.stop()
        raise _LocateError(
            f"接管浏览器失败（{cdp_url}）：{exc}。"
            f"确认浏览器以 --remote-debugging-port 启动且端口正确"
        ) from exc

    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    pages = _safe_list(getattr(context, "pages", None) or [])
    page = pages[-1] if pages else await context.new_page()

    setattr(ctx, "_playwright", pw)
    setattr(ctx, "browser", browser)
    setattr(ctx, "browser_context", context)
    setattr(ctx, "_ant_owned", True)  # 接管的浏览器不做 stealth 注入
    ctx.page = page
    ctx.log("info", f"已接管外部浏览器: {cdp_url}（{len(pages)} 个页面）")
    return page


@register("web_attach")
@_guard
async def web_attach(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {cdp?, url?, page_identifier?, kind=url|title, match=contains|exact, wait_until, limit_ms}

    两种用法：
      ① 给 cdp（如 http://127.0.0.1:9222）→ **接管外部浏览器**（O09 共享 CDP）：
         connect_over_cdp 连上去，挑一个已有页面。引擎不再自己 launch。
         接管的浏览器自动跳过 stealth 注入（指纹归浏览器自己管）。
      ② 不给 cdp → 在引擎已开的浏览器里按 url/title 挑一个已有页面。
    """
    kind = args.get("kind", "url")
    match = args.get("match", "contains")
    url = _text(ctx, args, "url", "") or ""
    ident = _text(ctx, args, "page_identifier", "") or url
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    cdp = _text(ctx, args, "cdp", "") or ""

    if cdp:
        await _attach_cdp(ctx, cdp)
        # 记住 CDP 地址：录制器要靠它给流程补"前置 attach 节点"，
        # 否则录出来的流程第一条就是操作，跑的时候没有页面可操作
        try:
            ctx.cdp_url = cdp
        except Exception:  # noqa: BLE001
            pass

    pages = _all_pages(ctx)
    if not pages:
        return Result.fail("没有已打开的页面可接管")

    chosen = None
    index = 0
    for i, page in enumerate(pages):
        hay = await _title_of(page) if kind == "title" else str(getattr(page, "url", "") or "")
        if _match(hay, ident, match):
            chosen, index = page, i
            break

    if chosen is None:
        return Result.fail(f"未找到匹配的页面（{kind}={ident}）")

    ctx.page = chosen
    frames = getattr(ctx, "frames", None)
    if isinstance(frames, list):
        frames.clear()

    # url 只用于**匹配**已有页面，绝不能顺手当导航目标 ——
    # 原来写成 `if url: goto(url)`，而到处都传 "myseller.taobao.com" 这种
    # 不带协议的标识串，结果 attach 完就炸在 Page.navigate: invalid URL。
    if _flag(args, "navigate", False):
        if not url.startswith(("http://", "https://")):
            return Result.fail(f"navigate=True 需要完整 URL（http/https 开头），收到 {url!r}")
        await chosen.goto(url, wait_until=args.get("wait_until", "load"), timeout=limit)

    return Result.done(f"已接管第 {index} 个页面", data={"index": index, "url": str(getattr(chosen, "url", ""))})


@register("web_close")
@_guard
async def web_close(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=page|all}"""
    page = _page(ctx)
    kind = args.get("kind", "page")

    pages = _all_pages(ctx)
    if kind == "all":
        for p in pages:
            try:
                await p.close()
            except Exception:  # noqa: BLE001 - 关不掉也不该中断收尾
                pass
        # 自己起的浏览器要连进程一起收掉，否则残留 playwright 子进程
        browser = getattr(ctx, "browser", None)
        pw = getattr(ctx, "_playwright", None)
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
            setattr(ctx, "browser", None)
            setattr(ctx, "browser_context", None)
        if pw is not None:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass
            setattr(ctx, "_playwright", None)
        ctx.page = None
        return Result.done(f"已关闭 {len(pages)} 个页面")

    await page.close()
    rest = [p for p in pages if p is not page]
    ctx.page = rest[-1] if rest else None
    return Result.done("页面已关闭")


@register("web_tab")
@_guard
async def web_tab(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=index|title|url, index=0, title, url, match=contains|exact, save_as}"""
    kind = args.get("kind", "index")
    match = args.get("match", "contains")
    pages = _all_pages(ctx)
    if not pages:
        return Result.fail("没有可切换的标签页")

    chosen = None
    index = 0

    if kind == "index":
        index = _ms(ctx, args, "index", 0)
        if index >= len(pages):
            return Result.fail(f"标签页下标越界: {index}（共 {len(pages)} 个）")
        chosen = pages[index]
    else:
        pattern = _text(ctx, args, "title" if kind == "title" else "url", "") or ""
        for i, page in enumerate(pages):
            hay = await _title_of(page) if kind == "title" else str(getattr(page, "url", "") or "")
            if _match(hay, pattern, match):
                chosen, index = page, i
                break
        if chosen is None:
            return Result.fail(f"未找到标签页（{kind}={pattern}）")

    ctx.page = chosen
    frames = getattr(ctx, "frames", None)
    if isinstance(frames, list):
        frames.clear()

    info = {"index": index, "url": str(getattr(chosen, "url", "") or ""), "title": await _title_of(chosen)}
    _save(ctx, args, info)
    return Result.done(f"已切到标签页 {index}", data=info)


@register("web_frame_in")
@_guard
async def web_frame_in(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=index|name|target, index=0, name, target}"""
    page = _page(ctx)
    kind = args.get("kind", "index")

    if kind == "name":
        name = _need(ctx, args, "name", "web_frame_in")
        selector = f'iframe[name="{name}"]'
    elif kind == "target":
        selector = _need(ctx, args, "target", "web_frame_in")
    else:
        index = _ms(ctx, args, "index", 0)
        selector = f"iframe >> nth={index}"

    frames = getattr(ctx, "frames", None)
    if not isinstance(frames, list):
        return Result.fail("上下文不支持 iframe 栈")
    frames.append(selector)
    return Result.done(f"已进入 iframe: {selector}", data=selector)


@register("web_frame_out")
@_guard
async def web_frame_out(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """回到主文档（清空 iframe 链）。"""
    _page(ctx)
    frames = getattr(ctx, "frames", None)
    depth = len(frames) if isinstance(frames, list) else 0
    if isinstance(frames, list):
        frames.clear()
    return Result.done(f"已回到主文档（退出 {depth} 层 iframe）")


@register("web_back")
@_guard
async def web_back(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {wait_until=load, limit_ms}"""
    page = _page(ctx)
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    await page.web_back(wait_until=args.get("wait_until", "load"), timeout=limit)
    return Result.done("已后退")


@register("web_forward")
@_guard
async def web_forward(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {wait_until=load, limit_ms}"""
    page = _page(ctx)
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    await page.web_forward(wait_until=args.get("wait_until", "load"), timeout=limit)
    return Result.done("已前进")


@register("web_reload")
@_guard
async def web_reload(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {wait_until=load, limit_ms}"""
    page = _page(ctx)
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    await page.reload(wait_until=args.get("wait_until", "load"), timeout=limit)
    return Result.done("已刷新")


# ================================================================ 交互

@register("web_click")
@_guard
async def web_click(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, limit_ms, kind=left|right|middle|double, force, follow_new_tab,
             mode=cdp|human|real, verify_text, hold_ms}

    mode：
      cdp   —— 原生 locator.click()（默认，快）
      human —— CDP 层拟人轨迹（贝塞尔 + 按住）
      real  —— **操作系统级真实鼠标**（SendInput）：淘宝/千牛这类后台的受控组件
               对 CDP 合成事件响应不稳，关键操作要用这个。
    verify_text：点之前校验元素文本，不符就拒绝（防止定位退化后点错）。
    """
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_click")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    kind = args.get("kind", "left")
    force = _flag(args, "force", False)
    mode = str(args.get("mode") or "cdp").lower()
    verify_text = str(args.get("verify_text") or "").strip()
    hold_ms = int(args.get("hold_ms", 60) or 60)

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = await _pick(loc, target, limit)

    state = args.get("wait_state", "visible")
    if state:
        await el.wait_for(state=state, timeout=limit)

    # 点前校验：定位退化（比如靠 path 命中）时，文本对不上就拒绝，避免点错
    if verify_text:
        try:
            got = ((await el.inner_text()) or "").strip()
        except Exception:  # noqa: BLE001
            got = ""
        if verify_text not in got:
            return Result.fail(
                f"点击前校验失败：期望含「{verify_text}」，实际「{got[:40]}」→ 拒绝点击（可能定位退化了）"
            )

    # ---------------------------------------------------------- 真实鼠标
    if mode == "real":
        from engine import real_mouse

        if not real_mouse.available():
            return Result.fail("mode=real 需要 Windows（SendInput 不可用）")
        try:
            box = await el.bounding_box()
        except Exception as exc:  # noqa: BLE001
            return Result.fail(f"拿不到 {target} 的位置，无法按坐标点击: {exc}")
        if not box:
            return Result.fail(f"拿不到 {target} 的位置（可能不可见或已移除）")
        # 硬件点击按屏幕绝对坐标投递，落到的是"当前前台窗口"——
        # 不把浏览器带到前台，点击会打进别的窗口（实测页面根本不响应）
        fg = await real_mouse.ensure_foreground(page)
        if not fg.get("brought"):
            # 置前失败也别直接放弃：仍然试着点（有些环境窗口本来就在前台）
            pass
        try:
            x, y = await real_mouse.page_point_to_screen(page, box)
            info = await real_mouse.click_at(
                x, y, button=("right" if kind == "right" else "left"),
                hold_ms=hold_ms, double=(kind == "double"),
            )
        except Exception as exc:  # noqa: BLE001
            return Result.fail(f"真实鼠标点击失败: {type(exc).__name__}: {exc}")
        return Result.done(f"真实鼠标点击 {target} @({x},{y})", data=info)

    before = len(_all_pages(ctx))

    if mode == "human":
        from engine.middleware import human_click

        await human_click(el, hold_ms=hold_ms)
    elif kind == "double":
        await el.dblclick(force=force)
    else:
        # 亚像素抖动：CDP 直接点中心会得到**整数** clientX/clientY，
        # 而真人在 150% 缩放的屏幕上点出来的坐标几乎必然带小数（物理像素 / DPR）。
        # 实测：CDP 点击 clientX=720（整），真实鼠标 clientX=457.33（带小数）。
        # 整数坐标是可检测特征，所以在元素范围内随机取一个亚像素位置来点。
        import random

        pos: dict[str, float] | None = None
        try:
            box = await el.bounding_box()
            if box and box["width"] > 2 and box["height"] > 2:
                pos = {
                    "x": box["width"] * (0.35 + random.random() * 0.3) + random.random(),
                    "y": box["height"] * (0.35 + random.random() * 0.3) + random.random(),
                }
        except Exception:  # noqa: BLE001 - 拿不到位置就按默认中心点
            pos = None
        if pos:
            await el.click(button=kind, force=force, position=pos)
        else:
            await el.click(button=kind, force=force)

    if _flag(args, "follow_new_tab", False):
        pages = _all_pages(ctx)
        if len(pages) > before:
            ctx.page = pages[-1]
            return Result.done(f"已点击并切到新标签页: {target}", data={"tab": len(pages) - 1})

    return Result.done(f"已点击 {target}")


@register("web_hover")
@_guard
async def web_hover(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, limit_ms, hold_ms=0, force}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_hover")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    hold = _ms(ctx, args, "hold_ms", 0)

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = await _pick(loc, target, limit)
    await el.hover(force=_flag(args, "force", False))

    if hold:
        await asyncio.sleep(hold / 1000)
    return Result.done(f"已悬停 {target}")


@register("web_scroll")
@_guard
async def web_scroll(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=down|up|top|bottom|element, distance=500, target, hints}"""
    page = _page(ctx)
    kind = args.get("kind", "down")
    distance = _ms(ctx, args, "distance", 500)

    if kind == "element":
        target = _need(ctx, args, "target", "web_scroll")
        limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
        loc = await _locate(page, target, args.get("hints"), ctx, limit)
        el = await _pick(loc, target, limit)
        await el.scroll_into_view_if_needed()
        return Result.done(f"已滚动到元素 {target}")

    if kind == "top":
        await page.evaluate("window.scrollTo(0, 0)")
        return Result.done("已滚到顶部")
    if kind == "bottom":
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return Result.done("已滚到底部")

    delta = -distance if kind == "up" else distance
    await page.mouse.wheel(0, delta)
    return Result.done(f"已滚动 {delta}px")


@register("web_drag")
@_guard
async def web_drag(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, to, to_hints, x, y, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_drag")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)

    src_loc = await _locate(page, target, args.get("hints"), ctx, limit)
    src = await _pick(src_loc, target, limit)

    to = _text(ctx, args, "to", "")
    if to:
        dst_loc = await _locate(page, to, args.get("to_hints"), ctx, limit)
        dst = await _pick(dst_loc, str(to))
        await src.drag_to(dst)
        return Result.done(f"已拖拽 {target} → {to}")

    if args.get("x") is None and args.get("y") is None:
        return Result.fail("web_drag 需要 to 或 x/y 坐标")

    x = _ms(ctx, args, "x", 0)
    y = _ms(ctx, args, "y", 0)
    await src.hover()
    mouse = page.mouse
    await mouse.down()
    await mouse.move(x, y)
    await mouse.up()
    return Result.done(f"已拖拽 {target} 到 ({x},{y})")


@register("web_type")
@_guard
async def web_type(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, text, hints, clear=true, kind=fill|type|press, delay_ms=0, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_type")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    # 兼容两套命名：节点文档写 text，MCP 工具/表单习惯写 value
    text = _text(ctx, args, "text", "") or _text(ctx, args, "value", "")
    kind = args.get("kind", "fill")
    delay = _ms(ctx, args, "delay_ms", 0)

    if not text and not _flag(args, "allow_empty", False):
        return Result.fail("web_type: 要填的内容为空（给 text 或 value；确实要清空请设 allow_empty=true）")

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = await _pick(loc, target, limit)

    if _flag(args, "clear", True) and kind != "fill":
        await el.fill("")

    if kind == "type":
        await el.type(text, delay=delay)
    elif kind == "press":
        await el.press_sequentially(text, delay=delay)
    else:
        await el.fill(text)

    return Result.done(f"已输入 {text!r} → {target}")


@register("web_type_label")
@_guard
async def web_type_label(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {label, value, kind=auto|label|placeholder|target, target, hints, limit_ms}"""
    page = _page(ctx)
    label = _need(ctx, args, "label", "web_type_label")
    value = _text(ctx, args, "value", "")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    kind = args.get("kind", "auto")
    root = _root(page, ctx)

    candidates: list[tuple[str, Any]] = []
    if kind in ("auto", "label"):
        candidates.append(("label", root.get_by_label(label)))
    if kind in ("auto", "placeholder"):
        getter = getattr(root, "get_by_placeholder", None)
        if getter is not None:
            candidates.append(("placeholder", getter(label)))
    if kind == "target" or args.get("target"):
        target = _text(ctx, args, "target", "") or label
        candidates.append(("target", await _locate(page, target, args.get("hints"), ctx, limit)))

    last = ""
    for how, loc in candidates:
        try:
            el = await _pick(loc, f"{how}:{label}", limit)
        except _LocateError as exc:
            last = str(exc)
            continue
        await el.fill(value)
        return Result.done(f"已按{how}填写 {label} = {value!r}")

    return Result.fail(f"找不到标签对应输入框: {label}（{last or '无候选'}）")


@register("web_fill_many")
@_guard
async def web_fill_many(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {pairs, pairs_as, stop_on_fail=false, gap_ms=0, limit_ms}

    pairs: [{"label": "用户名", "value": "x"}, {"target": "#pwd", "value": "y"}] 或 {标签: 值}
    """
    page = _page(ctx)
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    gap = _ms(ctx, args, "gap_ms", 0)

    raw = args.get("pairs")
    if not raw and args.get("pairs_as"):
        raw = f"{{{args['pairs_as']}}}"
    pairs = ctx.resolve(raw)
    if isinstance(pairs, dict):
        pairs = [{"label": k, "value": v} for k, v in pairs.items()]
    if not isinstance(pairs, (list, tuple)) or not pairs:
        return Result.fail("web_fill_many 的 pairs 必须是数组或字典")

    stop = _flag(args, "stop_on_fail", False)
    filled = 0
    errors: list[str] = []

    for i, item in enumerate(pairs):
        if not isinstance(item, dict):
            errors.append(f"第 {i} 项不是对象")
            if stop:
                break
            continue

        key = item.get("label") or item.get("target")
        if not key:
            errors.append(f"第 {i} 项缺 label/target")
            if stop:
                break
            continue

        value = ctx.resolve(item.get("value"))
        target = item.get("target")
        root = _root(page, ctx)

        try:
            if target:
                loc = await _locate(page, target, item.get("hints"), ctx, limit)
            else:
                loc = root.get_by_label(str(key))
            el = await _pick(loc, str(key), limit)
            await el.fill("" if value is None else str(value))
            filled += 1
        except _LocateError as exc:
            errors.append(str(exc))
            if stop:
                break

        if gap:
            await asyncio.sleep(gap / 1000)

    data = {"filled": filled, "errors": errors, "total": len(pairs)}
    if errors and stop:
        return Result(ok=False, error=f"批量填写中断: {errors[0]}", message="批量填写中断", data=data)
    return Result.done(f"已填 {filled}/{len(pairs)} 项", data=data)


@register("web_pick_option")
@_guard
async def web_pick_option(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, kind=value|label|index|text, value, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_pick_option")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    kind = args.get("kind", "value")
    value = _text(ctx, args, "value", "")
    if value == "":
        return Result.fail("web_pick_option 缺 value")

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = await _pick(loc, target, limit)

    if kind == "index":
        await el.select_option(index=_ms(ctx, args, "value", 0))
    elif kind in ("label", "text"):
        await el.select_option(label=str(value))
    else:
        await el.select_option(value=str(value))

    return Result.done(f"已选择 {kind}={value}")


@register("web_toggle")
@_guard
async def web_toggle(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, checked=true, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_toggle")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    checked = _flag(args, "checked", True)

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = await _pick(loc, target, limit)

    if checked:
        await el.check()
    else:
        await el.uncheck()
    return Result.done(f"已{'勾选' if checked else '取消勾选'} {target}")


@register("web_upload")
@_guard
async def web_upload(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, path, limit_ms}  path 可为字符串或数组"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_upload")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)

    raw = ctx.resolve(args.get("path"))
    if raw is None:
        return Result.fail("web_upload 缺 path")
    from engine.paths import resolve_path

    items = raw if isinstance(raw, (list, tuple)) else [raw]
    files = [str(resolve_path(x)) for x in items]
    if not files or not files[0]:
        return Result.fail("web_upload 缺 path")

    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        return Result.fail(f"文件不存在: {missing[0]}")

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = await _pick(loc, target, limit)
    await el.set_input_files(files if len(files) > 1 else files[0])
    return Result.done(f"已上传 {len(files)} 个文件")


# ================================================================ 读取

@register("web_elem_info")
@_guard
async def web_elem_info(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, attr=text|html|value|href|src|count|all, name, save_as, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_elem_info")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    attr = args.get("attr", "text")

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    if attr == "count":
        count = await loc.count()
        _save(ctx, args, count)
        return Result.done(f"{target} 命中 {count} 个", data=count)

    el = await _pick(loc, target, limit)

    if attr == "all":
        info: dict[str, Any] = {}
        for name, getter in (
            ("text", "inner_text"),
            ("html", "inner_html"),
            ("value", "input_value"),
        ):
            fn = getattr(el, getter, None)
            if fn is None:
                continue
            try:
                info[name] = await fn()
            except Exception:  # noqa: BLE001 - 某字段读不到不影响其它
                info[name] = None
        for attr_name in ("href", "src", "title"):
            try:
                info[attr_name] = await el.get_attribute(attr_name)
            except Exception:  # noqa: BLE001
                info[attr_name] = None
        _save(ctx, args, info)
        return Result.done(f"已读取 {target} 全部属性", data=info)

    if attr == "text":
        value = await el.inner_text()
    elif attr == "html":
        value = await el.inner_html()
    elif attr == "value":
        value = await el.input_value()
    else:
        name = args.get("name") or attr
        value = await el.get_attribute(str(name))

    _save(ctx, args, value)
    return Result.done(f"已读取 {target}.{attr}", data=value)


@register("web_children")
@_guard
async def web_children(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, child=*, save_as, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_children")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    child = _text(ctx, args, "child", "") or "*"

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    await _pick(loc, target, limit)

    js = """(els) => els.map(e => ({
        tag: e.tagName ? e.tagName.toLowerCase() : '',
        text: (e.innerText || '').trim(),
        html: e.outerHTML || ''
    }))"""
    items = await loc.locator(child).evaluate_all(js)
    items = _safe_list(items)
    _save(ctx, args, items)
    return Result.done(f"{target} 有 {len(items)} 个子元素", data=items)


@register("web_siblings")
@_guard
async def web_siblings(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, kind=all|prev|next, include_self=false, save_as, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_siblings")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    kind = args.get("kind", "all")

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    await _pick(loc, target, limit)

    js = """(els, opt) => {
        if (!els.length) return [];
        const el = els[0];
        const parent = el.parentElement;
        if (!parent) return [];
        const all = Array.from(parent.children);
        const i = all.indexOf(el);
        let list = all;
        if (opt.kind === 'prev') list = all.slice(0, i < 0 ? 0 : i);
        else if (opt.kind === 'next') list = all.slice(i + 1);
        else if (!opt.includeSelf) list = all.filter(x => x !== el);
        return list.map(e => ({
            tag: e.tagName ? e.tagName.toLowerCase() : '',
            text: (e.innerText || '').trim(),
            html: e.outerHTML || ''
        }));
    }"""
    items = await loc.evaluate_all(js, {"kind": kind, "includeSelf": _flag(args, "include_self", False)})
    items = _safe_list(items)
    _save(ctx, args, items)
    return Result.done(f"{target} 有 {len(items)} 个兄弟元素", data=items)


@register("web_exec", raw=("code",))
@_guard
async def web_exec(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {code, kind=now|on_load|script, save_as}"""
    page = _page(ctx)
    code = _script(ctx, args, "code")
    if not code:
        raise _NodeError("web_exec 缺 code")
    kind = args.get("kind", "now")

    if kind == "on_load":
        context = getattr(page, "context", None)
        if context is None or not hasattr(context, "add_init_script"):
            return Result.fail("当前上下文不支持 on_load 注入")
        await context.add_init_script(code)
        return Result.done("已注册页面加载前脚本")

    if kind == "script":
        await page.add_script_tag(content=code)
        return Result.done("已插入 <script>")

    result = await page.evaluate(code)
    _save(ctx, args, result)
    return Result.done("已执行注入脚本", data=result)


@register("web_js", raw=("code",))
@_guard
async def web_js(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {code, save_as}"""
    page = _page(ctx)
    code = _script(ctx, args, "code")
    if not code:
        raise _NodeError("web_js 缺 code")
    result = await page.evaluate(code)
    _save(ctx, args, result)
    return Result.done("JS 已执行", data=result)


# ================================================================ 等待与判断

JS_SCAN_INTERACTIVE = r"""
([kind, limit]) => {
  // 传统可交互标签
  const NATIVE = "a,button,input,select,textarea,summary,label,"
    + "[role],[onclick],[tabindex],[contenteditable=true]";
  // 淘宝这类后台把按钮做成 <p class="text">男装</p> / <div class="info-content">23区</div>
  // —— 没有 a/button，没有 role，onclick 也是 React 事件委托（DOM 上没有属性）。
  // 唯一通用判据是 cursor:pointer。
  const TEXTUAL = "p,span,div,li,td,dd,dt,h1,h2,h3,h4,h5,h6";

  const out = [];
  const seen = new Set();
  const max = limit || 100;

  const accRole = (e) => {
    const explicit = e.getAttribute("role");
    if (explicit) return explicit;
    const t = (e.getAttribute("type") || "").toLowerCase();
    if (e.tagName === "INPUT") {
      if (t === "hidden") return null;
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (["submit", "button", "reset", "image"].includes(t)) return "button";
      if (t === "search") return "searchbox";
      return "textbox";
    }
    if (e.tagName === "BUTTON") return "button";
    if (e.tagName === "A") return e.getAttribute("href") !== null ? "link" : null;
    if (e.tagName === "SELECT") return e.multiple ? "listbox" : "combobox";
    if (e.tagName === "TEXTAREA") return "textbox";
    return null;
  };

  const accName = (e) => {
    const lb = e.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map(id => {
        const n = document.getElementById(id);
        return n ? (n.innerText || "").trim() : "";
      }).filter(Boolean).join(" ").trim();
      if (t) return t;
    }
    const v = (e.getAttribute("aria-label") || "").trim();
    if (v) return v;
    if (e.id) {
      try {
        const lab = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
        if (lab) { const t = (lab.innerText || "").trim(); if (t) return t; }
      } catch (_) {}
    }
    for (const a of ["placeholder", "title", "alt"]) {
      const x = (e.getAttribute(a) || "").trim();
      if (x) return x;
    }
    if (e.tagName === "INPUT" && e.value && e.value.trim()) return e.value.trim();
    return (e.innerText || "").trim();
  };

  const visible = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(e);
    return !(st.visibility === "hidden" || st.display === "none" || st.opacity === "0");
  };

  const push = (e, viaPointer) => {
    const role = accRole(e);
    const name = accName(e).replace(/\s+/g, " ").slice(0, 60);
    if (!role && !name) return;
    const sig = (role || "?") + "|" + name + "|" + Math.round(e.getBoundingClientRect().y / 4);
    if (seen.has(sig)) return;
    seen.add(sig);
    const r = e.getBoundingClientRect();
    out.push({
      tag: e.tagName.toLowerCase(), role: role, name: name,
      type: (e.getAttribute("type") || "").toLowerCase() || null,
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      disabled: !!(e.disabled || e.getAttribute("aria-disabled") === "true"),
      via: viaPointer ? "pointer" : "native",
    });
  };

  // ① 原生可交互
  for (const e of document.querySelectorAll(kind === "all" ? "body *" : NATIVE)) {
    if (!visible(e)) continue;
    push(e, false);
    if (out.length >= max) return out;
  }

  // ② cursor:pointer 的文本元素（淘宝类目树/菜单/选项）
  if (kind !== "all") {
    const isPtr = (e) => { try { return getComputedStyle(e).cursor === "pointer"; } catch (_) { return false; } };
    for (const e of document.querySelectorAll(TEXTUAL)) {
      if (!isPtr(e) || !visible(e)) continue;
      // 只取最内层可点元素：子元素也是 pointer 就跳过（取更深的那层）
      let deeper = false;
      for (const c of e.children) { if (isPtr(c)) { deeper = true; break; } }
      if (deeper) continue;
      const txt = (e.innerText || "").trim();
      if (txt.length > 60) continue;   // 太大的容器不算可点项
      push(e, true);
      if (out.length >= max) return out;
    }
  }

  return out;
}
"""


JS_EXTRACT_LIST = r"""
([itemSel, fields, limit]) => {
  const items = Array.from(document.querySelectorAll(itemSel)).slice(0, limit);
  const out = [];
  for (const it of items) {
    const row = {};
    if (!fields || !fields.length) {
      row["文本"] = (it.innerText || "").trim().slice(0, 300);
    } else {
      let idx = 0;
      for (const f of fields) {
        const nm = f.name || f.key || ("列" + (++idx));
        let v = null;
        if (f.selector) {
          const el = it.querySelector(f.selector);
          if (el) {
            if (f.attr) v = el.getAttribute(f.attr);
            else v = el.innerText || el.value || el.getAttribute("href") || "";
          }
        } else {
          v = it.innerText || "";
        }
        row[nm] = v === null || v === undefined ? null : String(v).trim().slice(0, 300);
      }
    }
    out.push(row);
  }
  return out;
}
"""


@register("web_extract")
@_guard
async def web_extract(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """批量抓列表成结构化数组。args: {item, fields=[{name, selector, attr?}], limit=50, save_as}

    item：每一项的容器选择器；fields 不给就取整块文本。
    比"逐个元素读"快得多：一次 evaluate 拿完整页数据。
    """
    page = _page(ctx)
    item = _need(ctx, args, "item", "web_extract")
    fields = args.get("fields") or []
    if not isinstance(fields, list):
        return Result.fail("web_extract: fields 必须是数组，形如 [{name, selector}]")
    limit = int(args.get("limit", 50) or 50)

    rows = await page.evaluate(JS_EXTRACT_LIST, [str(item), fields, limit])
    _save(ctx, args, rows)
    cols = list(rows[0].keys()) if rows else []
    return Result.done(
        f"抓到 {len(rows)} 项" + (f"，字段 {cols}" if cols else ""),
        data=rows,
    )


JS_DISCOVER_FIELDS = r"""
([maxLabel, includeEmpty]) => {
  // 表单控件的 label 在正上方 ~24px（next-design Form.Item 结构）。
  // 淘宝发品页 25 个下拉框 placeholder 全是"请选择"、无 id 无 name ——
  // 靠这个把真实字段名找出来，不用一个个猜 CSS。
  const labelOf = (e) => {
    const r0 = e.getBoundingClientRect();
    let best = "", bestDy = 999;
    for (const c of document.querySelectorAll("label, dt, th, span, div")) {
      if (c === e || c.contains(e)) continue;
      const cr = c.getBoundingClientRect();
      if (cr.width < 8 || cr.height < 6) continue;
      const dy = r0.y - cr.y;
      if (dy < 2 || dy > 34) continue;
      if (cr.x > r0.x + r0.width || cr.x + cr.width < r0.x) continue;
      const direct = Array.from(c.childNodes)
        .filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join("").trim();
      if (!direct || direct.length < 2 || direct.length > 20) continue;
      if (/^[\*＊]|^(重要|必填|选填)$/.test(direct)) continue;
      if (dy < bestDy) { best = direct; bestDy = dy; }
    }
    return best;
  };

  const out = [];
  for (const e of document.querySelectorAll("input, select, textarea")) {
    const r = e.getBoundingClientRect();
    if (r.width < 20 || r.height < 8) continue;
    const ty = (e.getAttribute("type") || "").toLowerCase();
    if (ty === "hidden") continue;
    const label = labelOf(e);
    const value = String(e.value || "");
    if (!includeEmpty && !value) continue;
    out.push({
      label: label.slice(0, maxLabel),
      tag: e.tagName.toLowerCase(),
      type: ty || null,
      value: value.slice(0, 30),
      ph: (e.getAttribute("placeholder") || "").slice(0, 20),
      required: !!e.required || !!(e.closest && e.closest("[class*=required]")),
      disabled: !!e.disabled,
      xy: [Math.round(r.x), Math.round(r.y)],
      w: Math.round(r.width),
    });
  }
  return out;
}
"""


@register("web_fields")
@_guard
async def web_fields(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """一次发现页面所有表单字段：label + 类型 + 值 + 必填。

    args: {max_label=16, include_empty=true, save_as}
    直接解决"25 个下拉框全都叫请选择"的识别问题。
    """
    page = _page(ctx)
    mx = int(args.get("max_label", 16) or 16)
    inc = bool(args.get("include_empty", True))
    items = await page.evaluate(JS_DISCOVER_FIELDS, [mx, inc])
    _save(ctx, args, items)
    named = sum(1 for i in items if i.get("label"))
    return Result.done(f"发现 {len(items)} 个字段（{named} 个有 label）", data=items)


JS_PAGE_TEXT = r"""
(limit) => {
  const out = [];
  const seen = new Set();
  for (const e of document.querySelectorAll("h1,h2,h3,h4,h5,p,li,td,th,label,dt,dd,span,div")) {
    const r = e.getBoundingClientRect();
    if (r.width < 10 || r.height < 6) continue;
    // 只取"自己带文本"的节点，避免整块容器重复
    const direct = Array.from(e.childNodes)
      .filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join("").trim();
    if (!direct || direct.length < 2) continue;
    const k = direct.slice(0, 40) + "|" + Math.round(r.y / 8);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push({tag: e.tagName.toLowerCase(), text: direct.slice(0, 80), xy: [Math.round(r.x), Math.round(r.y)]});
    if (out.length >= limit) break;
  }
  return out;
}
"""


@register("web_snapshot")
@_guard
async def web_snapshot(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """页面快照。args: {kind=structure|text, limit=200, save_as}

    structure：可交互元素 + 表单字段的合并视图（"这页能干什么"）
    text：     只列文本块（"这页写了什么"）
    """
    page = _page(ctx)
    kind = str(args.get("kind") or "structure").lower()
    limit = int(args.get("limit", 200) or 200)

    if kind == "text":
        items: Any = await page.evaluate(JS_PAGE_TEXT, [limit])
        _save(ctx, args, items)
        return Result.done(f"页面文本 {len(items)} 块", data=items)

    inter = await page.evaluate(JS_SCAN_INTERACTIVE, ["interactive", limit])
    fields = await page.evaluate(JS_DISCOVER_FIELDS, [16, True])
    data = {"interactive": inter, "fields": fields}
    _save(ctx, args, data)
    return Result.done(
        f"可交互 {len(inter)} 个 / 表单字段 {len(fields)} 个", data=data
    )


@register("web_verify")
@_guard
async def web_verify(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """验证选择器/`@el:` 条目现在还能不能定位。args: {target, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_verify")
    limit = _ms(ctx, args, "limit_ms", 5000)
    try:
        loc = await _locate(page, target, args.get("hints"), ctx, limit)
        el = await _pick(loc, target, limit)
        n = 0
        try:
            n = await loc.count()
        except Exception:  # noqa: BLE001
            n = 1
        info = await el.evaluate(
            """e => {
                const r = e.getBoundingClientRect();
                return {
                  tag: e.tagName.toLowerCase(),
                  text: (e.innerText || e.value || "").trim().slice(0, 60),
                  visible: e.offsetParent !== null,
                  box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                };
            }"""
        )
        info["matched"] = n
        return Result.done(f"{target} 定位成功（匹配 {n} 个）", data=info)
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"{target} 定位失败: {type(exc).__name__}: {exc}")


@register("web_scan")
@_guard
async def web_scan(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """扫出页面上所有可交互元素（按钮/链接/输入框），带 role+name+坐标。

    args: {kind=interactive|all, limit=100, save_as}

    为什么需要：Agent 读不到"页面上有哪些按钮"，只能靠猜 CSS。
    有了这个可以先看一遍再决定拾取哪个。
    """
    page = _page(ctx)
    kind = str(args.get("kind") or "interactive").lower()
    limit = int(args.get("limit", 100) or 100)

    items = list(await page.evaluate(JS_SCAN_INTERACTIVE, [kind, limit]))

    # 子 frame 里也有控件（后台系统大量用 iframe）——一起扫，并标上 frame 链
    from elements.capture import _frame_chain_of

    for f in page.frames:
        if f is page.main_frame:
            continue
        try:
            sub = await f.evaluate(JS_SCAN_INTERACTIVE, [kind, max(limit - len(items), 10)])
            chain = await _frame_chain_of(page, f)
            for it in sub:
                it["frame"] = chain
            items.extend(sub)
        except Exception:  # noqa: BLE001 - 某个 frame 扫不了就跳过
            continue
        if len(items) >= limit:
            break

    items = items[:limit]
    _save(ctx, args, items)
    clickable = sum(1 for i in items if i.get("role"))
    in_frame = sum(1 for i in items if i.get("frame"))
    suffix = f"，其中 iframe 内 {in_frame} 个" if in_frame else ""
    return Result.done(
        f"扫到 {len(items)} 个元素（有语义 role 的 {clickable} 个{suffix}）",
        data=items,
    )


@register("web_matches")
@_guard
async def web_matches(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """匹配到的所有元素列表（支持 css / text= / xpath= / @el: 全部写法）。

    args: {target, hints, limit=50, text_len=200, limit_ms, save_as}
    之前 MCP 的 kind=list 直接用 JS querySelectorAll，遇到 `text=Next` 会
    SyntaxError —— 那个语法只有 locator 认识。
    """
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_matches")
    limit_ms = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    max_n = int(args.get("limit", 50) or 50)
    text_len = int(args.get("text_len", 200) or 200)

    loc = await _locate(page, target, args.get("hints"), ctx, limit_ms)
    try:
        total = await loc.count()
    except Exception:  # noqa: BLE001
        total = 0

    out: list[dict[str, Any]] = []
    for i in range(min(total, max_n)):
        el = loc.nth(i)
        tag, txt = "?", ""
        try:
            tag = (await el.evaluate("e => e.tagName.toLowerCase()")) or "?"
        except Exception:  # noqa: BLE001
            pass
        try:
            txt = ((await el.inner_text()) or "").strip()[:text_len]
        except Exception:  # noqa: BLE001
            pass
        out.append({"index": i, "tag": tag, "text": txt})

    _save(ctx, args, out)
    return Result.done(f"命中 {total} 个，返回 {len(out)} 条", data=out)


@register("web_wait")
@_guard
async def web_wait(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """等等条件成立。args: {target?, text?, url?, kind=visible|attached|hidden|detached, match=contains, limit_ms}

    三种等待对象（任给其一）：
      - target：等元素到某状态（kind 决定状态，兼容老参数名 state）
      - text：  等页面上出现这段文字
      - url：   等地址包含这段字符

    为什么三种放一个节点：MCP 的 web_wait 有 when=selector|text|url|gone 四个分支，
    之前三个分支的参数全被丢弃（state 被忽略、text/url 直接被无视），会返回假阳性。
    """
    page = _page(ctx)
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    # 兼容两套参数名：节点老写法 kind / MCP 的 state
    kind = str(args.get("kind") or args.get("state") or "visible")
    state = _STATES.get(kind)
    if state is None:
        return Result.fail(f"web_wait 不支持的 kind/state: {kind}（可用: {list(_STATES)}）")

    # ① 等文本出现
    text = _text(ctx, args, "text", "") or ""
    if text:
        try:
            await page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=limit)
        except Exception as exc:  # noqa: BLE001
            raise _LocateError(f"等待文本「{text}」超时（{limit}ms）: {type(exc).__name__}") from exc
        return Result.done(f"文本「{text}」已出现")

    # ② 等 URL 变化
    url = _text(ctx, args, "url", "") or ""
    if url:
        import asyncio as _aio

        deadline = _aio.get_event_loop().time() + limit / 1000
        while _aio.get_event_loop().time() < deadline:
            if url in str(page.url):
                return Result.done(f"URL 已包含 {url}")
            await _aio.sleep(0.2)
        return Result.fail(f"等待 URL 包含 {url} 超时（{limit}ms），当前: {str(page.url)[:100]}")

    # ③ 等元素状态
    target = _need(ctx, args, "target", "web_wait")
    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = getattr(loc, "first", loc)
    try:
        await el.wait_for(state=state, timeout=limit)
    except Exception as exc:  # noqa: BLE001
        raise _LocateError(f"等待 {target} 变成 {kind} 超时（{limit}ms）: {type(exc).__name__}") from exc
    return Result.done(f"{target} 已 {kind}")


@register("web_wait_load")
@_guard
async def web_wait_load(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=load|domcontentloaded|networkidle, limit_ms}"""
    page = _page(ctx)
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    kind = args.get("kind", "load")
    if kind not in ("load", "domcontentloaded", "networkidle"):
        return Result.fail(f"web_wait_load 不支持的 kind: {kind}")
    await page.wait_for_load_state(kind, timeout=limit)
    return Result.done(f"页面已 {kind}")


@register("web_load_done")
@_guard
async def web_load_done(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=load|ready, save_as}  → data=bool"""
    page = _page(ctx)
    kind = args.get("kind", "load")
    state = str(await page.evaluate("document.readyState") or "")

    ok = state == "complete" if kind == "load" else state in ("interactive", "complete")
    _save(ctx, args, ok)
    return Result.done(f"readyState={state} → {ok}", data=ok)


@register("web_has")
@_guard
async def web_has(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, limit_ms, save_as} → data=bool（不存在不算失败）"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_has")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    exists = bool(await loc.count())
    _save(ctx, args, exists)
    return Result.done(f"{target} 存在={exists}", data=exists)


@register("web_visible")
@_guard
async def web_visible(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, limit_ms, save_as} → data=bool"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_visible")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    if not await loc.count():
        _save(ctx, args, False)
        return Result.done(f"{target} 不存在 → 不可见", data=False)

    visible = bool(await getattr(loc, "first", loc).web_visible())
    _save(ctx, args, visible)
    return Result.done(f"{target} 可见={visible}", data=visible)


@register("wait")
@_guard
async def wait(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {ms=1000, kind=time|element, target, hints, state=visible, limit_ms}"""
    kind = args.get("kind", "time")

    if kind == "element":
        page = _page(ctx)
        target = _need(ctx, args, "target", "wait")
        limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
        state = _STATES.get(args.get("state", "visible"), "visible")
        loc = await _locate(page, target, args.get("hints"), ctx, limit)
        await getattr(loc, "first", loc).wait_for(state=state, timeout=limit)
        return Result.done(f"已等到 {target} 变为 {state}")

    ms = _ms(ctx, args, "ms", 1000)
    if ms:
        await asyncio.sleep(ms / 1000)
    return Result.done(f"等待 {ms}ms")


# ================================================================ 截图与下载

@register("web_shot")
@_guard
async def web_shot(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=page|full|element, target, hints, path, name, save_as}"""
    page = _page(ctx)
    kind = args.get("kind", "page")
    path = _out_path(ctx, args, _stamp_name("shot"))

    if kind == "element":
        target = _need(ctx, args, "target", "web_shot")
        limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
        loc = await _locate(page, target, args.get("hints"), ctx, limit)
        el = await _pick(loc, target, limit)
        await el.web_shot(path=path)
    else:
        await page.web_shot(path=path, full_page=(kind == "full"))

    _save(ctx, args, path)
    return Result.done(f"已截图 → {path}", data=path)


@register("web_download")
@_guard
async def web_download(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=click|url, target, hints, url, path, name, save_as, limit_ms}"""
    page = _page(ctx)
    kind = args.get("kind", "click")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    path = _out_path(ctx, args, _stamp_name("download", "bin"))

    if kind == "url":
        url = _need(ctx, args, "url", "web_download")
        context = getattr(page, "context", None)
        request = getattr(context, "request", None)
        if request is None:
            return Result.fail("当前上下文不支持直接下载，请用 kind=click")
        resp = await request.get(url)
        body = await resp.body()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(body)
        _save(ctx, args, path)
        return Result.done(f"已下载 → {path}", data=path)

    target = _need(ctx, args, "target", "web_download")
    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = await _pick(loc, target, limit)

    manager = None
    if hasattr(page, "expect_download"):
        cm = page.expect_download()
        if hasattr(cm, "__aenter__"):
            manager = await cm.__aenter__()

    try:
        await el.click()
    finally:
        if manager is not None:
            await cm.__aexit__(None, None, None)

    if manager is None:
        return Result.fail("当前页面不支持下载事件")

    download = await _maybe_await(getattr(manager, "value", None))
    if download is None:
        return Result.fail("没有捕获到下载")
    await download.save_as(path)
    _save(ctx, args, path)
    return Result.done(f"已下载 → {path}", data=path)


@register("web_save_img")
@_guard
async def web_save_img(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {target, hints, path, name, save_as, limit_ms}"""
    page = _page(ctx)
    target = _need(ctx, args, "target", "web_save_img")
    limit = _ms(ctx, args, "limit_ms", DEFAULT_LIMIT_MS)
    path = _out_path(ctx, args, _stamp_name("image"))

    loc = await _locate(page, target, args.get("hints"), ctx, limit)
    el = await _pick(loc, target, limit)

    src = None
    try:
        src = await el.get_attribute("src")
    except Exception:  # noqa: BLE001 - 没有 src 就走元素截图
        src = None

    if src:
        url = urljoin(str(getattr(page, "url", "") or ""), str(src))
        request = getattr(getattr(page, "context", None), "request", None)
        if request is not None:
            resp = await request.get(url)
            body = await resp.body()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(body)
            _save(ctx, args, path)
            return Result.done(f"已保存图片 → {path}", data=path)

    await el.web_shot(path=path)
    _save(ctx, args, path)
    return Result.done(f"已保存图片（元素截图） → {path}", data=path)


# ================================================================ 弹窗与校验

@register("web_dialog")
@_guard
async def web_dialog(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=accept|dismiss, prompt_text, save_as, wait_ms=0}

    先挂一次性处理器（放在触发弹窗的节点之前），wait_ms>0 时再等它真的出现。
    """
    page = _page(ctx)
    kind = args.get("kind", "accept")
    prompt = _text(ctx, args, "prompt_text", "") or ""
    wait_ms = _ms(ctx, args, "wait_ms", 0)
    box: dict[str, Any] = {}

    async def _on_dialog(dialog: Any) -> None:
        try:
            if kind == "dismiss":
                await dialog.dismiss()
            else:
                await dialog.accept(prompt or None)
            box["message"] = str(getattr(dialog, "message", "") or "")
        except Exception as exc:  # noqa: BLE001 - 处理器里不能抛
            box["error"] = str(exc)

    page.once("dialog", _on_dialog)

    if wait_ms:
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        while not box and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if not box:
            return Result.fail(f"{wait_ms}ms 内没有出现弹窗")

    if box:
        _save(ctx, args, box.get("message", ""))
        if box.get("error"):
            return Result.fail(f"处理弹窗失败: {box['error']}")
        return Result.done(f"已{kind}弹窗: {box.get('message', '')}")

    return Result.done(f"已挂上弹窗处理器（{kind}）")


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "is_true":
        return bool(left)
    if op == "is_false":
        return not bool(left)
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op in (">", ">=", "<", "<="):
        try:
            a, b = float(left), float(right)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    if op == "contains":
        try:
            return right in left  # type: ignore[operator]
        except TypeError:
            return False
    if op == "not_contains":
        try:
            return right not in left  # type: ignore[operator]
        except TypeError:
            return False
    if op == "in":
        try:
            return left in right  # type: ignore[operator]
        except TypeError:
            return False
    if op == "regex":
        import re

        try:
            return re.search(str(right), str(left)) is not None
        except re.error:
            return False
    raise _NodeError(f"不支持的运算符: {op}")


@register("web_assert", raw=("expression",))
@_guard
async def web_assert(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=variable|element|text|url|expression, left, op, right,
              target, hints, state=exist, expected_text, message, save_as}"""
    kind = args.get("kind", "variable")
    message = _text(ctx, args, "message", "") or ""

    if kind == "variable":
        left = ctx.resolve(args.get("left"))
        right = ctx.resolve(args.get("right"))
        ok = _compare(left, str(args.get("op", "is_true")), right)
        detail = f"{left!r} {args.get('op', 'is_true')} {right!r}"
    elif kind == "element":
        page = _page(ctx)
        target = _need(ctx, args, "target", "web_assert")
        state = str(args.get("state", "exist"))
        loc = await _locate(page, target, args.get("hints"), ctx, DEFAULT_LIMIT_MS)
        count = await loc.count()
        if state in ("exist", "visible"):
            ok = bool(count)
            if ok and state == "visible":
                ok = bool(await getattr(loc, "first", loc).web_visible())
        elif state in ("absent", "hidden", "detached"):
            ok = not count
            if state == "hidden" and count:
                ok = not bool(await getattr(loc, "first", loc).web_visible())
        else:
            return Result.fail(f"web_assert 不支持的 state: {state}")
        detail = f"{target} 应 {state}（命中 {count}）"
    elif kind == "text":
        page = _page(ctx)
        expected = _text(ctx, args, "expected_text", "") or _text(ctx, args, "right", "")
        if not expected:
            return Result.fail("web_assert(kind=text) 缺 expected_text")
        content = str(await page.content() or "")
        ok = expected in content
        detail = f"页面应包含 {expected!r}"
    elif kind == "url":
        page = _page(ctx)
        expected = _text(ctx, args, "expected_text", "") or _text(ctx, args, "right", "")
        if not expected:
            return Result.fail("web_assert(kind=url) 缺 expected_text")
        url = str(getattr(page, "url", "") or "")
        ok = expected in url
        detail = f"URL 应包含 {expected!r}（实际 {url}）"
    elif kind == "expression":
        page = _page(ctx)
        expr = _need(ctx, args, "left", "web_assert")
        ok = bool(await page.evaluate(expr))
        detail = f"表达式 {expr} 应为真"
    else:
        return Result.fail(f"web_assert 不支持的 kind: {kind}")

    _save(ctx, args, ok)
    if not ok:
        return Result(ok=False, error=message or f"检查点未通过: {detail}", message=message or f"检查点未通过: {detail}", data=False)
    return Result.done(message or f"检查点通过: {detail}", data=True)


async def _snapshot(page: Any, ctx: ExecutionContext, targets: Any) -> dict[str, Any]:
    """按 targets 逐个读值，形成 {键: 值} 快照。"""
    if not targets:
        raise _NodeError("web_snapshot_diff 缺 targets")
    if not isinstance(targets, (list, tuple)):
        targets = [targets]

    root = _root(page, ctx)
    out: dict[str, Any] = {}

    for i, item in enumerate(targets):
        if isinstance(item, dict):
            key = item.get("key") or item.get("label") or item.get("target") or f"f{i}"
            by_label = bool(item.get("label")) and not item.get("target")
            selector = item.get("target") or item.get("label") or ""
        else:
            key = selector = str(item)
            by_label = False

        if not selector:
            out[str(key)] = None
            continue

        loc = root.get_by_label(str(selector)) if by_label else _build(root, str(selector))
        try:
            if not await loc.count():
                out[str(key)] = None
                continue
            el = getattr(loc, "first", loc)
            value: Any = None
            try:
                value = await el.input_value()
            except Exception:  # noqa: BLE001 - 非输入框退回文本
                value = await el.inner_text()
            out[str(key)] = value
        except Exception as exc:  # noqa: BLE001 - 单个字段读不到不影响整体
            out[str(key)] = None
            ctx.log("warning", f"snapshot 读取失败: {key}", error=str(exc))
    return out


async def _ask_input(message: str, default: str = "", kind: str = "single") -> str | None:
    """原生弹窗取一个值（Tk）。没有图形界面就明确失败，不静默卡死。"""
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except Exception as exc:  # noqa: BLE001
        raise _NodeError(f"人工输入不可用（无图形界面）: {exc}") from exc

    def _run() -> str | None:
        root = tk.Tk()
        root.withdraw()
        try:
            return simpledialog.askstring("输入", message, initialvalue=default)
        finally:
            root.destroy()

    return await asyncio.to_thread(_run)


@register("web_prompt")
@_guard
async def web_prompt(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {name, message=请输入值:, default, kind=single|multi, required=true,
              options, min_len, max_len, save_as}"""
    name = _need(ctx, args, "name", "web_prompt")
    message = _text(ctx, args, "message", "") or "请输入值:"
    default = _text(ctx, args, "default", "") or ""
    kind = args.get("kind", "single")
    required = _flag(args, "required", True)
    options = ctx.resolve(args.get("options"))
    limit = _ms(ctx, args, "limit_ms", 0)

    call = _ask_input(message, default, kind)
    value = await (asyncio.wait_for(call, limit / 1000) if limit else call)

    if value is None or (required and str(value).strip() == ""):
        return Result.fail("人工输入被取消或为空")

    text = str(value).strip()
    if isinstance(options, (list, tuple)) and options and text not in [str(o) for o in options]:
        return Result.fail(f"输入值不在可选项内: {text}")

    min_len = _ms(ctx, args, "min_len", 0)
    max_len = _ms(ctx, args, "max_len", 0)
    if min_len and len(text) < min_len:
        return Result.fail(f"输入长度小于 {min_len}")
    if max_len and len(text) > max_len:
        return Result.fail(f"输入长度大于 {max_len}")

    ctx.set_var(str(args.get("save_as") or name), text)
    return Result.done(f"已写入 {name}", data=text)


@register("web_snapshot_diff")
@_guard
async def web_snapshot_diff(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """args: {kind=capture|compare, targets, save_as=form_snapshot}"""
    page = _page(ctx)
    kind = args.get("kind", "capture")
    name = str(args.get("save_as") or "form_snapshot")
    targets = ctx.resolve(args.get("targets"))

    now = await _snapshot(page, ctx, targets)

    if kind == "capture":
        ctx.set_var(name, now)
        return Result.done(f"已记录 {len(now)} 个字段快照 → {name}", data=now)

    if kind != "compare":
        return Result.fail(f"web_snapshot_diff 不支持的 kind: {kind}")

    if not ctx.has_var(name):
        return Result.fail(f"快照对比缺少基线变量: {name}")
    base = ctx.get_var(name)
    if not isinstance(base, dict):
        return Result.fail(f"快照变量 {name} 不是字典")

    diffs = {
        k: {"before": base.get(k), "after": now.get(k)}
        for k in set(base) | set(now)
        if base.get(k) != now.get(k)
    }
    if diffs:
        detail = ", ".join(f"{k}: {v['before']!r}→{v['after']!r}" for k, v in sorted(diffs.items()))
        return Result(ok=False, error=f"快照不一致: {detail}", message=f"快照不一致: {detail}", data=diffs)
    return Result.done("快照一致", data={"diff": {}})
