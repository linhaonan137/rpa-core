"""middleware 的测试：拟人点击（贝塞尔轨迹 + 回退链）。

元素定位在 nodes/web.py，不在这里测。
"""

from __future__ import annotations

import pytest

from engine.middleware import LocateError, MiddlewareError, _bezier, human_click

_UNSET = object()


class FakeLocator:
    """够用的 locator 替身。closed=True 让所有操作抛异常。"""

    def __init__(
        self,
        count: int = 1,
        visible: bool = True,
        box: dict | None = _UNSET,  # type: ignore[assignment]
        page=None,
        closed: bool = False,
    ) -> None:
        self._count = count
        self._visible = visible
        self._box = {"x": 10, "y": 20, "width": 100, "height": 30} if box is _UNSET else box
        self.page = page
        self.closed = closed
        self.clicked = 0

    async def count(self) -> int:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._count

    async def bounding_box(self) -> dict | None:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._box

    async def click(self) -> None:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        self.clicked += 1

    @property
    def first(self) -> "FakeLocator":
        return self


class FakeMouse:
    def __init__(self, fail: bool = False) -> None:
        self.moves: list[tuple[float, float]] = []
        self.downs = 0
        self.ups = 0
        self.fail = fail

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


class FakePage:
    def __init__(self) -> None:
        self.mouse = FakeMouse()


# ---------------------------------------------------------------- 贝塞尔


def test_bezier_endpoint_is_target():
    pts = _bezier(0, 0, 100, 100)
    assert pts[-1] == (100.0, 100.0)


def test_bezier_has_arc():
    """控制点偏离直线，中点不该落在直线上。"""
    pts = _bezier(0, 0, 100, 0)
    mid = pts[len(pts) // 2]
    assert mid[1] != 0


# ---------------------------------------------------------------- 拟人点击


async def test_human_click_moves_and_holds():
    page = FakePage()
    loc = FakeLocator(count=1, page=page)

    await human_click(loc, hold_ms=0)

    assert len(page.mouse.moves) >= 9  # 贝塞尔 8 个采样 + 末端对齐
    assert page.mouse.downs == 1
    assert page.mouse.ups == 1
    assert loc.clicked == 0  # 走的是坐标点击，不是原生 click


async def test_human_click_endpoint_is_center():
    page = FakePage()
    loc = FakeLocator(count=1, page=page, box={"x": 100, "y": 50, "width": 40, "height": 20})

    await human_click(loc, hold_ms=0)

    assert page.mouse.moves[-1] == (120.0, 60.0)


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


def test_errors_are_hierarchy():
    """LocateError 必须能被 MiddlewareError 捕获（调用方只处理一种）。"""
    assert issubclass(LocateError, MiddlewareError)
