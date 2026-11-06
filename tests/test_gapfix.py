"""三个缺口的回归测试：iframe 内 eval / 原生键盘 / flow_run 复用已 attach 浏览器。

背景（2026-11-06 实测）：
  · web_read(kind=eval) 进了 iframe 仍只跑主文档 → 素材选择器里的东西读不到
  · 合成 KeyboardEvent 关不掉 Fusion 浮层 → 必须 CDP 原生按键
  · MCP 服务没设 RPA_CDP，flow_run 会 launch 无登录态浏览器 → 千牛跳登录页
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import api.mcp as mcp_mod
import nodes.web as web
from engine.context import ExecutionContext


class FakeKeyboard:
    def __init__(self) -> None:
        self.press = AsyncMock()
        self.type = AsyncMock()
        self.down = AsyncMock()
        self.up = AsyncMock()


class FakeFrame:
    def __init__(self) -> None:
        self.evaluate = AsyncMock(return_value={"where": "iframe"})


class FakeHandle:
    def __init__(self, frame: FakeFrame) -> None:
        self.content_frame = AsyncMock(return_value=frame)


class FakeLocator:
    def __init__(self, frame: FakeFrame) -> None:
        self.element_handle = AsyncMock(return_value=FakeHandle(frame))


class FakePage:
    def __init__(self) -> None:
        self.keyboard = FakeKeyboard()
        self.evaluate = AsyncMock(return_value={"where": "main"})
        self._frame = FakeFrame()
        self.locator = MagicMock(return_value=FakeLocator(self._frame))


def make_ctx(page: FakePage | None = None) -> ExecutionContext:
    return ExecutionContext(page=page or FakePage(), elements={})


# ---------------------------------------------------------------- A: iframe eval


async def test_web_js_main_document_when_no_frames():
    page = FakePage()
    ctx = make_ctx(page)

    r = await web.web_js({"code": "() => 1"}, ctx)

    assert r.ok is True
    page.evaluate.assert_awaited_once()
    page._frame.evaluate.assert_not_awaited()


async def test_web_js_follows_iframe_chain():
    page = FakePage()
    ctx = make_ctx(page)
    ctx.frames.append("iframe#mainImagesGroup")

    r = await web.web_js({"code": "() => location.href"}, ctx)

    assert r.ok is True
    assert r.data == {"where": "iframe"}
    page._frame.evaluate.assert_awaited_once()
    page.evaluate.assert_not_awaited()
    page.locator.assert_called_once_with("iframe#mainImagesGroup")


async def test_eval_root_falls_back_to_page_when_frame_resolve_fails():
    page = FakePage()
    page.locator = MagicMock(side_effect=RuntimeError("iframe 没了"))
    ctx = make_ctx(page)
    ctx.frames.append("iframe#missing")

    r = await web.web_js({"code": "() => 1"}, ctx)

    assert r.ok is True
    assert r.data == {"where": "main"}
    page.evaluate.assert_awaited_once()


# ---------------------------------------------------------------- B: 原生键盘


async def test_web_key_press():
    page = FakePage()
    r = await web.web_key({"key": "Escape"}, make_ctx(page))

    assert r.ok is True
    page.keyboard.press.assert_awaited_once_with("Escape", delay=0)


async def test_web_key_type_with_delay():
    page = FakePage()
    r = await web.web_key(
        {"kind": "type", "text": "abc", "delay_ms": 60}, make_ctx(page)
    )

    assert r.ok is True
    page.keyboard.type.assert_awaited_once_with("abc", delay=60)


async def test_web_key_down_up():
    page = FakePage()
    ctx = make_ctx(page)
    assert (await web.web_key({"kind": "down", "key": "Shift"}, ctx)).ok is True
    assert (await web.web_key({"kind": "up", "key": "Shift"}, ctx)).ok is True
    page.keyboard.down.assert_awaited_once_with("Shift")
    page.keyboard.up.assert_awaited_once_with("Shift")


async def test_web_key_rejects_bad_args():
    page = FakePage()
    ctx = make_ctx(page)
    assert (await web.web_key({}, ctx)).ok is False                       # 缺 key
    assert (await web.web_key({"kind": "type"}, ctx)).ok is False           # 缺 text
    assert (await web.web_key({"kind": "nope", "key": "A"}, ctx)).ok is False


# ---------------------------------------------------------------- C: 复用已 attach 浏览器


async def test_attach_shared_browser_reuses_ctx_page(monkeypatch):
    """没设 RPA_CDP 时，不许 launch 新浏览器，必须复用 _ctx 已接管的那只。"""
    bctx = MagicMock()
    browser = MagicMock()
    bctx.browser = browser
    page = MagicMock()
    page.context = bctx

    monkeypatch.setattr(mcp_mod._ctx, "page", page)
    monkeypatch.setattr(mcp_mod, "_SHARED_BROWSER", {})
    monkeypatch.setenv("RPA_CDP", "")

    async def must_not_launch(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("不该 launch 新浏览器")

    monkeypatch.setattr(web, "_open_browser", must_not_launch)

    ctx = ExecutionContext()
    await mcp_mod._attach_shared_browser(ctx)

    assert ctx.browser is browser
    assert ctx.browser_context is bctx
    assert mcp_mod._SHARED_BROWSER["context"] is bctx
    assert getattr(ctx, "_ant_owned", False) is True
