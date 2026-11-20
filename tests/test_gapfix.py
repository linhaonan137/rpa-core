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
    page.evaluate = AsyncMock(return_value=1)   # 探活必须能 await，否则会被当成死引用

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


async def test_attach_clears_dead_ref_and_uses_recorded_cdp(monkeypatch):
    """_ctx.page 是死引用（标签被关）时：清掉，用记录的 CDP 端点接管，绝不 launch。"""
    dead = MagicMock()
    dead.evaluate = AsyncMock(side_effect=RuntimeError("Target page has been closed"))
    monkeypatch.setattr(mcp_mod._ctx, "page", dead)
    monkeypatch.setattr(mcp_mod, "_SHARED_BROWSER", {})
    monkeypatch.setenv("RPA_CDP", "")
    monkeypatch.setattr(mcp_mod, "_last_cdp", lambda: "http://127.0.0.1:59493")
    # 端口预检要放行：这里测的是"记录端点能用"，不是真实端口
    monkeypatch.setattr(mcp_mod, "_port_open", lambda port: True)
    monkeypatch.setattr(mcp_mod, "_discover_cdp", lambda: "")

    connected_browser = MagicMock()
    connected_browser.contexts = [MagicMock()]

    class _FakeChromium:
        def __init__(self) -> None:
            self.connect_over_cdp = AsyncMock(return_value=connected_browser)

    class _FakePW:
        def __init__(self) -> None:
            self.chromium = _FakeChromium()

        async def start(self) -> "_FakePW":
            return self

    class _FakeAPW:
        def __call__(self) -> "_FakeAPW":
            return self

        async def start(self) -> _FakePW:
            return _FakePW()

    monkeypatch.setattr("playwright.async_api.async_playwright", _FakeAPW)

    async def must_not_launch(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("死引用 + 有端点记录时不该 launch")

    monkeypatch.setattr(web, "_open_browser", must_not_launch)

    ctx = ExecutionContext()
    await mcp_mod._attach_shared_browser(ctx)

    assert mcp_mod._ctx.page is None          # 死引用被清
    assert ctx.browser is connected_browser
    assert getattr(ctx, "_ant_owned", False) is True


async def test_attach_launches_only_when_everything_dead(monkeypatch):
    """死引用 + 无缓存 + 无端点：launch 是最后手段（且必须显式允许）。"""
    monkeypatch.setattr(mcp_mod._ctx, "page", None)
    monkeypatch.setattr(mcp_mod, "_SHARED_BROWSER", {})
    monkeypatch.setenv("RPA_CDP", "")
    monkeypatch.setenv("RPA_ALLOW_LAUNCH", "1")   # 显式允许自建
    monkeypatch.setattr(mcp_mod, "_last_cdp", lambda: "")
    monkeypatch.setattr(mcp_mod, "_discover_cdp", lambda: "")  # 不让本机真浏览器干扰

    new_bctx = MagicMock()

    async def fake_open(headless: bool = True):  # noqa: ANN001
        return MagicMock(), MagicMock(), new_bctx

    monkeypatch.setattr(web, "_open_browser", fake_open)

    ctx = ExecutionContext()
    await mcp_mod._attach_shared_browser(ctx)

    assert ctx.browser_context is new_bctx


async def test_attach_refuses_launch_without_explicit_flag(monkeypatch):
    """默认禁止自建浏览器：接管不可达就报错，绝不静默换成没有登录态的新浏览器。"""
    monkeypatch.setattr(mcp_mod._ctx, "page", None)
    monkeypatch.setattr(mcp_mod, "_SHARED_BROWSER", {})
    monkeypatch.setenv("RPA_CDP", "")
    monkeypatch.delenv("RPA_ALLOW_LAUNCH", raising=False)
    monkeypatch.setattr(mcp_mod, "_last_cdp", lambda: "")
    monkeypatch.setattr(mcp_mod, "_discover_cdp", lambda: "")  # 不让本机真浏览器干扰

    async def must_not_launch(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("未允许时不该 launch")

    monkeypatch.setattr(web, "_open_browser", must_not_launch)

    ctx = ExecutionContext()
    with pytest.raises(RuntimeError, match="RPA_ALLOW_LAUNCH"):
        await mcp_mod._attach_shared_browser(ctx)
