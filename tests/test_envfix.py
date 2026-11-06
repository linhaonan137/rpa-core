"""环境/状态管理修复的回归测试（2026-11-06）。

修的是这几类"每次起跑环境不一样"的问题：
  · 页面淘汰按最老关 → 会关掉用户/别的进程正在用的标签
  · ctx.page 为空时复用 pages[0] → 从上次失败残留的脏页开始
  · web_close(kind=all) 不区分浏览器归属 → 把用户接管的 Ant 关掉
  · web_tab 首个命中即用 → 切到历史同名标签
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import nodes.web as web
from engine.context import ExecutionContext


class _Page:
    def __init__(self, title: str = "页面", url: str = "https://x/") -> None:
        self._title = title
        self.url = url
        self.closed = False

    async def title(self) -> str:
        return self._title

    async def close(self) -> None:
        self.closed = True


def _ctx_with_pages(pages: list, current=None, ant_owned: bool = False):
    """造一个带 browser/context 的 ctx，_all_pages 能枚举到 pages。"""
    ctx = ExecutionContext(page=current or pages[0], elements={})
    browser = MagicMock()
    browser.close = AsyncMock()
    context = type("C", (), {"pages": pages})()
    browser.contexts = [context]
    ctx.browser = browser
    ctx.browser_context = context
    pw = MagicMock()
    pw.stop = AsyncMock()
    ctx._playwright = pw
    ctx._ant_owned = ant_owned
    return ctx


# ---------------------------------------------------------------- 页面归属


async def test_close_all_keeps_ant_browser(monkeypatch):
    """接管来的浏览器：只关自己 new_page 出来的页，绝不关浏览器。"""
    owned, user = _Page("自有页"), _Page("用户页")
    ctx = _ctx_with_pages([owned, user], current=owned, ant_owned=True)

    monkeypatch.setattr(web, "_OWNED_PAGES", {id(owned)})
    r = await web.web_close({"kind": "all"}, ctx)

    assert r.ok is True
    assert owned.closed is True
    assert user.closed is False                 # 用户的标签页不动
    ctx.browser.close.assert_not_awaited()      # 不关浏览器
    ctx._playwright.stop.assert_not_awaited()   # 不停 playwright


async def test_close_all_closes_self_launched_browser():
    """自己 launch 的浏览器：照旧全部关掉（原有行为不能退化）。"""
    a, b = _Page(), _Page()
    ctx = _ctx_with_pages([a, b], current=a, ant_owned=False)
    browser, pw = ctx.browser, ctx._playwright   # 收尾会把 ctx 上的引用置 None

    r = await web.web_close({"kind": "all"}, ctx)

    assert r.ok is True
    assert a.closed and b.closed
    browser.close.assert_awaited()
    pw.stop.assert_awaited()


# ---------------------------------------------------------------- 切标签偏好


async def test_web_tab_prefers_latest_match(monkeypatch):
    """同名标签有多个时取最新的那个，而不是第一个（历史残留标签）。"""
    old = _Page("淘宝网 - 商品发布")
    cur = _Page("我的商品")
    new = _Page("淘宝网 - 商品发布")
    ctx = _ctx_with_pages([old, cur, new], current=cur)

    r = await web.web_tab({"kind": "title", "title": "商品发布"}, ctx)

    assert r.ok is True
    assert ctx.page is new          # 最新，不是 old


async def test_web_tab_can_still_pick_first(monkeypatch):
    old = _Page("淘宝网 - 商品发布")
    new = _Page("淘宝网 - 商品发布")
    ctx = _ctx_with_pages([old, new], current=old)

    r = await web.web_tab({"kind": "title", "title": "商品发布", "prefer": "first"}, ctx)

    assert r.ok is True
    assert ctx.page is old


# ---------------------------------------------------------------- 干净起点


async def test_ensure_page_opens_fresh_page_instead_of_reusing_pages0(monkeypatch):
    """ctx.page 为空时新开一页，绝不复用 context.pages[0]（可能是脏页）。"""
    dirty = _Page("脏页")
    fresh = _Page("新页")

    ctx = ExecutionContext(page=None, elements={})
    context = MagicMock()
    context.pages = [dirty]
    context.new_page = AsyncMock(return_value=fresh)
    ctx.browser_context = context

    page = await web._ensure_page(ctx, new_tab=False)

    assert page is fresh
    assert page is not dirty
    context.new_page.assert_awaited_once()
    assert ctx.page is fresh


# ---------------------------------------------------------------- 跨进程浏览器锁


async def test_browser_lock_is_exclusive_and_releases(monkeypatch, tmp_path):
    """同一时刻只允许一个持有者；超时抛 BrowserBusy；释放后可再次获取。"""
    from engine import browser_lock as bl

    monkeypatch.setattr(bl, "_lock_path", lambda: tmp_path / ".browser.lock")
    monkeypatch.delenv("RPA_BROWSER_LOCK", raising=False)

    holder = bl.browser_lock(timeout_s=2)
    async with holder:
        with pytest.raises(bl.BrowserBusy):
            async with bl.browser_lock(timeout_s=0.2, poll_s=0.05):
                pass

    # 释放后能重新拿到
    async with bl.browser_lock(timeout_s=2):
        pass


async def test_browser_lock_can_be_disabled(monkeypatch, tmp_path):
    """RPA_BROWSER_LOCK=off 时不做互斥（单进程调试用）。"""
    from engine import browser_lock as bl

    monkeypatch.setattr(bl, "_lock_path", lambda: tmp_path / ".browser.lock")
    monkeypatch.setenv("RPA_BROWSER_LOCK", "off")

    async with bl.browser_lock():
        async with bl.browser_lock():   # 关掉后嵌套也不报错
            pass


# ---------------------------------------------------------------- 端点不被污染


async def test_attach_marks_reused_so_caller_does_not_persist_cdp():
    """已连接时 attach 只标 reused，不重复 connect（调用方据此不写端点文件）。"""
    ctx = ExecutionContext(page=None, elements={})
    ctx.browser = MagicMock()
    page = _Page("当前页")
    ctx.page = page

    got = await web._attach_cdp(ctx, "http://127.0.0.1:1")

    assert got is page
    assert getattr(ctx, "_cdp_reused", False) is True
