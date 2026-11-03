"""middleware 的单元测试：全 mock，不启真浏览器。

约定：
  · FakeLocator 用真实的方法名/属性名（count / web_visible / bounding_box /
    evaluate / click / first / nth / page），方便在测试里一眼看出在测哪条路径
  · 失败路径的 limit_ms 一律给 0，让 deadline 立刻过期 —— 既不睡 30 秒，
    也能保证"检查一次再超时"这条真实逻辑被覆盖
  · 只有需要"等第二轮才成功"的用例才给正数 limit_ms
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.context import ExecutionContext
from engine import middleware as mw
from engine.middleware import (
    LocateError,
    MiddlewareError,
    WaitTimeout,
    backoff,
    candidates,
    describe,
    focus_latest,
    human_click,
    locate,
    pick_visible,
    scroll_find,
    snapshot,
    wait_ready,
    watch_new_tabs,
    with_retry,
)

# ---------------------------------------------------------------- 假对象


_UNSET = object()


class FakeLocator:
    """够用的 locator 替身。page 已关闭 = 让 count 抛异常。"""

    def __init__(
        self,
        count: int = 1,
        visible: bool = True,
        box: dict | None = _UNSET,  # type: ignore[assignment]
        cover: str | None = None,
        page=None,
        closed: bool = False,
        children: list["FakeLocator"] | None = None,
        boxes: list[dict | None] | None = None,
    ) -> None:
        self._count = count
        self._visible = visible
        if box is _UNSET:
            self._box = {"x": 10, "y": 20, "width": 100, "height": 30}
        else:
            self._box = box
        self._boxes = list(boxes) if boxes else None
        self._cover = cover
        self.page = page
        self.closed = closed
        self.children = children or []
        self.clicked = 0

    async def count(self) -> int:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._count

    async def web_visible(self) -> bool:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._visible

    async def bounding_box(self) -> dict | None:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        if self._boxes:
            return self._boxes.pop(0) if len(self._boxes) > 1 else self._boxes[0]
        return self._box

    async def evaluate(self, script: str, *args) -> object:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        if self._cover is None:
            return None
        return self._cover

    async def click(self) -> None:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        self.clicked += 1

    @property
    def first(self) -> "FakeLocator":
        return self.children[0] if self.children else self

    def nth(self, i: int) -> "FakeLocator":
        return self.children[i]


class FakeMouse:
    def __init__(self, fail: bool = False) -> None:
        self.moves: list[tuple[float, float]] = []
        self.downs = 0
        self.ups = 0
        self.fail = fail
        self.wheels: list[int] = []

    async def move(self, x: float, y: float) -> None:
        if self.fail:
            raise RuntimeError("mouse 不可用")
        self.moves.append((x, y))

    async def down(self) -> None:
        if self.fail:
            raise RuntimeError("mouse 不可用")
        self.downs += 1

    async def up(self) -> None:
        if self.fail:
            raise RuntimeError("mouse 不可用")
        self.ups += 1

    async def wheel(self, x: int, y: int) -> None:
        self.wheels.append(y)


class FakeBrowserContext:
    def __init__(self, pages: list | None = None) -> None:
        self.pages = list(pages or [])
        self.listeners: dict[str, object] = {}
        self.removed: list[tuple[str, object]] = []

    def on(self, event: str, fn) -> None:
        self.listeners[event] = fn

    def remove_listener(self, event: str, fn) -> None:
        self.removed.append((event, fn))
        self.listeners.pop(event, None)

    def emit_page(self, page) -> None:
        self.listeners["page"](page)  # type: ignore[operator]


class FakePage:
    def __init__(
        self,
        resolver=None,
        url: str = "https://example.com/order",
        html: str = "<html><body>ok</body></html>",
        context=None,
        screenshot_error: bool = False,
        evaluate=None,
    ) -> None:
        self.resolver = resolver or (lambda kind, value, name=None: FakeLocator(count=0, visible=False))
        self.url = url
        self._html = html
        self.context = context or FakeBrowserContext()
        self.mouse = FakeMouse()
        self.calls: list[tuple[str, str, str | None]] = []
        self.brought = 0
        self.reloads = 0
        self._screenshot_error = screenshot_error
        self._evaluate = evaluate
        self.viewport_size = {"width": 1280, "height": 800}

    # —— 定位接口 ——
    def locator(self, selector: str) -> FakeLocator:
        self.calls.append(("css", selector, None))
        return self.resolver("css", selector, None)

    def get_by_role(self, role: str, name: str | None = None) -> FakeLocator:
        self.calls.append(("role", role, name))
        return self.resolver("role", role, name)

    def get_by_text(self, text: str) -> FakeLocator:
        self.calls.append(("text", text, None))
        return self.resolver("text", text, None)

    def get_by_label(self, text: str) -> FakeLocator:
        self.calls.append(("label", text, None))
        return self.resolver("label", text, None)

    def get_by_placeholder(self, text: str) -> FakeLocator:
        self.calls.append(("placeholder", text, None))
        return self.resolver("placeholder", text, None)

    def get_by_title(self, text: str) -> FakeLocator:
        self.calls.append(("title", text, None))
        return self.resolver("title", text, None)

    def get_by_test_id(self, text: str) -> FakeLocator:
        self.calls.append(("testid", text, None))
        return self.resolver("testid", text, None)

    # —— 页面接口 ——
    async def content(self) -> str:
        return self._html

    async def web_shot(self, path: str = "", full_page: bool = False) -> bytes:
        if self._screenshot_error:
            raise RuntimeError("截图失败：页面已崩溃")
        return b"png"

    async def bring_to_front(self) -> None:
        self.brought += 1

    async def reload(self) -> None:
        self.reloads += 1

    async def evaluate(self, script: str, *args):
        if self._evaluate is None:
            raise RuntimeError("没有配置 evaluate")
        return self._evaluate(script, *args)


class FakeCtxPage:
    """只带 page 属性的最小 ctx，用来验证 focus_latest 会回写 ctx.page。"""

    def __init__(self, page=None) -> None:
        self.page = page


# ---------------------------------------------------------------- locate


async def test_locate_at_el_role_first():
    ctx = ExecutionContext(elements={"submit": {"role": "button", "name": "提交订单", "fallback": "#submit"}})
    target = FakeLocator(count=1)
    page = FakePage(lambda kind, value, name=None: target if kind == "role" else FakeLocator(count=0))

    assert await locate(page, "@el:submit", None, ctx, limit_ms=0) is target
    assert page.calls[0] == ("role", "button", "提交订单")


async def test_locate_at_el_falls_back_to_css():
    ctx = ExecutionContext(elements={"submit": {"role": "button", "name": "提交订单", "fallback": "#submit-btn"}})
    css_hit = FakeLocator(count=1)
    page = FakePage(lambda kind, value, name=None: css_hit if kind == "css" else FakeLocator(count=0))

    loc = await locate(page, "@el:submit", None, ctx, limit_ms=0)

    assert loc is css_hit
    assert ("css", "#submit-btn", None) in page.calls


async def test_locate_at_el_falls_back_to_hints_text():
    ctx = ExecutionContext(
        elements={"submit": {"role": "button", "name": "提交订单", "hints": {"text": "提交订单"}}}
    )
    hit = FakeLocator(count=1)
    page = FakePage(lambda kind, value, name=None: hit if kind == "text" else FakeLocator(count=0))

    loc = await locate(page, "@el:submit", None, ctx, limit_ms=0)

    assert loc is hit
    assert ("text", "提交订单", None) in page.calls


async def test_locate_at_el_string_entry_is_css():
    ctx = ExecutionContext(elements={"submit": "#s"})
    hit = FakeLocator(count=1)
    page = FakePage(lambda kind, value, name=None: hit if value == "#s" else FakeLocator(count=0))

    assert await locate(page, "@el:submit", None, ctx, limit_ms=0) is hit


async def test_locate_text_prefix():
    hit = FakeLocator(count=1)
    page = FakePage(lambda kind, value, name=None: hit if kind == "text" else FakeLocator(count=0))

    assert await locate(page, "text=提交", None, None, limit_ms=0) is hit
    assert page.calls[-1] == ("text", "提交", None)


async def test_locate_css_prefix():
    hit = FakeLocator(count=1)
    page = FakePage(lambda kind, value, name=None: hit if kind == "css" else FakeLocator(count=0))

    assert await locate(page, "css=.btn", None, None, limit_ms=0) is hit
    assert page.calls[-1] == ("css", ".btn", None)


async def test_locate_bare_css():
    hit = FakeLocator(count=1)
    page = FakePage(lambda kind, value, name=None: hit if kind == "css" else FakeLocator(count=0))

    assert await locate(page, "#submit", None, None, limit_ms=0) is hit
    assert page.calls[-1] == ("css", "#submit", None)


async def test_locate_xpath_prefix():
    hit = FakeLocator(count=1)
    page = FakePage(
        lambda kind, value, name=None: hit if str(value).startswith("xpath=") else FakeLocator(count=0)
    )

    assert await locate(page, "xpath=//div[@id='a']", None, None, limit_ms=0) is hit
    assert page.calls[-1] == ("css", "xpath=//div[@id='a']", None)


async def test_locate_unknown_element_name_raises():
    ctx = ExecutionContext(elements={"submit": {"role": "button"}})
    page = FakePage()

    with pytest.raises(LocateError) as ei:
        await locate(page, "@el:pay", None, ctx, limit_ms=0)
    assert "元素库无此项" in str(ei.value)
    assert ei.value.target == "@el:pay"


async def test_locate_all_strategies_fail_raises_with_tried():
    ctx = ExecutionContext(
        elements={"submit": {"role": "button", "name": "提交订单", "fallback": "#s", "hints": {"text": "去支付"}}}
    )
    page = FakePage()  # 所有 resolver 都返回 count=0

    with pytest.raises(LocateError) as ei:
        await locate(page, "@el:submit", None, ctx, limit_ms=0)
    assert len(ei.value.tried) >= 3
    assert "都失效" in str(ei.value)


async def test_locate_page_none_raises():
    with pytest.raises(LocateError):
        await locate(None, "#a", None, None)


async def test_locate_empty_target_raises():
    with pytest.raises(LocateError):
        await locate(FakePage(), "   ", None, None)


async def test_locate_hints_argument_overrides_entry():
    ctx = ExecutionContext(elements={"submit": {"role": "button", "name": "旧名字"}})
    hit = FakeLocator(count=1)
    page = FakePage(lambda kind, value, name=None: hit if kind == "text" else FakeLocator(count=0))

    assert await locate(page, "@el:submit", {"text": "新名字"}, ctx, limit_ms=0) is hit


async def test_locate_bad_selector_raises_locate_error():
    class BadPage(FakePage):
        def locator(self, selector: str):
            raise ValueError("非法选择器")

    with pytest.raises(LocateError):
        await locate(BadPage(), "div:::bad", None, None, limit_ms=0)


# ---------------------------------------------------------------- wait_ready


async def test_wait_ready_visible_ok():
    assert await wait_ready(FakeLocator(count=1, visible=True), "visible", limit_ms=1000) is None


async def test_wait_ready_visible_timeout():
    with pytest.raises(WaitTimeout) as ei:
        await wait_ready(FakeLocator(count=1, visible=False), "visible", limit_ms=0)
    assert ei.value.state == "visible"


async def test_wait_ready_attached_ignores_visibility():
    assert await wait_ready(FakeLocator(count=3, visible=False), "attached", limit_ms=0) is None


async def test_wait_ready_exist_alias():
    assert await wait_ready(FakeLocator(count=1, visible=False), "exist", limit_ms=0) is None


async def test_wait_ready_hidden_when_absent():
    assert await wait_ready(FakeLocator(count=0), "hidden", limit_ms=0) is None


async def test_wait_ready_stable_needs_two_frames():
    box = {"x": 1, "y": 2, "width": 10, "height": 10}
    loc = FakeLocator(count=1, visible=True, box=box)
    assert await wait_ready(loc, "stable", limit_ms=2000) is None


async def test_wait_ready_stable_timeout_when_never_settles():
    boxes = [
        {"x": 0, "y": 0, "width": 10, "height": 10},
        {"x": 90, "y": 90, "width": 10, "height": 10},
    ]
    loc = FakeLocator(count=1, visible=True, boxes=boxes)
    with pytest.raises(WaitTimeout):
        await wait_ready(loc, "stable", limit_ms=0)


async def test_wait_ready_unknown_state():
    with pytest.raises(WaitTimeout) as ei:
        await wait_ready(FakeLocator(), "blinking", limit_ms=0)
    assert "未知的等待状态" in str(ei.value)


async def test_wait_ready_none_locator():
    with pytest.raises(WaitTimeout):
        await wait_ready(None, "visible", limit_ms=0)


# ---------------------------------------------------------------- pick_visible


async def test_pick_visible_chooses_visible_one():
    hidden = FakeLocator(count=1, visible=False)
    shown = FakeLocator(count=1, visible=True)
    multi = FakeLocator(count=2, children=[hidden, shown])

    assert await pick_visible(multi) is shown


async def test_pick_visible_falls_back_to_first():
    a = FakeLocator(count=1, visible=False)
    b = FakeLocator(count=1, visible=False)
    multi = FakeLocator(count=2, children=[a, b])

    assert await pick_visible(multi) is a


async def test_pick_visible_empty_raises():
    with pytest.raises(LocateError):
        await pick_visible(FakeLocator(count=0))


# ---------------------------------------------------------------- scroll_find


async def test_scroll_find_hits_after_scroll():
    state = {"y": 0}
    found = FakeLocator(count=1)

    def evaluate(script: str, *args):
        if "scrollBy" in script:
            state["y"] += args[0]
            return None
        if "innerHeight" in script:
            return 800
        return state["y"]

    page = FakePage(evaluate=evaluate)
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return found if calls["n"] >= 2 else FakeLocator(count=0)

    assert await scroll_find(page, factory, limit_ms=2000) is found
    assert state["y"] > 0


async def test_scroll_find_returns_none_at_bottom():
    state = {"y": 5000}

    def evaluate(script: str, *args):
        if "scrollBy" in script:
            return None  # 滚不动
        return state["y"]

    page = FakePage(evaluate=evaluate)

    assert await scroll_find(page, lambda: FakeLocator(count=0), limit_ms=2000) is None


async def test_scroll_find_page_none_raises():
    with pytest.raises(LocateError):
        await scroll_find(None, lambda: FakeLocator())


async def test_scroll_find_factory_errors_are_swallowed():
    state = {"y": 0, "n": 0}

    def evaluate(script: str, *args):
        if "scrollBy" in script:
            state["n"] += 1
            state["y"] += args[0]
            return None
        return state["y"]

    page = FakePage(evaluate=evaluate)

    def factory():
        if state["n"] < 1:
            raise LocateError("还不在 DOM 里")
        return FakeLocator(count=1)

    assert await scroll_find(page, factory, limit_ms=2000) is not None


# ---------------------------------------------------------------- 页面聚焦


async def test_focus_latest_switches_to_newest_tab():
    old = FakePage()
    new = FakePage()
    ctx_page = FakeCtxPage(page=old)
    old.context = FakeBrowserContext(pages=[old, new])

    await focus_latest(old, ctx_page)

    assert ctx_page.page is new
    assert new.brought == 1


async def test_focus_latest_no_pages_is_silent():
    page = FakePage()
    page.context = FakeBrowserContext(pages=[])
    ctx_page = FakeCtxPage(page=page)

    await focus_latest(page, ctx_page)

    assert ctx_page.page is page


async def test_watch_new_tabs_collects_and_detaches():
    page = FakePage()
    watcher = watch_new_tabs(page)
    new_tab = FakePage()

    with watcher as w:
        assert w.attached is True
        page.context.emit_page(new_tab)
        assert w.pages == [new_tab]
        assert w.latest is new_tab

    assert watcher.attached is False
    assert page.context.removed, "退出时必须摘掉监听"


async def test_watch_new_tabs_detaches_on_exception():
    page = FakePage()
    with pytest.raises(ValueError):
        with watch_new_tabs(page):
            raise ValueError("块里炸了")

    assert page.context.removed


async def test_watch_new_tabs_async_context():
    page = FakePage()
    new_tab = FakePage()

    async with watch_new_tabs(page) as w:
        page.context.emit_page(new_tab)

    assert w.pages == [new_tab]
    assert page.context.removed


async def test_watch_new_tabs_without_context():
    class NoCtx:
        @property
        def context(self):
            raise RuntimeError("no context")

    w = watch_new_tabs(NoCtx())
    with w:
        assert w.attached is False
    assert w.pages == []


# ---------------------------------------------------------------- describe


async def test_describe_normal():
    loc = FakeLocator(count=2, visible=True, cover="div#mask")

    info = await describe(loc)

    assert info == {"exists": True, "visible": True, "count": 2, "covered_by": "div#mask"}


async def test_describe_page_closed_returns_defaults():
    loc = FakeLocator(count=1, visible=True, closed=True)

    assert await describe(loc) == {
        "exists": False,
        "visible": False,
        "count": 0,
        "covered_by": None,
    }


async def test_describe_not_found():
    info = await describe(FakeLocator(count=0))

    assert info["exists"] is False
    assert info["count"] == 0
    assert info["covered_by"] is None


async def test_describe_none_locator():
    assert await describe(None) == {"exists": False, "visible": False, "count": 0, "covered_by": None}


async def test_describe_hidden_element_skips_cover_check():
    loc = FakeLocator(count=1, visible=False, cover="div#mask")

    info = await describe(loc)

    assert info["exists"] is True
    assert info["visible"] is False
    assert info["covered_by"] is None


# ---------------------------------------------------------------- snapshot


async def test_snapshot_writes_files(tmp_path):
    ctx = ExecutionContext(page=None, run_id="r1")
    ctx.set_var("order_id", "A100", scope="global")
    ctx.last_error = {"node": "n5", "args": {"selector": "#submit"}}
    page = FakePage()

    info = await snapshot(ctx, page, "n5", "Timeout 30000ms", save_dir=str(tmp_path))

    assert info["node_id"] == "n5"
    assert info["url"] == "https://example.com/order"
    assert info["vars"]["order_id"] == "A100"
    assert info["args"] == {"selector": "#submit"}
    assert info["stack"]
    assert info["web_shot"].endswith("web_shot.png")
    assert Path(info["html"]).exists()
    assert Path(info["meta"]).exists()
    assert info["warnings"] == []

    meta = json.loads(Path(info["meta"]).read_text(encoding="utf-8"))
    assert meta["error"].startswith("Timeout")
    assert meta["node_id"] == "n5"


async def test_snapshot_dir_failure_does_not_raise(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file", encoding="utf-8")
    ctx = ExecutionContext(run_id="r2")

    info = await snapshot(ctx, FakePage(), "n1", "boom", save_dir=str(blocker / "sub"))

    assert info["warnings"], "建目录失败必须落在 warnings 里"
    assert info["web_shot"] is None
    assert info["error"] == "boom"


async def test_snapshot_screenshot_error_only_warns(tmp_path):
    ctx = ExecutionContext(run_id="r3")
    page = FakePage(screenshot_error=True)

    info = await snapshot(ctx, page, "n2", "boom", save_dir=str(tmp_path))

    assert any("截图失败" in w for w in info["warnings"])
    assert Path(info["meta"]).exists()


async def test_snapshot_without_page(tmp_path):
    ctx = ExecutionContext(run_id="r4")

    info = await snapshot(ctx, None, "n3", "no page", save_dir=str(tmp_path))

    assert info["url"] is None
    assert info["html"] is None
    assert Path(info["meta"]).exists()


async def test_snapshot_truncates_huge_vars(tmp_path):
    ctx = ExecutionContext(run_id="r5")
    ctx.set_var("blob", "x" * 2000, scope="global")

    info = await snapshot(ctx, FakePage(), "n4", "boom", save_dir=str(tmp_path))

    assert "chars)" in info["vars"]["blob"]


# ---------------------------------------------------------------- candidates


def _cand_resolver(rows):
    return lambda script, *args: rows


async def test_candidates_sorted_by_score_and_limited():
    rows = [
        {"role": "button", "name": "取消", "css": "button.cancel", "visible": True},
        {"role": "button", "name": "提交订单", "css": "button.submit", "visible": True},
        {"role": "link", "name": "帮助", "css": "a.help", "visible": False},
    ]
    page = FakePage(evaluate=_cand_resolver(rows))

    got = await candidates(page, {"role": "button", "name": "提交订单"}, limit=2)

    assert len(got) == 2
    assert got[0]["name"] == "提交订单"
    assert got[0]["score"] > got[1]["score"]
    assert set(got[0]) == {"role", "name", "css", "visible", "score"}


async def test_candidates_page_closed_returns_empty():
    def boom(script, *args):
        raise RuntimeError("Target page has been closed")

    page = FakePage(evaluate=boom)

    assert await candidates(page, {"name": "提交"}, limit=5) == []


async def test_candidates_limit_zero_returns_empty():
    page = FakePage(evaluate=_cand_resolver([]))

    assert await candidates(page, {}, limit=0) == []


async def test_candidates_ignores_non_dict_rows():
    page = FakePage(evaluate=_cand_resolver(["nonsense", 42]))

    assert await candidates(page, {"name": "x"}, limit=5) == []


async def test_candidates_empty_hints_still_returns_rows():
    rows = [{"role": "button", "name": "A", "css": "#a", "visible": True}]
    page = FakePage(evaluate=_cand_resolver(rows))

    got = await candidates(page, {}, limit=3)

    assert len(got) == 1
    assert got[0]["score"] == 0.05  # 无线索时只剩"可见"加成


# ---------------------------------------------------------------- 重试


async def test_with_retry_succeeds_on_second_try():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise LocateError("还没出现")
        return "ok"

    assert await with_retry(flaky, attempts=3, backoff_ms=1) == "ok"
    assert calls["n"] == 2


async def test_with_retry_exhausted_raises_original():
    async def always_fail():
        raise WaitTimeout("永远等不到", state="visible", ms=10)

    with pytest.raises(WaitTimeout):
        await with_retry(always_fail, attempts=2, backoff_ms=1)


async def test_with_retry_reloads_before_retry():
    page = FakePage()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise LocateError("nope")
        return True

    assert await with_retry(flaky, attempts=2, backoff_ms=1, reload_before=True, page=page)
    assert page.reloads == 1


async def test_with_retry_no_reload_when_disabled():
    page = FakePage()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise LocateError("nope")
        return True

    await with_retry(flaky, attempts=2, backoff_ms=1, reload_before=False, page=page)
    assert page.reloads == 0


async def test_with_retry_attempts_zero_still_runs_once():
    calls = {"n": 0}

    async def once():
        calls["n"] += 1
        return "done"

    assert await with_retry(once, attempts=0, backoff_ms=0) == "done"
    assert calls["n"] == 1


async def test_with_retry_requires_callable():
    with pytest.raises(MiddlewareError):
        await with_retry(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- backoff


def test_backoff_exponential():
    assert [backoff(i) for i in range(4)] == [1000, 2000, 4000, 8000]


def test_backoff_capped():
    assert backoff(10, base_ms=1000, cap_ms=10000) == 10000


def test_backoff_zero_base():
    assert backoff(3, base_ms=0) == 0
    assert backoff(3, base_ms=1000, cap_ms=0) == 0


def test_backoff_negative_index_treated_as_zero():
    assert backoff(-5, base_ms=100) == 100


def test_backoff_huge_index_no_overflow():
    assert backoff(9999, base_ms=1000, cap_ms=10000) == 10000


# ---------------------------------------------------------------- 拟人点击


async def test_human_click_moves_and_holds():
    page = FakePage()
    loc = FakeLocator(count=1, visible=True, page=page)

    await human_click(loc, hold_ms=0)

    assert len(page.mouse.moves) >= 9  # 贝塞尔 8 个采样 + 末端对齐
    assert page.mouse.downs == 1
    assert page.mouse.ups == 1
    assert loc.clicked == 0  # 走的是坐标点击，不是原生 click


async def test_human_click_falls_back_when_no_box():
    page = FakePage()
    loc = FakeLocator(count=1, page=page, box=None)

    await human_click(loc, hold_ms=0)

    assert loc.clicked == 1
    assert page.mouse.downs == 0


async def test_human_click_falls_back_when_mouse_broken():
    page = FakePage()
    page.mouse = FakeMouse(fail=True)
    loc = FakeLocator(count=1, page=page)

    await human_click(loc, hold_ms=0)

    assert loc.clicked == 1


async def test_human_click_raises_locate_error_when_both_fail():
    page = FakePage()
    page.mouse = FakeMouse(fail=True)
    loc = FakeLocator(count=1, page=page, closed=True)

    with pytest.raises(LocateError):
        await human_click(loc, hold_ms=0)


async def test_human_click_endpoint_is_center():
    page = FakePage()
    loc = FakeLocator(count=1, page=page, box={"x": 100, "y": 50, "width": 40, "height": 20})

    await human_click(loc, hold_ms=0)

    assert page.mouse.moves[-1] == (120.0, 60.0)
