"""拟人点击：贝塞尔轨迹 + 按下停留。

唯一的生产入口是 `human_click`（`web_click(mode="human")` 用）。
元素定位在 `nodes/web.py:_locate`，不在这里。
"""

from __future__ import annotations

import asyncio
from typing import Any


class MiddlewareError(Exception):
    """中间件内部错误。"""


class LocateError(MiddlewareError):
    """定位或点击彻底失败。"""


def _first(locator: Any) -> Any:
    """取多匹配定位器里的第一个；没有 .first 就原样返回。"""
    try:
        return locator.first
    except Exception:  # noqa: BLE001 - 已关闭的 locator 取 first 会炸
        return locator


async def _box(locator: Any) -> dict[str, float] | None:
    if locator is None:
        return None
    try:
        box = await _first(locator).bounding_box()
    except Exception:  # noqa: BLE001
        return None
    return box if isinstance(box, dict) else None


def _bezier(
    x0: float, y0: float, x1: float, y1: float, steps: int = 8
) -> list[tuple[float, float]]:
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


async def _native_click(locator: Any) -> None:
    try:
        await locator.click()
    except Exception as exc:  # noqa: BLE001
        raise LocateError(f"点击失败（拟人与原生都试过）: {exc}") from exc


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
