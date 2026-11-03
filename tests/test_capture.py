"""元素拾取器测试：校验、转换、落库（mock，不启浏览器）。"""

from __future__ import annotations

import pytest

from elements.capture import JS_CAPTURE, capture, raw_to_entry, validate_raw
from elements.store import ElementError, ElementStore


RAW = {
    "role": "button",
    "name": "提交订单",
    "attrs": {"id": "submit", "data-testid": "go", "class": "btn"},
    "path": ["form#f", "div.actions", "button:nth-child(1)"],
    "siblings": ["取消", "保存草稿"],
    "frame": [],
    "geometry": {"x": 820, "y": 640, "w": 120, "h": 36,
                 "scrollX": 0, "scrollY": 120, "viewportW": 1440, "viewportH": 900},
}


# ------------------------------------------------------------ validate_raw


def test_validate_ok():
    validate_raw(RAW)  # 不抛


def test_validate_no_role_no_name():
    with pytest.raises(ElementError, match="至少一个非空"):
        validate_raw({"attrs": {}, "path": ["div"]})


def test_validate_no_path():
    with pytest.raises(ElementError, match="path 为空"):
        validate_raw({"role": "button", "name": "x", "path": []})


def test_validate_not_dict():
    with pytest.raises(ElementError, match="不是对象"):
        validate_raw("oops")


# ------------------------------------------------------------ raw_to_entry


def test_raw_to_entry_full():
    e = raw_to_entry(RAW, "submit_order")
    assert e.key == "submit_order"
    assert e.role == "button"
    assert e.name == "提交订单"
    assert e.fingerprint.tag == "button"
    assert e.fingerprint.path[-1] == "button:nth-child(1)"
    assert e.fingerprint.geometry.y == 640
    assert e.fingerprint.geometry.scroll_y == 120
    assert e.meta["source"] == "picker"
    e.validate()  # 落库校验也过


def test_raw_to_entry_tag_from_path():
    raw = dict(RAW, path=["div#box", "a:nth-child(2)"], role="link")
    e = raw_to_entry(raw, "link1")
    assert e.fingerprint.tag == "a"


# ------------------------------------------------------------ capture（mock）


class FakeLocator:
    def __init__(self, raw):
        self._raw = raw
        self.calls: list[str] = []

    async def evaluate(self, js, *args, **kw):
        self.calls.append(js)
        if args:  # frame 链注入那次没有返回值
            return None
        return self._raw

    @property
    def first(self):
        return self


def _last_eval_is_capture(loc):
    """抓取脚本必须是最后一次 evaluate（之前可能有 frame 注入）。"""
    assert loc.calls, "没有执行抓取脚本"
    assert loc.calls[-1] == JS_CAPTURE, "必须用统一的抓取脚本"


class FakePage:
    def __init__(self, raw):
        self._raw = raw
        self.selector = None
        self.last_loc = None

    def locator(self, selector):
        self.selector = selector
        self.last_loc = FakeLocator(self._raw)
        return self.last_loc


@pytest.mark.asyncio
async def test_capture_roundtrip(tmp_path):
    store = ElementStore(str(tmp_path / "el.json"))
    page = FakePage(RAW)

    entry = await capture(page, "#submit", "submit_order", store=store)
    _last_eval_is_capture(page.last_loc)

    assert entry.key == "submit_order"
    assert store.has("submit_order")
    # 落盘了
    assert (tmp_path / "el.json").exists()
    # 选择器透传
    assert page.selector == "#submit"


@pytest.mark.asyncio
async def test_capture_overwrite_existing(tmp_path):
    store = ElementStore(str(tmp_path / "el.json"))
    page = FakePage(RAW)
    await capture(page, "#submit", "k", store=store)
    await capture(page, "#submit", "k", store=store)  # 二次拾取=覆盖
    assert len(store) == 1


@pytest.mark.asyncio
async def test_capture_bad_key_rejected(tmp_path):
    with pytest.raises(ElementError, match="非法元素 key"):
        await capture(FakePage(RAW), "#x", "有 空格 的 key", store=None)


@pytest.mark.asyncio
async def test_capture_frame_chain_recorded(tmp_path):
    store = ElementStore(str(tmp_path / "el.json"))
    chain = [{"selector": "iframe#pay"}]
    page = FakePage(RAW)
    entry = await capture(page, "#submit", "in_frame",
                          store=store, frame_chain=chain)
    assert entry.fingerprint.frame == chain
    _last_eval_is_capture(page.last_loc)  # 先注入 frame 链，最后仍是抓取脚本


@pytest.mark.asyncio
async def test_capture_unpickable_element_fails(tmp_path):
    """抓回来 role/name 全空 → 当场报错，不产出坏档案。"""
    bad = dict(RAW, role=None, name="")
    with pytest.raises(ElementError, match="至少一个非空"):
        await capture(FakePage(bad), "#x", "k", store=ElementStore())
