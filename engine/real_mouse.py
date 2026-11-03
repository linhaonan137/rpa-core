"""操作系统级真实鼠标（Windows SendInput）。

为什么需要：Playwright 的 `page.mouse` 走 CDP，浏览器端能收到 `Input.dispatchMouseEvent`
痕迹，而且淘宝/千牛这类后台的受控组件（next-design 等）对 CDP 合成事件响应不稳定。
实战 RPA 流程里关键点击全用真实鼠标，就是这个原因。

两个必须踩过的坑（已在实战中验证）：
  1. **独立 WinDLL 实例**：`ctypes.windll.user32` 是进程级单例，别的模块若改了
     SendInput.argtypes（键盘版结构体），这里再传鼠标版会类型不匹配、永久失败。
     所以这里 `ctypes.WinDLL('user32')` 单独拿一个。
  2. **DPI awareness**：不设的话在 125%/150% 缩放下坐标整体偏，点不中。

另外硬件点击按**屏幕绝对坐标**投递，落到的是"当前前台窗口" ——
所以点之前要确认目标浏览器是前台，否则会点进别的窗口。
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from typing import Any

__all__ = [
    "available",
    "click_at",
    "move_to",
    "page_point_to_screen",
    "MOUSEEVENTF_LEFTDOWN",
    "MOUSEEVENTF_LEFTUP",
    "MOUSEEVENTF_RIGHTDOWN",
    "MOUSEEVENTF_RIGHTUP",
]

# ---------------------------------------------------------------- Win32 定义

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010

_ready = False
_user32: Any = None
_STRUCT: Any = None

try:  # Windows only
    if sys.platform == "win32":
        from ctypes import wintypes as _wt

        class _MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", _wt.LONG),
                ("dy", _wt.LONG),
                ("mouseData", _wt.DWORD),
                ("dwFlags", _wt.DWORD),
                ("time", _wt.DWORD),
                ("dwExtraInfo", ctypes.c_ulonglong),
            ]

        class _INPUT(ctypes.Structure):
            _fields_ = [
                ("type", _wt.DWORD),
                ("mi", _MOUSEINPUT),
            ]

        # 独立实例（见模块注释第 1 条坑）
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.SendInput.argtypes = [_wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        _user32.SendInput.restype = _wt.UINT
        _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        _user32.SetCursorPos.restype = _wt.BOOL
        _user32.GetCursorPos.argtypes = [ctypes.POINTER(_wt.POINT)]
        _user32.GetCursorPos.restype = _wt.BOOL
        _user32.GetForegroundWindow.restype = _wt.HWND
        _user32.SetForegroundWindow.argtypes = [_wt.HWND]
        _user32.SetForegroundWindow.restype = _wt.BOOL
        _user32.ShowWindow.argtypes = [_wt.HWND, ctypes.c_int]
        _user32.ShowWindow.restype = _wt.BOOL
        _user32.IsWindowVisible.argtypes = [_wt.HWND]
        _user32.IsWindowVisible.restype = _wt.BOOL
        _user32.GetWindowTextLengthW.argtypes = [_wt.HWND]
        _user32.GetWindowTextLengthW.restype = ctypes.c_int
        _user32.GetWindowTextW.argtypes = [_wt.HWND, _wt.LPWSTR, ctypes.c_int]
        _user32.GetWindowTextW.restype = ctypes.c_int
        _user32.EnumWindows.argtypes = [ctypes.c_void_p, _wt.LPARAM]
        _user32.EnumWindows.restype = _wt.BOOL
        _user32.GetWindowThreadProcessId.argtypes = [_wt.HWND, ctypes.POINTER(_wt.DWORD)]
        _user32.GetWindowThreadProcessId.restype = _wt.DWORD
        _user32.AttachThreadInput.argtypes = [_wt.DWORD, _wt.DWORD, _wt.BOOL]
        _user32.AttachThreadInput.restype = _wt.BOOL
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.GetCurrentThreadId.restype = _wt.DWORD
        _STRUCT = _INPUT
        _ready = True
except Exception:  # noqa: BLE001 - 非 Windows 或结构体定义失败 → 不可用
    _ready = False


def available() -> bool:
    """当前环境能不能用真实鼠标。"""
    return _ready


def _set_dpi_aware() -> None:
    """DPI 感知：125%/150% 缩放下不设这个，坐标会整体偏。"""
    if not _ready:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


_dpi_done = False


def _ensure_dpi() -> None:
    global _dpi_done
    if not _dpi_done and _ready:
        _set_dpi_aware()
        _dpi_done = True


def system_scale() -> float:
    """系统 DPI 缩放比（1.0 / 1.25 / 1.5 …），取不到按 1.0。

    注意它和 `window.devicePixelRatio` 不是一回事：
    实测系统 150% 缩放时，Playwright 启的浏览器 devicePixelRatio 仍是 1.0
    （浏览器不是 Per-Monitor aware），所以换算屏幕坐标要乘的是**系统缩放**，
    不能乘 DPR，否则点不到（实测偏 125px）。
    """
    if not _ready:
        return 1.0
    try:
        import ctypes as _c

        _ensure_dpi()
        gdi32 = _c.WinDLL("gdi32", use_last_error=True)
        gdi32.GetDeviceCaps.argtypes = [_c.c_void_p, _c.c_int]
        gdi32.GetDeviceCaps.restype = _c.c_int
        _user32.GetDC.restype = _c.c_void_p
        _user32.GetDC.argtypes = [_c.c_void_p]
        _user32.ReleaseDC.argtypes = [_c.c_void_p, _c.c_void_p]
        hdc = _user32.GetDC(None)
        if not hdc:
            return 1.0
        dpi = int(gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        _user32.ReleaseDC(None, hdc)
        return (dpi / 96.0) if dpi else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def _send(flag: int) -> bool:
    if not _ready:
        return False
    inp = _STRUCT()
    inp.type = 0  # INPUT_MOUSE
    inp.mi.dx = 0
    inp.mi.dy = 0
    inp.mi.mouseData = 0
    inp.mi.dwFlags = flag
    inp.mi.time = 0
    inp.mi.dwExtraInfo = 0
    return _user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(_STRUCT)) > 0


# ---------------------------------------------------------------- 移动与点击


def _cursor_pos() -> tuple[int, int]:
    pt = ctypes.wintypes.POINT() if hasattr(ctypes, "wintypes") else None
    if pt is None:
        from ctypes import wintypes as wt

        pt = wt.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


async def move_to(x: int, y: int, *, steps: int = 12, jitter: bool = True) -> None:
    """分段移动（不是瞬移），模拟人手轨迹。"""
    if not _ready:
        raise RuntimeError("真实鼠标不可用（非 Windows）")
    _ensure_dpi()
    x0, y0 = _cursor_pos()
    steps = max(1, min(steps, 40))
    for i in range(1, steps + 1):
        u = i / steps
        # 缓出曲线：起步快、收尾慢，接近人手
        e = 1 - (1 - u) ** 2
        px = x0 + (x - x0) * e
        py = y0 + (y - y0) * e
        if jitter and i < steps:
            px += ((i * 7) % 3) - 1
            py += ((i * 5) % 3) - 1
        _user32.SetCursorPos(int(px), int(py))
        await asyncio.sleep(0.012)
    _user32.SetCursorPos(int(x), int(y))


async def click_at(
    x: int,
    y: int,
    *,
    button: str = "left",
    hold_ms: int = 60,
    steps: int = 12,
    double: bool = False,
) -> dict[str, Any]:
    """移动到 (x,y) 再真实点击。返回诊断信息。"""
    if not _ready:
        raise RuntimeError("真实鼠标不可用（非 Windows 或 SendInput 初始化失败）")
    await move_to(x, y, steps=steps)
    await asyncio.sleep(0.03)

    down, up = (
        (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
        if button == "right"
        else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
    )
    ok_down = _send(down)
    await asyncio.sleep(max(hold_ms, 1) / 1000)
    ok_up = _send(up)
    if double:
        await asyncio.sleep(0.06)
        _send(down)
        await asyncio.sleep(max(hold_ms, 1) / 1000)
        _send(up)
    return {"x": x, "y": y, "down": ok_down, "up": ok_up, "button": button}


async def page_point_to_screen(page: Any, box: dict[str, Any]) -> tuple[int, int]:
    """把元素在页面里的 bounding_box 换算成屏幕物理坐标。

    物理坐标 = (screenX + 左边框 + 页面x) × (系统缩放 / 浏览器DPR)

    为什么是这个系数：
      - 进程 PerMonitorAware 后 SetCursorPos 用**物理像素**
      - 浏览器 API（screenX / getBoundingClientRect）用 **CSS 像素**
      - 实测系统 150% 缩放时 Playwright 的浏览器 devicePixelRatio 仍是 1.0
        （浏览器非 Per-Monitor aware）→ 系数 = 1.5/1.0 = 1.5
      - 若浏览器本身 aware（DPR=1.5）→ 系数 = 1.5/1.5 = 1.0，正好抵消
    """
    info = await page.evaluate(
        """() => ({
            sx: window.screenX, sy: window.screenY,
            ow: window.outerWidth, oh: window.outerHeight,
            iw: window.innerWidth, ih: window.innerHeight,
            dpr: window.devicePixelRatio || 1,
        })"""
    )
    dpr = float(info.get("dpr", 1) or 1)
    k = system_scale() / dpr if dpr else 1.0
    bx = (float(info.get("ow", 0)) - float(info.get("iw", 0))) / 2.0
    by = float(info.get("oh", 0)) - float(info.get("ih", 0))
    cx = float(box.get("x", 0)) + float(box.get("width", 0)) / 2.0
    cy = float(box.get("y", 0)) + float(box.get("height", 0)) / 2.0
    return (
        int(round((float(info.get("sx", 0)) + bx + cx) * k)),
        int(round((float(info.get("sy", 0)) + by + cy) * k)),
    )


def foreground_hwnd() -> int:
    """当前前台窗口句柄（用于确认点击不会打进别的窗口）。"""
    if not _ready:
        return 0
    try:
        return int(_user32.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return 0


def find_window_by_title(substr: str) -> int:
    """按标题子串找可见顶层窗口（浏览器窗口）。找不到返回 0。"""
    if not _ready or not substr:
        return 0
    found: list[int] = []

    from ctypes import wintypes as wt

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            n = int(_user32.GetWindowTextLengthW(hwnd))
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                if substr in buf.value:
                    found.append(int(hwnd))
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        return 0
    return found[0] if found else 0


def bring_to_front(hwnd: int) -> bool:
    """把窗口置前。

    Windows 限制：只有前台进程才能 SetForegroundWindow，否则静默失败。
    绕过方式：AttachThreadInput 把自己的输入线程挂到目标/前台线程上再置前。
    """
    if not _ready or not hwnd:
        return False
    try:
        fg = int(_user32.GetForegroundWindow())
        if fg == hwnd:
            return True
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE：最小化的也能拉起来
        cur = int(_kernel32.GetCurrentThreadId())
        fg_tid = int(_user32.GetWindowThreadProcessId(fg, None)) if fg else 0
        tgt_tid = int(_user32.GetWindowThreadProcessId(hwnd, None))
        if fg_tid:
            _user32.AttachThreadInput(cur, fg_tid, True)
        if tgt_tid:
            _user32.AttachThreadInput(cur, tgt_tid, True)
        ok = bool(_user32.SetForegroundWindow(hwnd))
        if tgt_tid:
            _user32.AttachThreadInput(cur, tgt_tid, False)
        if fg_tid:
            _user32.AttachThreadInput(cur, fg_tid, False)
        return ok and int(_user32.GetForegroundWindow()) == hwnd
    except Exception:  # noqa: BLE001
        return False


async def ensure_foreground(page: Any) -> dict[str, Any]:
    """把页面所在的浏览器窗口拉到前台，返回诊断。

    真实鼠标按屏幕绝对坐标投递，落到"当前前台窗口" ——
    不置前的话点击会打进终端/编辑器，页面一点反应都没有（实测）。
    """
    info: dict[str, Any] = {"brought": False, "title": "", "hwnd": 0, "why": ""}
    if not _ready or page is None:
        info["why"] = "真实鼠标不可用"
        return info
    try:
        title = (await page.title()) or ""
        info["title"] = title[:40]
        await page.bring_to_front()  # 先让 CDP 激活标签页
        await asyncio.sleep(0.15)
        # 再按标题找 Windows 顶层窗口置前（CDP 激活标签页不会改 OS 前台窗口）
        hwnd = find_window_by_title(title) if title else 0
        if not hwnd:
            hwnd = find_window_by_title("Chrome") or find_window_by_title("Edge")
        info["hwnd"] = hwnd
        if hwnd:
            info["brought"] = bring_to_front(hwnd)
        if not info["brought"]:
            info["why"] = "没找到浏览器窗口或置前被系统拒绝"
    except Exception as exc:  # noqa: BLE001
        info["why"] = f"{type(exc).__name__}: {exc}"
    return info
