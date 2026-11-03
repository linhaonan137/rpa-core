"""中间件：节点共用的定位 / 等待 / 诊断 / 重试 / 拟人工具。

这一层**不是节点**，是给 nodes/*.py 调用的工具函数。放这里的原因：
  · 让 36 个网页节点只写业务语义，不各写一套"找不到等 3 秒"的轮子
  · 定位策略、可见性判定、失败快照的口径只有一处，改一次全节点生效

四条设计约束：
  1. **异常只有两种**：LocateError（定位不到）/ WaitTimeout（等不到），
     节点里 `except MiddlewareError` 就能一把兜住，不会有裸 Exception
  2. **诊断类函数绝不抛**：describe / candidates 在 page 已关闭时返回安全默认值，
     因为它们运行在"已经失败"的路径上，再抛就把原始错误盖掉了
  3. **快照写盘失败只记警告**：snapshot 的第一职责是留下现场，盘写不进去
     也得把变量/URL/栈返回给调用方，否则失败现场彻底丢失
  4. **等待一律带 deadline**：任何循环都对比 time.perf_counter()，
     没有"无限等"的路径（朴素实现在这上面卡死过）

元素库条目的解析顺序（PLAN 8.6，顺序不能反）：
    role+name（语义层） → css fallback（属性层） → hints（辅助层）
ARIA 优先是因为页面改版通常只改样式不改语义。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from engine.paths import OUT_DIR

if TYPE_CHECKING:  # 只为类型提示，运行时不依赖 playwright
    from playwright.async_api import Locator, Page

# ---------------------------------------------------------------- 异常


class MiddlewareError(Exception):
    """中间件异常基类。节点只需 catch 这一个类型。"""


class LocateError(MiddlewareError):
    """定位失败：元素库条目不存在、选择器无效、等到超时都没出现。"""

    def __init__(
        self,
        message: str,
        target: str | None = None,
        tried: list[str] | None = None,
    ) -> None:
        self.target = target
        self.tried: list[str] = list(tried or [])
        detail = message
        if self.tried:
            detail = f"{detail}\n  已尝试: " + " → ".join(self.tried)
        super().__init__(detail)


class WaitTimeout(MiddlewareError):
    """等待超时：元素没到指定状态，或 state 参数不认识。"""

    def __init__(self, message: str, state: str | None = None, ms: int = 0) -> None:
        self.state = state
        self.ms = ms
        super().__init__(message)


# ---------------------------------------------------------------- 常量

#: 轮询间隔（毫秒）。50ms ≈ 一秒 20 次，足够灵敏又不打满 CPU
POLL_MS = 50

#: 元素库里算作"定位线索"的键，按可靠性从高到低
_HINT_KEYS = (
    "css",
    "selector",
    "text",
    "label",
    "ariaLabel",
    "aria_label",
    "placeholder",
    "title",
    "testId",
    "test_id",
    "xpath",
    "id",
)


# ---------------------------------------------------------------- 小工具


def _deadline(limit_ms: int) -> float:
    return time.perf_counter() + max(0, int(limit_ms)) / 1000


def _expired(deadline: float) -> bool:
    return time.perf_counter() >= deadline


async def _maybe_await(value: Any) -> Any:
    if asyncio.isfuture(value) or asyncio.iscoroutine(value):
        return await value
    return value


def _first(locator: Any) -> Any:
    """取多匹配定位器里的第一个；没有 .first 就原样返回。"""
    try:
        return locator.first
    except Exception:  # noqa: BLE001 - 已关闭的 locator 取 first 会炸
        return locator


async def _count(locator: Any) -> int:
    if locator is None:
        return 0
    try:
        return int(await locator.count())
    except Exception:  # noqa: BLE001 - page 关闭 / 定位器失效都算 0 个
        return 0


async def _is_visible(locator: Any) -> bool:
    if locator is None:
        return False
    try:
        return bool(await _first(locator).is_visible())
    except Exception:  # noqa: BLE001
        return False


async def _box(locator: Any) -> dict[str, float] | None:
    if locator is None:
        return None
    try:
        box = await _first(locator).bounding_box()
    except Exception:  # noqa: BLE001
        return None
    return box if isinstance(box, dict) else None


def _same_box(a: dict[str, Any], b: dict[str, Any], tol: float = 1.0) -> bool:
    keys = ("x", "y", "width", "height")
    for k in keys:
        try:
            if abs(float(a.get(k, 0)) - float(b.get(k, 0))) > tol:
                return False
        except (TypeError, ValueError):
            return False
    return True


async def _attached_until(locator: Any, deadline: float) -> bool:
    """在 deadline 之前反复问 count()，出现即返回。"""
    while True:
        if await _count(locator) > 0:
            return True
        if _expired(deadline):
            return False
        await asyncio.sleep(POLL_MS / 1000)


# ---------------------------------------------------------------- 定位


def _elements(ctx: Any) -> dict[str, Any]:
    els = getattr(ctx, "elements", None) if ctx is not None else None
    return els if isinstance(els, dict) else {}


def _entry_of(ctx: Any, name: str) -> dict[str, Any]:
    els = _elements(ctx)
    if name not in els:
        avail = "、".join(sorted(map(str, els))[:10]) or "（空）"
        raise LocateError(
            f"元素库无此项: {name}（可用: {avail}）",
            target=f"@el:{name}",
        )
    raw = els[name]
    if isinstance(raw, str):  # 只存了 css 的简写条目
        return {"fallback": raw}
    if isinstance(raw, dict):
        return raw
    raise LocateError(
        f"元素库条目 {name} 类型非法: {type(raw).__name__}（要 dict 或 str）",
        target=f"@el:{name}",
    )


def _make_locator(page: Any, kind: str, value: str, name: str | None = None) -> Any:
    """按 (kind, value) 造定位器。调用方负责 catch —— 非法 role 会抛。"""
    if kind == "role":
        if name:
            return page.get_by_role(value, name=name)
        return page.get_by_role(value)
    if kind == "css":
        return page.locator(value)
    if kind == "text":
        return page.get_by_text(value)
    if kind == "label":
        return page.get_by_label(value)
    if kind == "placeholder":
        return page.get_by_placeholder(value)
    if kind == "title":
        return page.get_by_title(value)
    if kind == "testid":
        return page.get_by_test_id(value)
    if kind == "id":
        return page.locator(f"#{value}")
    if kind == "xpath":
        return page.locator(f"xpath={value}")
    raise LocateError(f"不支持的定位类型: {kind}")


_HINT_KIND = {
    "css": "css",
    "selector": "css",
    "text": "text",
    "label": "label",
    "ariaLabel": "label",
    "aria_label": "label",
    "placeholder": "placeholder",
    "title": "title",
    "testId": "testid",
    "test_id": "testid",
    "xpath": "xpath",
    "id": "id",
}


def _strategies(
    entry: dict[str, Any], hints: dict[str, Any] | None
) -> list[tuple[str, str, str | None]]:
    """把一条元素库条目展开成有序策略表 [(kind, value, name)]，已去重。"""
    merged: dict[str, Any] = {}
    raw_hints = entry.get("hints")
    if isinstance(raw_hints, dict):
        merged.update(raw_hints)
    for k in _HINT_KEYS:  # 条目顶层的即时线索也算
        if entry.get(k):
            merged.setdefault(k, entry[k])
    if hints:
        merged.update({k: v for k, v in hints.items() if v})

    out: list[tuple[str, str, str | None]] = []
    seen: set[tuple[str, str, str | None]] = set()

    def push(item: tuple[str, str, str | None]) -> None:
        if item[1] and item not in seen:
            seen.add(item)
            out.append(item)

    # ① 语义层
    role = entry.get("role") or merged.get("role")
    if role:
        push(("role", str(role), str(entry.get("name") or merged.get("name") or "") or None))
    # ② 属性层
    for key in ("fallback", "css", "selector"):
        val = entry.get(key)
        if val:
            push(("css", str(val), None))
    # ③ 辅助层
    for key in _HINT_KEYS:
        val = merged.get(key)
        if val and key not in ("css", "selector"):
            push((_HINT_KIND[key], str(val), None))
    return out


async def _try_strategy(
    page: Any,
    spec: tuple[str, str, str | None],
    deadline: float,
    tried: list[str],
) -> Any:
    kind, value, name = spec
    label = f"{kind}={value}" + (f"[name={name}]" if name else "")
    tried.append(label)
    try:
        locator = _make_locator(page, kind, value, name)
        locator = await _maybe_await(locator)
    except LocateError:
        raise
    except Exception:  # noqa: BLE001 - 非法 role / 非法 css 都只是"这条策略不行"
        return None
    if locator is None:
        return None
    return locator if await _attached_until(locator, deadline) else None


async def locate(
    page: Any,
    target: str,
    hints: dict[str, Any] | None = None,
    ctx: Any = None,
    limit_ms: int = 30000,
) -> "Locator":
    """统一元素定位。四种 target 形态：

    1. `@el:submit`  → 查 ctx.elements，role+name → css → hints
    2. `text=提交`   → get_by_text
    3. `xpath=//div` → xpath 选择器
    4. 其他          → 当 css

    找不到抛 LocateError（调用方 catch 后转 Result.fail）。
    """
    if page is None:
        raise LocateError("page 为 None，无法定位", target=target)
    if not target or not str(target).strip():
        raise LocateError("target 为空", target=target)
    text = str(target).strip()
    deadline = _deadline(limit_ms)
    tried: list[str] = []

    if text.startswith("@el:"):
        name = text[4:].strip()
        if not name:
            raise LocateError("@el: 后面缺元素名", target=text)
        entry = _entry_of(ctx, name)
        specs = _strategies(entry, hints)
        if not specs:
            raise LocateError(
                f"元素库条目 {name} 没给出任何定位线索（role/css/hints 全空）",
                target=text,
            )
        for idx, spec in enumerate(specs):
            # 剩余预算按剩余策略数均分，避免第一条策略把 30s 全吃掉，
            # 导致后面的 css/hints 根本没机会试
            now = time.perf_counter()
            left = len(specs) - idx
            per_ms = max(0.0, (deadline - now) * 1000 / left)
            sub_deadline = min(deadline, now + per_ms / 1000)
            locator = await _try_strategy(page, spec, sub_deadline, tried)
            if locator is not None:
                return locator
        raise LocateError(f"元素 {name} 的所有定位策略都失效", target=text, tried=tried)

    kind, value = _parse_target(text)
    if not value:
        raise LocateError(f"target 前缀后没有选择器: {text}", target=text)
    tried.append(f"{kind}={value}")
    try:
        locator = _make_locator(page, kind, value)
        locator = await _maybe_await(locator)
    except Exception as exc:  # noqa: BLE001 - 非法选择器要转成 LocateError
        raise LocateError(f"选择器非法: {text}（{exc}）", target=text, tried=tried) from exc

    if await _attached_until(locator, deadline):
        return locator
    raise LocateError(f"元素在 {limit_ms}ms 内未出现: {text}", target=text, tried=tried)


def _parse_target(text: str) -> tuple[str, str]:
    if text.startswith("text="):
        return "text", text[5:].strip()
    if text.startswith("xpath="):
        return "xpath", text[6:].strip()
    if text.startswith("css="):
        return "css", text[4:].strip()
    return "css", text


# ---------------------------------------------------------------- 等待


_STATES = {"attached", "visible", "stable", "hidden", "exist"}


async def wait_ready(locator: Any, state: str = "visible", limit_ms: int = 30000) -> None:
    """三态等待（外加 hidden / exist 两个别名）：

    · attached —— DOM 里有这个节点就行（哪怕 display:none）
    · visible  —— 有且可见（有尺寸、没被 visibility:hidden）
    · stable   —— 可见且位置连续两帧不变（挡动画/加载导致的点偏）
    · hidden / exist —— schema 里的写法，映射到 hidden / attached
    """
    key = (state or "visible").strip().lower()
    if key not in _STATES:
        raise WaitTimeout(f"未知的等待状态: {state}（支持 {sorted(_STATES)}）", state=key)
    if key == "exist":
        key = "attached"

    if locator is None:
        raise WaitTimeout("locator 为 None，无法等待", state=key, ms=limit_ms)

    deadline = _deadline(limit_ms)
    prev: dict[str, float] | None = None

    while True:
        if key == "attached":
            if await _count(locator) > 0:
                return
        elif key == "hidden":
            if await _count(locator) == 0 or not await _is_visible(locator):
                return
        elif key == "visible":
            if await _count(locator) > 0 and await _is_visible(locator):
                return
        else:  # stable
            if await _count(locator) > 0 and await _is_visible(locator):
                box = await _box(locator)
                if box and prev is not None and _same_box(prev, box):
                    return
                prev = box
            else:
                prev = None

        if _expired(deadline):
            raise WaitTimeout(
                f"等待超时 {limit_ms}ms（state={key}）", state=key, ms=limit_ms
            )
        await asyncio.sleep(POLL_MS / 1000)


async def pick_visible(locator: Any) -> "Locator":
    """同名元素有多个时挑可见的那个（可见性优先选择）。

    全都不可见时返回第一个 —— 让调用方的 wait_ready 去报超时，
    比在这里抛"都不可见"更接近真实原因（可能是滚动没到）。
    """
    total = await _count(locator)
    if total <= 0:
        raise LocateError("pick_visible 时元素一个都没有（count=0）")
    for i in range(total):
        try:
            item = locator.nth(i)
            item = await _maybe_await(item)
        except Exception:  # noqa: BLE001 - 单个索引取失败不影响其他候选
            continue
        if await _is_visible(item):
            return item
    return _first(locator)


async def scroll_find(
    page: Any,
    locator_factory: Callable[[], Any],
    limit_ms: int = 15000,
) -> "Locator | None":
    """分段滚动找懒渲染元素（分段滚动查找）。

    locator_factory 是个零参函数，返回 locator（可返回 awaitable）。
    每滚一屏就重问一次；滚不动了（scrollY 不再变）就返回 None。
    """
    if page is None:
        raise LocateError("page 为 None，无法滚动查找")
    if not callable(locator_factory):
        raise LocateError("scroll_find 需要 locator_factory 可调用对象")

    deadline = _deadline(limit_ms)
    step = await _viewport_step(page)

    while True:
        locator = await _safe_factory(locator_factory)
        if locator is not None and await _count(locator) > 0:
            return locator

        if _expired(deadline):
            return None

        before = await _scroll_y(page)
        await _scroll_by(page, step)
        after = await _scroll_y(page)
        if after is None:  # page 可能已关闭
            return None
        if before is not None and after <= before + 1:  # 到底了，再滚不动
            return None
        await asyncio.sleep(POLL_MS / 1000)


async def _safe_factory(factory: Callable[[], Any]) -> Any:
    try:
        return await _maybe_await(factory())
    except Exception:  # noqa: BLE001 - 找不到就是 None，不是异常
        return None


async def _viewport_step(page: Any) -> int:
    try:
        size = await _maybe_await(page.viewport_size)
        if isinstance(size, dict) and size.get("height"):
            return max(100, int(size["height"]) * 3 // 4)
    except Exception:  # noqa: BLE001
        pass
    try:
        h = await page.evaluate("() => window.innerHeight")
        if h:
            return max(100, int(h) * 3 // 4)
    except Exception:  # noqa: BLE001
        pass
    return 600


async def _scroll_y(page: Any) -> int | None:
    try:
        val = await page.evaluate("() => window.scrollY")
    except Exception:  # noqa: BLE001
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


async def _scroll_by(page: Any, step: int) -> None:
    try:
        await page.evaluate("(d) => window.scrollBy(0, d)", step)
    except Exception:  # noqa: BLE001
        try:
            await page.mouse.wheel(0, step)
        except Exception:  # noqa: BLE001 - 滚不动也不能抛，交给下一轮判 dead
            pass


# ---------------------------------------------------------------- 页面聚焦


def _pages_of(page: Any, ctx: Any = None) -> list[Any]:
    for holder in (page, getattr(ctx, "page", None) if ctx is not None else None):
        if holder is None:
            continue
        try:
            pages = holder.context.pages
        except Exception:  # noqa: BLE001
            pages = None
        if isinstance(pages, (list, tuple)) and pages:
            return list(pages)
    extra = getattr(ctx, "pages", None) if ctx is not None else None
    if isinstance(extra, (list, tuple)) and extra:
        return list(extra)
    return []


async def focus_latest(page: Any, ctx: Any = None) -> None:
    """切到最新打开的标签页（通常刚被 target=_blank 打开的那个）。

    找不到任何 page 就静默返回 —— 聚焦失败不该让节点失败。
    """
    pages = _pages_of(page, ctx)
    if not pages:
        return
    latest = pages[-1]
    try:
        await _maybe_await(latest.bring_to_front())
    except Exception:  # noqa: BLE001 - 已关闭 / 不支持置前
        pass
    if ctx is not None and hasattr(ctx, "page"):
        ctx.page = latest


class TabWatcher:
    """监听新标签页的上下文管理器。

    用法：
        with watch_new_tabs(page) as w:
            await page.click("text=打开")
        w.pages  # 期间新开的页（按打开顺序）

    也支持 `async with`。退出时一定摘监听，异常路径也一样。
    """

    def __init__(self, page: Any) -> None:
        self._page = page
        self._ctx = self._resolve_context(page)
        self._handler: Callable[[Any], None] | None = None
        self.pages: list[Any] = []
        self.attached = False

    @staticmethod
    def _resolve_context(page: Any) -> Any:
        try:
            return page.context
        except Exception:  # noqa: BLE001
            return None

    def start(self) -> None:
        if self._ctx is None or self._handler is not None:
            return
        self._handler = self.pages.append
        try:
            self._ctx.on("page", self._handler)
            self.attached = True
        except Exception:  # noqa: BLE001 - 挂不上监听就退化成空列表
            self._handler = None
            self.attached = False

    def stop(self) -> None:
        if self._handler is None:
            return
        # playwright 的 EventEmitter 是 remove_listener，老版本/假对象用 off
        for name in ("remove_listener", "off"):
            fn = getattr(self._ctx, name, None)
            if not callable(fn):
                continue
            try:
                fn("page", self._handler)
                break
            except Exception:  # noqa: BLE001
                continue
        self._handler = None
        self.attached = False

    @property
    def latest(self) -> Any:
        return self.pages[-1] if self.pages else None

    def __enter__(self) -> "TabWatcher":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.stop()
        return False

    async def __aenter__(self) -> "TabWatcher":
        self.start()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self.stop()
        return False


def watch_new_tabs(page: Any) -> TabWatcher:
    return TabWatcher(page)


# ---------------------------------------------------------------- 诊断


#: 元素自己身上跑：中心点被谁盖住了？没盖住返回 null
_COVER_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const top = document.elementFromPoint(x, y);
  if (!top) return null;
  if (top === el || el.contains(top) || top.contains(el)) return null;
  const tag = top.tagName.toLowerCase();
  if (top.id) return tag + '#' + top.id;
  const cls = (typeof top.className === 'string' ? top.className : '').trim().split(/\\s+/)[0];
  return cls ? tag + '.' + cls : tag;
}
"""


async def describe(locator: Any) -> dict[str, Any]:
    """元素诊断：存在吗 / 可见吗 / 几个同名 / 被谁遮挡。

    永远返回完整四个键，page 已关闭也不抛。
    """
    out: dict[str, Any] = {"exists": False, "visible": False, "count": 0, "covered_by": None}
    if locator is None:
        return out

    total = await _count(locator)
    out["count"] = total
    if total <= 0:
        return out

    out["exists"] = True
    out["visible"] = await _is_visible(locator)
    if out["visible"]:
        out["covered_by"] = await _covered_by(locator)
    return out


async def _covered_by(locator: Any) -> str | None:
    try:
        res = await _first(locator).evaluate(_COVER_JS)
    except Exception:  # noqa: BLE001
        return None
    if not res:
        return None
    return str(res)


def _jsonable(value: Any, limit: int = 500) -> Any:
    """快照用：截断超长字符串/列表，其余转成 JSON 可写类型。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...({len(value)} chars)"
    if isinstance(value, (list, tuple)):
        items = [_jsonable(v, limit) for v in list(value)[:20]]
        if len(value) > 20:
            items.append(f"...({len(value)} items)")
        return items
    if isinstance(value, dict):
        return {str(k): _jsonable(v, limit) for k, v in list(value.items())[:50]}
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _warn(ctx: Any, message: str, **kv: Any) -> None:
    try:
        if ctx is not None and callable(getattr(ctx, "log", None)):
            ctx.log("warn", message, **kv)
    except Exception:  # noqa: BLE001 - 记日志失败不能反过来打断流程
        pass


def _node_args(ctx: Any, node_id: str) -> dict[str, Any]:
    last = getattr(ctx, "last_error", None) if ctx is not None else None
    if isinstance(last, dict) and node_id in (
        last.get("node"),
        last.get("node_id"),
    ):
        for key in ("args", "config"):
            args = last.get(key)
            if isinstance(args, dict):
                return dict(args)
    args = getattr(ctx, "node_args", None) if ctx is not None else None
    return dict(args) if isinstance(args, dict) else {}


async def snapshot(
    ctx: Any,
    page: Any,
    node_id: str,
    error: str,
    save_dir: str | Path | None = None,
) -> dict[str, Any]:
    """失败快照 7 件套：截图 / HTML / 变量 / URL / 时间 / 栈 / 节点 args。

    写盘失败只追加 warnings，绝不抛 —— 它本身就跑在失败路径上。
    返回 dict 里的 web_shot/html/meta 是落盘路径（失败时为 None），
    其余字段是内联值，调用方不进磁盘也能读到现场。
    """
    now = time.time()
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
    run_id = str(getattr(ctx, "run_id", "") or "run")
    base = Path(save_dir) if save_dir is not None else OUT_DIR / "runs"
    base = base / f"{run_id}-{node_id}-{ts}"

    info: dict[str, Any] = {
        "node_id": node_id,
        "run_id": run_id,
        "error": str(error),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "ts": ts,
        "url": None,
        "vars": {},
        "args": _node_args(ctx, node_id),
        "stack": "".join(traceback.format_stack(limit=12)),
        "dir": str(base),
        "web_shot": None,
        "html": None,
        "meta": None,
        "warnings": [],
    }

    if callable(getattr(ctx, "all_vars", None)):
        try:
            info["vars"] = _jsonable(ctx.all_vars())
        except Exception as exc:  # noqa: BLE001
            info["warnings"].append(f"变量读取失败: {exc}")

    target_page = page if page is not None else getattr(ctx, "page", None)
    try:
        info["url"] = getattr(target_page, "url", None) if target_page is not None else None
    except Exception:  # noqa: BLE001
        pass

    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        info["warnings"].append(f"建目录失败: {base}（{exc}）")
        _warn(ctx, "快照目录创建失败", node=node_id, dir=str(base))
        return info

    if target_page is not None:
        shot = base / "web_shot.png"
        try:
            await target_page.screenshot(path=str(shot), full_page=True)
            info["web_shot"] = str(shot)
        except Exception as exc:  # noqa: BLE001
            info["warnings"].append(f"截图失败: {exc}")

        try:
            html = await target_page.content()
            html_path = base / "page.html"
            html_path.write_text(str(html), encoding="utf-8")
            info["html"] = str(html_path)
        except Exception as exc:  # noqa: BLE001
            info["warnings"].append(f"HTML 落盘失败: {exc}")

    meta_path = base / "context.json"
    try:
        payload = {k: v for k, v in info.items() if k not in ("dir",)}
        meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        info["meta"] = str(meta_path)
    except Exception as exc:  # noqa: BLE001
        info["warnings"].append(f"元信息落盘失败: {exc}")

    if info["warnings"]:
        _warn(ctx, "快照部分失败", node=node_id, warnings=info["warnings"])
    return info


#: 扫页面里所有"可能可交互"的元素，供失败后做候选发现
_COLLECT_JS = """
() => {
  const SEL = 'a,button,input,select,textarea,[role],[onclick],[contenteditable="true"]';
  const cssOf = (el) => {
    if (el.id) return '#' + el.id;
    const parts = [];
    let cur = el, depth = 0;
    while (cur && cur.nodeType === 1 && depth < 3) {
      let part = cur.tagName.toLowerCase();
      const cls = (typeof cur.className === 'string' ? cur.className : '').trim().split(/\\s+/)[0];
      if (cls) part += '.' + cls;
      const parent = cur.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      cur = parent; depth++;
    }
    return parts.join(' > ');
  };
  const ROLE = {A: 'link', BUTTON: 'button', INPUT: 'textbox', SELECT: 'combobox', TEXTAREA: 'textbox'};
  const out = [];
  for (const el of Array.from(document.querySelectorAll(SEL)).slice(0, 400)) {
    const tag = el.tagName;
    const role = (el.getAttribute('role') || ROLE[tag] || tag.toLowerCase()).toLowerCase();
    const name = (el.getAttribute('aria-label') || el.innerText || el.value ||
                  el.getAttribute('placeholder') || el.getAttribute('title') || '')
                  .trim().replace(/\\s+/g, ' ').slice(0, 60);
    const r = el.getBoundingClientRect();
    let visible = false;
    try {
      const st = getComputedStyle(el);
      visible = !!(r.width && r.height) && st.visibility !== 'hidden' && st.display !== 'none';
    } catch (e) { visible = !!(r.width && r.height); }
    out.push({role: role, name: name, css: cssOf(el), visible: visible});
  }
  return out;
}
"""


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _similarity(want: Any, got: Any) -> float:
    a, b = _norm(want), _norm(got)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 3)


def _score(hints: dict[str, Any], cand: dict[str, Any]) -> float:
    """候选打分：role 0.35 / 名字 0.45 / css 0.20，可见再补 0.05。

    没有线索（hints 为空）时不做维度打分，只按可见性排 —— 至少让
    失败报告里的候选是按"用户真能看见"排的，而不是 DOM 顺序。
    """
    total = 0.0
    weight = 0.0

    role = _norm(hints.get("role"))
    if role:
        weight += 0.35
        total += 0.35 if _norm(cand.get("role")) == role else 0.0

    want = hints.get("name") or hints.get("text") or hints.get("ariaLabel") or hints.get("aria_label")
    if want:
        weight += 0.45
        total += 0.45 * _similarity(want, cand.get("name"))

    css = hints.get("css") or hints.get("selector") or hints.get("fallback")
    if css:
        weight += 0.20
        target = _norm(css)
        got = _norm(cand.get("css"))
        total += 0.20 if (target == got or (got and got.startswith(target))) else 0.0

    base = total / weight if weight else 0.0
    if cand.get("visible"):
        base = min(1.0, base + 0.05)
    return round(base, 3)


async def candidates(page: Any, hints: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """失败时的候选发现：扫页面返回 [{role,name,css,visible,score}]，按分数降序。

    这是自愈省一次 MCP 往返的关键（PLAN 8.5）。page 已关闭时返回 []。
    """
    if page is None or limit <= 0:
        return []

    try:
        raw = await page.evaluate(_COLLECT_JS)
    except Exception:  # noqa: BLE001 - 页面关闭 / 导航中 / 被 sandbox
        return []
    if not isinstance(raw, (list, tuple)):
        return []

    safe_hints = hints if isinstance(hints, dict) else {}
    items: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        cand = {
            "role": str(row.get("role") or ""),
            "name": str(row.get("name") or ""),
            "css": str(row.get("css") or ""),
            "visible": bool(row.get("visible")),
        }
        cand["score"] = _score(safe_hints, cand)
        items.append(cand)

    items.sort(key=lambda d: (d["score"], d["visible"]), reverse=True)
    return items[:limit]


# ---------------------------------------------------------------- 重试


def backoff(i: int, base_ms: int = 1000, cap_ms: int = 10000) -> int:
    """指数退避：base * 2^i，封顶 cap。i 是"第几次重试"（从 0 开始）。"""
    try:
        idx = max(0, int(i))
        base = max(0, int(base_ms))
        cap = max(0, int(cap_ms))
    except (TypeError, ValueError):
        return 0
    if base == 0 or cap == 0:
        return 0
    ms = base * (2 ** min(idx, 30))
    return int(min(cap, ms))


async def with_retry(
    fn: Callable[[], Any],
    attempts: int = 2,
    backoff_ms: int = 1000,
    reload_before: bool = False,
    page: Any = None,
) -> Any:
    """带退避的重试。attempts 含首次，attempts=2 → 最多跑两次。

    reload_before=True：每次**重试**前先 reload（首次不等也不 reload）。
    全部失败则抛出最后一次的原始异常 —— 调用方拿到的还是 LocateError 等
    业务异常，不会被包成 RuntimeError 而丢掉类型。
    """
    if not callable(fn):
        raise MiddlewareError("with_retry 需要可调用对象")

    total = max(1, int(attempts))
    last: BaseException | None = None

    for i in range(total):
        if i > 0:
            wait_ms = backoff(i - 1, base_ms=backoff_ms)
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000)
            if reload_before and page is not None:
                try:
                    await _maybe_await(page.reload())
                except Exception:  # noqa: BLE001 - reload 失败也要继续尝试目标任务
                    pass
        try:
            return await _maybe_await(fn())
        except Exception as exc:  # noqa: BLE001 - 统一收集，最后一次重抛
            last = exc

    assert last is not None
    raise last


# ---------------------------------------------------------------- 拟人


def _bezier(x0: float, y0: float, x1: float, y1: float, steps: int = 8) -> list[tuple[float, float]]:
    """二次贝塞尔采样，控制点偏离直线 15%，轨迹带一点弧度。"""
    dx, dy = x1 - x0, y1 - y0
    cx = (x0 + x1) / 2 - dy * 0.15
    cy = (y0 + y1) / 2 + dx * 0.15
    pts: list[tuple[float, float]] = []
    for i in range(1, max(1, steps) + 1):
        t = i / steps
        u = 1 - t
        pts.append(
            (
                round(u * u * x0 + 2 * u * t * cx + t * t * x1, 2),
                round(u * u * y0 + 2 * u * t * cy + t * t * y1, 2),
            )
        )
    return pts


async def human_click(locator: Any, hold_ms: int = 60) -> None:
    """拟人点击：贝塞尔轨迹移过去 + 按下停留 hold_ms 再抬起。

    失败（拿不到 box / 鼠标接口不可用）回退原生 click()；
    连 click 都失败才抛 LocateError —— 保证节点只需处理一个异常类型。
    """
    try:
        box = await _box(locator)
        if not box:
            raise MiddlewareError("没有 bounding_box，无法按坐标点击")
        page = locator.page
        mouse = page.mouse
        tx = float(box["x"]) + float(box["width"]) / 2
        ty = float(box["y"]) + float(box["height"]) / 2
        sx = tx - 40.0 - (float(box["width"]) % 17)
        sy = ty - 30.0

        for px, py in _bezier(sx, sy, tx, ty):
            await mouse.move(px, py)
        await mouse.move(tx, ty)
        await mouse.down()
        if hold_ms and hold_ms > 0:
            await asyncio.sleep(min(hold_ms, 500) / 1000)
        await mouse.up()
    except Exception:  # noqa: BLE001 - 任何一步不行都退回原生点击
        await _native_click(locator)


async def _native_click(locator: Any) -> None:
    try:
        await locator.click()
    except Exception as exc:  # noqa: BLE001
        raise LocateError(f"点击失败（拟人与原生都试过）: {exc}") from exc
