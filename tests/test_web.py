"""网页节点单测：不启真浏览器，全部用假 page / locator。

只验证三件事：参数解析对不对、调用顺序对不对、失败是否返回 Result.fail。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import nodes.web as web
from engine.context import ExecutionContext
from engine.registry import Result


# ---------------------------------------------------------------- 假对象


class FakeLocator:
    """够用的 Locator 替身：每个方法都是 AsyncMock，方便断言调用。"""

    def __init__(
        self,
        count: int = 1,
        items: list | None = None,
        text: str = "元素文本",
        value: str = "输入值",
        visible: bool = True,
        attrs: dict | None = None,
        children: "FakeLocator | None" = None,
    ) -> None:
        self.first = self
        self.count = AsyncMock(return_value=count)
        self.click = AsyncMock()
        self.dblclick = AsyncMock()
        self.hover = AsyncMock()
        self.fill = AsyncMock()
        self.type = AsyncMock()
        self.press_sequentially = AsyncMock()
        self.press = AsyncMock()
        self.check = AsyncMock()
        self.uncheck = AsyncMock()
        self.select_option = AsyncMock()
        self.set_input_files = AsyncMock()
        self.wait_for = AsyncMock()
        self.scroll_into_view_if_needed = AsyncMock()
        self.drag_to = AsyncMock()
        self.web_shot = AsyncMock()
        self.inner_text = AsyncMock(return_value=text)
        self.inner_html = AsyncMock(return_value=f"<b>{text}</b>")
        self.input_value = AsyncMock(return_value=value)
        self.web_visible = AsyncMock(return_value=visible)
        self.get_attribute = AsyncMock(side_effect=lambda n: (attrs or {}).get(n))
        self.evaluate = AsyncMock(return_value=None)
        self.evaluate_all = AsyncMock(return_value=items if items is not None else [])
        self._children = children
        self.locator = MagicMock(side_effect=lambda sel: self._children or FakeLocator(items=[]))
        self.get_by_label = MagicMock(return_value=self)
        self.get_by_placeholder = MagicMock(return_value=self)


class FakeMouse:
    def __init__(self) -> None:
        self.wheel = AsyncMock()
        self.down = AsyncMock()
        self.move = AsyncMock()
        self.up = AsyncMock()


class FakeResponse:
    def __init__(self, body: bytes = b"binary") -> None:
        self.body = AsyncMock(return_value=body)


class FakeRequest:
    def __init__(self, body: bytes = b"binary") -> None:
        self.get = AsyncMock(return_value=FakeResponse(body))


class FakeDownload:
    def __init__(self) -> None:
        self.save_as = AsyncMock()


class FakeAsyncCM:
    """模拟 async with 管理器的返回值对象。"""

    def __init__(self, value=None) -> None:
        self.value = value
        self.__aenter__ = AsyncMock(return_value=self)
        self.__aexit__ = AsyncMock(return_value=None)


class FakeContext:
    def __init__(self, pages: list | None = None, body: bytes = b"binary") -> None:
        self.pages = pages or []
        self.new_page = AsyncMock(side_effect=lambda: FakePage())
        self.request = FakeRequest(body)
        self.add_init_script = AsyncMock()


class FakePage:
    def __init__(
        self,
        url: str = "https://example.com/",
        loc: FakeLocator | None = None,
        title: str = "示例页面",
        content: str = "<html><body>页面示例文本</body></html>",
        ready_state: str = "complete",
        context: FakeContext | None = None,
    ) -> None:
        self.url = url
        self.loc = loc or FakeLocator()
        self.goto = AsyncMock()
        self.web_back = AsyncMock(return_value=None)
        self.web_forward = AsyncMock(return_value=None)
        self.reload = AsyncMock()
        self.close = AsyncMock()
        self.title = AsyncMock(return_value=title)
        self.content = AsyncMock(return_value=content)
        self.evaluate = AsyncMock(return_value=ready_state)
        self.wait_for_load_state = AsyncMock()
        self.web_shot = AsyncMock()
        self.add_script_tag = AsyncMock()
        self.once = MagicMock()
        self.mouse = FakeMouse()
        self.context = context or FakeContext()
        self.locator = MagicMock(return_value=self.loc)
        self.get_by_text = MagicMock(return_value=self.loc)
        self.get_by_label = MagicMock(return_value=self.loc)
        self.get_by_placeholder = MagicMock(return_value=self.loc)
        self.frame_locator = MagicMock(return_value=self)
        self.expect_download = MagicMock(return_value=FakeAsyncCM(FakeDownload()))


def make_ctx(page=None, elements=None) -> ExecutionContext:
    return ExecutionContext(page=page, elements=elements or {})


# ================================================================ 页面

async def test_open_page_reuses_current_page():
    page = FakePage()
    ctx = make_ctx(page)

    r = await web.web_open({"url": "https://a.com"}, ctx)

    assert r.ok is True
    page.goto.assert_awaited_once()
    assert page.goto.await_args.args[0] == "https://a.com"
    assert page.goto.await_args.kwargs["wait_until"] == "load"


async def test_open_page_launches_when_no_page(monkeypatch):
    new_page = FakePage()
    context = FakeContext(pages=[])
    context.new_page = AsyncMock(return_value=new_page)
    browser = MagicMock()
    browser.contexts = []
    browser.new_context = AsyncMock(return_value=context)
    browser.launch_headless = True

    async def fake_launch(headless=True):  # noqa: ANN001
        browser.launch_headless = headless
        return MagicMock(), browser

    monkeypatch.setattr(web, "_launch", fake_launch)
    ctx = make_ctx(None)

    r = await web.web_open({"url": "https://b.com", "kind": "new_tab"}, ctx)

    assert r.ok is True, r.error
    assert ctx.page is new_page
    new_page.goto.assert_awaited_once()


async def test_open_page_missing_url_fails():
    r = await web.web_open({}, make_ctx(FakePage()))
    assert r.ok is False
    assert "url" in r.error


async def test_open_page_timeout_fails():
    page = FakePage()
    page.goto = AsyncMock(side_effect=TimeoutError("Timeout 30000ms exceeded"))
    r = await web.web_open({"url": "https://a.com"}, make_ctx(page))
    assert r.ok is False
    assert "超时" in r.error


async def test_use_opened_page_by_url():
    p1 = FakePage(url="https://a.com/x")
    p2 = FakePage(url="https://b.com/y")
    browser = MagicMock()
    browser.contexts = [FakeContext(pages=[p1, p2])]
    ctx = make_ctx(p1)
    ctx.browser = browser

    r = await web.web_attach({"url": "b.com"}, ctx)

    assert r.ok is True
    assert ctx.page is p2


async def test_use_opened_page_no_match_fails():
    ctx = make_ctx(FakePage(url="https://a.com/"))
    r = await web.web_attach({"url": "zzz.com"}, ctx)
    assert r.ok is False
    assert "未找到" in r.error


async def test_close_page_detaches_handle():
    page = FakePage()
    ctx = make_ctx(page)

    r = await web.web_close({}, ctx)

    assert r.ok is True
    page.close.assert_awaited_once()
    assert ctx.page is None


async def test_switch_tab_by_index():
    p1 = FakePage(url="https://a.com/")
    p2 = FakePage(url="https://b.com/")
    ctx = make_ctx(p1)
    ctx.browser = MagicMock(contexts=[FakeContext(pages=[p1, p2])])

    r = await web.web_tab({"kind": "index", "index": "1", "save_as": "tab"}, ctx)

    assert r.ok is True
    assert ctx.page is p2
    assert ctx.get_var("tab")["index"] == 1


async def test_switch_tab_index_out_of_range_fails():
    ctx = make_ctx(FakePage())
    r = await web.web_tab({"kind": "index", "index": 5}, ctx)
    assert r.ok is False
    assert "越界" in r.error


async def test_switch_iframe_by_index_and_locates_inside_frame():
    page = FakePage()
    ctx = make_ctx(page)

    r = await web.web_frame_in({"kind": "index", "index": 0}, ctx)

    assert r.ok is True
    assert ctx.frames == ["iframe >> nth=0"]

    r2 = await web.web_click({"target": "#ok"}, ctx)
    assert r2.ok is True, r2.error
    page.frame_locator.assert_called_once_with("iframe >> nth=0")


async def test_switch_iframe_by_name():
    ctx = make_ctx(FakePage())
    r = await web.web_frame_in({"kind": "name", "name": "pay"}, ctx)
    assert r.ok is True
    assert ctx.frames == ['iframe[name="pay"]']


async def test_switch_to_main_clears_frames():
    ctx = make_ctx(FakePage())
    ctx.frames = ["iframe >> nth=0", 'iframe[name="x"]']

    r = await web.web_frame_out({}, ctx)

    assert r.ok is True
    assert ctx.frames == []


async def test_go_back_and_forward_and_refresh():
    page = FakePage()
    ctx = make_ctx(page)

    assert (await web.web_back({}, ctx)).ok is True
    assert (await web.web_forward({}, ctx)).ok is True
    assert (await web.web_reload({}, ctx)).ok is True

    page.web_back.assert_awaited_once()
    page.web_forward.assert_awaited_once()
    page.reload.assert_awaited_once()
    assert page.reload.await_args.kwargs["wait_until"] == "load"


# ================================================================ 交互

async def test_click_element_success():
    loc = FakeLocator()
    page = FakePage(loc=loc)

    r = await web.web_click({"target": "#submit"}, make_ctx(page))

    assert r.ok is True, r.error
    loc.wait_for.assert_awaited()  # 可能两次：定位时 attached + 动作前 visible
    loc.click.assert_awaited_once_with(button="left", force=False)
    page.locator.assert_called_with("#submit")


async def test_click_element_not_found_fails():
    page = FakePage(loc=FakeLocator(count=0))
    r = await web.web_click({"target": "#nope"}, make_ctx(page))
    assert r.ok is False
    assert "元素不存在" in r.error


async def test_click_element_timeout_fails():
    loc = FakeLocator()
    loc.wait_for = AsyncMock(side_effect=TimeoutError("Timeout 30000ms exceeded"))
    r = await web.web_click({"target": "#slow", "limit_ms": 10}, make_ctx(FakePage(loc=loc)))
    assert r.ok is False
    assert "超时" in r.error


async def test_click_element_double_and_new_tab():
    loc = FakeLocator()
    page = FakePage(loc=loc)
    ctx = make_ctx(page)
    new_tab = FakePage()

    async def _open_tab(*a, **kw):  # 点击时才多出一个标签页
        page.context.pages.append(new_tab)

    loc.dblclick = AsyncMock(side_effect=_open_tab)

    r = await web.web_click({"target": "#x", "kind": "double", "follow_new_tab": True}, ctx)

    assert r.ok is True
    loc.dblclick.assert_awaited_once()
    assert ctx.page is new_tab


async def test_click_element_el_reference():
    loc = FakeLocator()
    page = FakePage(loc=loc)
    ctx = make_ctx(page, elements={"提交按钮": {"css": "#submit"}})

    r = await web.web_click({"target": "@el:提交按钮"}, ctx)

    assert r.ok is True
    page.locator.assert_called_with("#submit")


async def test_hover_element():
    loc = FakeLocator()
    page = FakePage(loc=loc)
    r = await web.web_hover({"target": "菜单", "hold_ms": 0}, make_ctx(page))
    assert r.ok is True
    loc.hover.assert_awaited_once_with(force=False)
    page.get_by_text.assert_called_with("菜单")


async def test_scroll_page_down_and_element():
    page = FakePage()
    ctx = make_ctx(page)

    assert (await web.web_scroll({"kind": "down", "distance": 300}, ctx)).ok is True
    page.mouse.wheel.assert_awaited_with(0, 300)

    assert (await web.web_scroll({"kind": "element", "target": "#foot"}, ctx)).ok is True
    page.loc.scroll_into_view_if_needed.assert_awaited_once()

    assert (await web.web_scroll({"kind": "top"}, ctx)).ok is True
    page.evaluate.assert_awaited_with("window.scrollTo(0, 0)")


async def test_drag_element_to_target():
    src = FakeLocator()
    dst = FakeLocator()
    page = FakePage(loc=src)
    page.locator = MagicMock(side_effect=lambda sel: dst if sel == "#dst" else src)

    r = await web.web_drag({"target": "#src", "to": "#dst"}, make_ctx(page))

    assert r.ok is True, r.error
    src.drag_to.assert_awaited_once_with(dst)


async def test_drag_element_without_destination_fails():
    r = await web.web_drag({"target": "#src"}, make_ctx(FakePage()))
    assert r.ok is False
    assert "to" in r.error


async def test_input_text_fill_success():
    loc = FakeLocator()
    page = FakePage(loc=loc)
    ctx = make_ctx(page)
    ctx.set_var("name", "张三")

    r = await web.web_type({"target": "#user", "text": "{name}"}, ctx)

    assert r.ok is True, r.error
    loc.fill.assert_awaited_once_with("张三")


async def test_input_text_not_found_fails():
    r = await web.web_type({"target": "#user", "text": "x"}, make_ctx(FakePage(loc=FakeLocator(count=0))))
    assert r.ok is False
    assert "元素不存在" in r.error


async def test_input_text_timeout_fails():
    loc = FakeLocator()
    loc.fill = AsyncMock(side_effect=TimeoutError("Timeout 5000ms exceeded"))
    r = await web.web_type({"target": "#user", "text": "x"}, make_ctx(FakePage(loc=loc)))
    assert r.ok is False
    assert "超时" in r.error


async def test_input_text_type_with_clear():
    loc = FakeLocator()
    page = FakePage(loc=loc)

    r = await web.web_type(
        {"target": "#user", "text": "abc", "kind": "type", "clear": True, "delay_ms": 20}, make_ctx(page)
    )

    assert r.ok is True
    loc.fill.assert_awaited_once_with("")
    loc.type.assert_awaited_once_with("abc", delay=20)


async def test_fill_by_label_uses_label_locator():
    loc = FakeLocator()
    page = FakePage(loc=loc)

    r = await web.web_type_label({"label": "用户名", "value": "李四"}, make_ctx(page))

    assert r.ok is True, r.error
    page.get_by_label.assert_called_with("用户名")
    loc.fill.assert_awaited_once_with("李四")


async def test_fill_by_label_not_found_fails():
    page = FakePage(loc=FakeLocator(count=0))
    r = await web.web_type_label({"label": "不存在的字段", "value": "x"}, make_ctx(page))
    assert r.ok is False
    assert "找不到" in r.error


async def test_fill_form_fills_all_pairs():
    loc = FakeLocator()
    page = FakePage(loc=loc)
    pairs = [{"label": "用户名", "value": "a"}, {"target": "#pwd", "value": "b"}]

    r = await web.web_fill_many({"pairs": pairs}, make_ctx(page))

    assert r.ok is True, r.error
    assert r.data == {"filled": 2, "errors": [], "total": 2}
    assert loc.fill.await_count == 2
    loc.fill.assert_awaited_with("b")


async def test_fill_form_stop_on_fail():
    page = FakePage(loc=FakeLocator(count=0))
    pairs = [{"target": "#a", "value": "1"}, {"target": "#b", "value": "2"}]

    r = await web.web_fill_many({"pairs": pairs, "stop_on_fail": True}, make_ctx(page))

    assert r.ok is False
    assert r.data["filled"] == 0


async def test_select_dropdown_by_value_and_index():
    loc = FakeLocator()
    page = FakePage(loc=loc)
    ctx = make_ctx(page)

    assert (await web.web_pick_option({"target": "#city", "kind": "value", "value": "bj"}, ctx)).ok is True
    loc.select_option.assert_awaited_with(value="bj")

    assert (await web.web_pick_option({"target": "#city", "kind": "index", "value": "2"}, ctx)).ok is True
    loc.select_option.assert_awaited_with(index=2)


async def test_select_dropdown_missing_value_fails():
    r = await web.web_pick_option({"target": "#city", "value": ""}, make_ctx(FakePage()))
    assert r.ok is False
    assert "value" in r.error


async def test_set_checkbox_check_and_uncheck():
    loc = FakeLocator()
    page = FakePage(loc=loc)
    ctx = make_ctx(page)

    assert (await web.web_toggle({"target": "#agree", "checked": True}, ctx)).ok is True
    loc.check.assert_awaited_once()

    assert (await web.web_toggle({"target": "#agree", "checked": False}, ctx)).ok is True
    loc.uncheck.assert_awaited_once()


async def test_upload_file_success(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    loc = FakeLocator()

    r = await web.web_upload({"target": "#file", "path": str(f)}, make_ctx(FakePage(loc=loc)))

    assert r.ok is True, r.error
    loc.set_input_files.assert_awaited_once_with(str(f))


async def test_upload_file_missing_file_fails(tmp_path):
    r = await web.web_upload(
        {"target": "#file", "path": str(tmp_path / "none.txt")}, make_ctx(FakePage())
    )
    assert r.ok is False
    assert "文件不存在" in r.error


# ================================================================ 读取

async def test_get_element_info_text_and_save():
    loc = FakeLocator(text="订单号 123")
    ctx = make_ctx(FakePage(loc=loc))

    r = await web.web_elem_info({"target": "#order", "attr": "text", "save_as": "no"}, ctx)

    assert r.ok is True, r.error
    assert r.data == "订单号 123"
    assert ctx.get_var("no") == "订单号 123"


async def test_get_element_info_html_and_count():
    loc = FakeLocator(text="x")
    page = FakePage(loc=loc)
    ctx = make_ctx(page)

    r = await web.web_elem_info({"target": "#box", "attr": "html"}, ctx)
    assert r.data == "<b>x</b>"

    r2 = await web.web_elem_info({"target": ".item", "attr": "count", "save_as": "n"}, ctx)
    assert r2.data == 1
    assert ctx.get_var("n") == 1


async def test_get_element_info_not_found_fails():
    page = FakePage(loc=FakeLocator(count=0))
    r = await web.web_elem_info({"target": "#none"}, make_ctx(page))
    assert r.ok is False
    assert "元素不存在" in r.error


async def test_get_child_elements():
    children = FakeLocator(items=[{"tag": "li", "text": "a"}, {"tag": "li", "text": "b"}])
    loc = FakeLocator(children=children)
    ctx = make_ctx(FakePage(loc=loc))

    r = await web.web_children({"target": "#list", "child": "li", "save_as": "kids"}, ctx)

    assert r.ok is True, r.error
    assert len(r.data) == 2
    assert ctx.get_var("kids")[1]["text"] == "b"


async def test_get_sibling_elements_next():
    loc = FakeLocator(items=[{"tag": "li", "text": "c"}])
    ctx = make_ctx(FakePage(loc=loc))

    r = await web.web_siblings({"target": "#b", "kind": "next"}, ctx)

    assert r.ok is True, r.error
    js, opt = loc.evaluate_all.await_args.args
    assert opt == {"kind": "next", "includeSelf": False}
    assert "parentElement" in js


async def test_inject_javascript_now_and_on_load():
    page = FakePage()
    page.evaluate = AsyncMock(return_value=7)
    ctx = make_ctx(page)

    r = await web.web_exec({"code": "1+6", "save_as": "r"}, ctx)
    assert r.ok is True
    assert ctx.get_var("r") == 7

    r2 = await web.web_exec({"code": "window.x=1", "kind": "on_load"}, ctx)
    assert r2.ok is True
    page.context.add_init_script.assert_awaited_once_with("window.x=1")


async def test_js_script_saves_result():
    page = FakePage()
    page.evaluate = AsyncMock(return_value={"a": 1})
    ctx = make_ctx(page)

    r = await web.web_js({"code": "() => ({a:1})", "save_as": "out"}, ctx)

    assert r.ok is True
    assert ctx.get_var("out") == {"a": 1}


# ================================================================ 等待与判断

async def test_wait_element_visible_success():
    loc = FakeLocator()
    r = await web.web_wait({"target": "#ready", "kind": "visible"}, make_ctx(FakePage(loc=loc)))
    assert r.ok is True
    loc.wait_for.assert_awaited_once_with(state="visible", timeout=30000)


async def test_wait_element_exist_maps_to_attached():
    loc = FakeLocator()
    r = await web.web_wait({"target": "#ready", "kind": "exist"}, make_ctx(FakePage(loc=loc)))
    assert r.ok is True
    loc.wait_for.assert_awaited_once_with(state="attached", timeout=30000)


async def test_wait_element_timeout_fails():
    loc = FakeLocator()
    loc.wait_for = AsyncMock(side_effect=TimeoutError("Timeout 8000ms exceeded"))
    r = await web.web_wait({"target": "#never", "limit_ms": 8000}, make_ctx(FakePage(loc=loc)))
    assert r.ok is False
    assert "超时" in r.error


async def test_wait_element_bad_kind_fails():
    r = await web.web_wait({"target": "#x", "kind": "什么鬼"}, make_ctx(FakePage()))
    assert r.ok is False
    assert "kind" in r.error


async def test_wait_page_load():
    page = FakePage()
    r = await web.web_wait_load({"kind": "networkidle", "limit_ms": 5000}, make_ctx(page))
    assert r.ok is True
    page.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=5000)


async def test_page_load_complete_true_and_false():
    ctx = make_ctx(FakePage(ready_state="complete"))
    r = await web.web_load_done({"save_as": "loaded"}, ctx)
    assert r.data is True
    assert ctx.get_var("loaded") is True

    ctx2 = make_ctx(FakePage(ready_state="loading"))
    r2 = await web.web_load_done({}, ctx2)
    assert r2.data is False


async def test_element_exists_true_and_false():
    ctx = make_ctx(FakePage(loc=FakeLocator(count=2)))
    r = await web.web_has({"target": ".row", "save_as": "has"}, ctx)
    assert r.ok is True and r.data is True
    assert ctx.get_var("has") is True

    r2 = await web.web_has({"target": ".row"}, make_ctx(FakePage(loc=FakeLocator(count=0))))
    assert r2.ok is True and r2.data is False


async def test_element_visible_true_false_and_absent():
    ctx = make_ctx(FakePage(loc=FakeLocator(visible=True)))
    assert (await web.web_visible({"target": "#a"}, ctx)).data is True

    ctx2 = make_ctx(FakePage(loc=FakeLocator(visible=False)))
    assert (await web.web_visible({"target": "#a"}, ctx2)).data is False

    ctx3 = make_ctx(FakePage(loc=FakeLocator(count=0)))
    assert (await web.web_visible({"target": "#a"}, ctx3)).data is False


async def test_wait_time_and_element():
    ctx = make_ctx(FakePage())
    r = await web.wait({"ms": 0}, ctx)
    assert r.ok is True

    r2 = await web.wait({"kind": "element", "target": "#x", "state": "hidden"}, ctx)
    assert r2.ok is True
    ctx.page.loc.wait_for.assert_awaited_once_with(state="hidden", timeout=30000)


# ================================================================ 截图与下载

async def test_screenshot_page_and_full(tmp_path):
    page = FakePage()
    ctx = make_ctx(page)
    out = tmp_path / "a.png"

    r = await web.web_shot({"path": str(out), "save_as": "img"}, ctx)
    assert r.ok is True
    page.web_shot.assert_awaited_once_with(path=str(out), full_page=False)
    assert ctx.get_var("img") == str(out)

    r2 = await web.web_shot({"kind": "full", "path": str(tmp_path / "b.png")}, ctx)
    assert r2.ok is True
    assert page.web_shot.await_args.kwargs["full_page"] is True


async def test_screenshot_element(tmp_path):
    loc = FakeLocator()
    page = FakePage(loc=loc)
    out = tmp_path / "el.png"

    r = await web.web_shot({"kind": "element", "target": "#chart", "path": str(out)}, make_ctx(page))

    assert r.ok is True
    loc.web_shot.assert_awaited_once_with(path=str(out))


async def test_download_file_by_url(tmp_path):
    page = FakePage()
    page.context.request = FakeRequest(b"payload")
    out = tmp_path / "f.bin"

    r = await web.web_download({"kind": "url", "url": "https://x.com/a", "path": str(out)}, make_ctx(page))

    assert r.ok is True, r.error
    assert out.read_bytes() == b"payload"


async def test_download_file_by_click(tmp_path):
    loc = FakeLocator()
    page = FakePage(loc=loc)
    cm = FakeAsyncCM(FakeDownload())
    page.expect_download = MagicMock(return_value=cm)
    out = tmp_path / "g.pdf"

    r = await web.web_download({"target": "#dl", "path": str(out)}, make_ctx(page))

    assert r.ok is True, r.error
    loc.click.assert_awaited_once()
    cm.value.save_as.assert_awaited_once_with(str(out))


async def test_save_image_downloads_src(tmp_path):
    loc = FakeLocator(attrs={"src": "/img/logo.png"})
    page = FakePage(url="https://e.com/page", loc=loc)
    page.context.request = FakeRequest(b"PNGDATA")
    out = tmp_path / "logo.png"

    r = await web.web_save_img({"target": ".logo", "path": str(out)}, make_ctx(page))

    assert r.ok is True, r.error
    page.context.request.get.assert_awaited_once_with("https://e.com/img/logo.png")
    assert out.read_bytes() == b"PNGDATA"


# ================================================================ 弹窗与校验

async def test_handle_dialog_arms_once_handler():
    page = FakePage()
    ctx = make_ctx(page)

    r = await web.web_dialog({"kind": "accept", "prompt_text": "确定"}, ctx)

    assert r.ok is True
    page.once.assert_called_once()
    event, handler = page.once.call_args.args
    assert event == "dialog"

    dialog = MagicMock()
    dialog.message = "确定要提交吗"
    dialog.accept = AsyncMock()
    await handler(dialog)
    dialog.accept.assert_awaited_once_with("确定")


async def test_handle_dialog_dismiss_and_timeout():
    page = FakePage()
    ctx = make_ctx(page)

    r = await web.web_dialog({"kind": "dismiss", "wait_ms": 20}, ctx)
    assert r.ok is False
    assert "没有出现弹窗" in r.error

    event, handler = page.once.call_args.args
    dialog = MagicMock()
    dialog.message = "x"
    dialog.dismiss = AsyncMock()
    await handler(dialog)
    dialog.dismiss.assert_awaited_once()


async def test_assert_checkpoint_variable_pass_and_fail():
    ctx = make_ctx(FakePage())
    ctx.set_var("n", 5)

    ok = await web.web_assert({"kind": "variable", "left": "{n}", "op": ">", "right": 3}, ctx)
    assert ok.ok is True and ok.data is True

    bad = await web.web_assert(
        {"kind": "variable", "left": "{n}", "op": "==", "right": 9, "message": "数量不对"}, ctx
    )
    assert bad.ok is False
    assert bad.error == "数量不对"
    assert bad.data is False


async def test_assert_checkpoint_element_and_url():
    ctx = make_ctx(FakePage(url="https://a.com/ok", loc=FakeLocator(count=1)))

    r = await web.web_assert({"kind": "element", "target": "#done", "state": "exist"}, ctx)
    assert r.ok is True

    r2 = await web.web_assert({"kind": "url", "expected_text": "/ok"}, ctx)
    assert r2.ok is True

    r3 = await web.web_assert({"kind": "url", "expected_text": "/bad"}, ctx)
    assert r3.ok is False


async def test_snapshot_capture_then_compare():
    loc = FakeLocator(value="张三")
    ctx = make_ctx(FakePage(loc=loc))

    cap = await web.web_snapshot_diff({"targets": ["#name"]}, ctx)
    assert cap.ok is True, cap.error
    assert ctx.get_var("form_snapshot") == {"#name": "张三"}

    same = await web.web_snapshot_diff({"kind": "compare", "targets": ["#name"]}, ctx)
    assert same.ok is True

    loc.input_value = AsyncMock(return_value="李四")
    diff = await web.web_snapshot_diff({"kind": "compare", "targets": ["#name"]}, ctx)
    assert diff.ok is False
    assert "#name" in diff.data


async def test_snapshot_compare_without_baseline_fails():
    r = await web.web_snapshot_diff({"kind": "compare", "targets": ["#a"]}, make_ctx(FakePage()))
    assert r.ok is False
    assert "基线" in r.error


async def test_input_prompt_saves_value(monkeypatch):
    monkeypatch.setattr(web, "_ask_input", AsyncMock(return_value="42"))
    ctx = make_ctx(FakePage())

    r = await web.web_prompt({"name": "count", "message": "几件?", "save_as": "n"}, ctx)

    assert r.ok is True, r.error
    assert ctx.get_var("n") == "42"


async def test_input_prompt_cancel_fails(monkeypatch):
    monkeypatch.setattr(web, "_ask_input", AsyncMock(return_value=None))
    r = await web.web_prompt({"name": "count"}, make_ctx(FakePage()))
    assert r.ok is False
    assert "取消" in r.error


# ================================================================ 通用防线

PAGE_NODES = [
    (web.web_close, {}),
    (web.web_back, {}),
    (web.web_forward, {}),
    (web.web_reload, {}),
    (web.web_click, {"target": "#a"}),
    (web.web_hover, {"target": "#a"}),
    (web.web_scroll, {"kind": "element", "target": "#a"}),
    (web.web_drag, {"target": "#a", "to": "#b"}),
    (web.web_type, {"target": "#a", "text": "x"}),
    (web.web_type_label, {"label": "字段", "value": "x"}),
    (web.web_fill_many, {"pairs": [{"label": "字段", "value": "x"}]}),
    (web.web_pick_option, {"target": "#a", "value": "v"}),
    (web.web_toggle, {"target": "#a"}),
    (web.web_upload, {"target": "#a", "path": "x"}),
    (web.web_elem_info, {"target": "#a"}),
    (web.web_children, {"target": "#a"}),
    (web.web_siblings, {"target": "#a"}),
    (web.web_exec, {"code": "1"}),
    (web.web_js, {"code": "1"}),
    (web.web_wait, {"target": "#a"}),
    (web.web_wait_load, {}),
    (web.web_load_done, {}),
    (web.web_has, {"target": "#a"}),
    (web.web_visible, {"target": "#a"}),
    (web.web_shot, {}),
    (web.web_download, {"target": "#a"}),
    (web.web_save_img, {"target": "#a"}),
    (web.web_dialog, {}),
    (web.web_snapshot_diff, {"targets": ["#a"]}),
    (web.web_frame_in, {}),
    (web.web_frame_out, {}),
]


@pytest.mark.parametrize("fn,args", PAGE_NODES, ids=[f.__name__ for f, _ in PAGE_NODES])
async def test_nodes_fail_without_page(fn, args):
    r = await fn(args, make_ctx(None))
    assert isinstance(r, Result)
    assert r.ok is False
    assert "页面未打开" in (r.error or "")


async def test_switch_tab_without_pages_fails():
    r = await web.web_tab({}, make_ctx(None))
    assert r.ok is False
    assert "没有可切换" in r.error


async def test_undefined_variable_reports_instead_of_crashing():
    ctx = make_ctx(FakePage())
    r = await web.web_type({"target": "#a", "text": "{不存在的变量}"}, ctx)
    assert r.ok is False
    assert "变量" in r.error


async def test_all_web_types_registered():
    from engine.registry import registry

    expected = {
        "web_open", "web_attach", "web_close", "web_tab", "web_frame_in",
        "web_frame_out", "web_back", "web_forward", "web_reload", "web_click",
        "web_hover", "web_scroll", "web_drag", "web_type", "web_type_label",
        "web_fill_many", "web_pick_option", "web_toggle", "web_upload", "web_elem_info",
        "web_children", "web_siblings", "web_exec", "web_js",
        "web_wait", "web_wait_load", "web_load_done", "web_has",
        "web_visible", "wait", "web_shot", "web_download", "web_save_img",
        "web_dialog", "web_assert", "web_snapshot_diff", "web_prompt",
    }
    missing = {t for t in expected if not registry.has(t)}
    assert not missing, f"未注册: {sorted(missing)}"
