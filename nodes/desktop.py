"""桌面三节点：批量抓表格 / 从控件选中取字 / 读焦点控件。

为什么只有三个：其余 37 个桌面能力交给 desktop-touch（租约 + 窗口校验 + 视觉兜底），
这里只留它没做、且只有 UIA 能做的三件事。

三条实现约定：
  1. **延迟 import uiautomation**——这个库只在 Windows 有，装不上也不该让
     `nodes.load_all()` 整个包导入失败。
  2. **库缺失返回 fail**，错误信息里带 pip 命令，不要抛到引擎外。
  3. **文字优先走 ValuePattern**——DataGrid 单元格的 `Name` 常常是空的或
     被截断，`GetValuePattern().Value` 才是完整文本；两者都拿不到时退到
     `Name`，再拿不到给空串（不伪造）。
"""

from __future__ import annotations

import time
from typing import Any

from engine.context import ExecutionContext
from engine.registry import Result, register

_MISSING_HINT = (
    "缺少 uiautomation 库（仅 Windows 可用）。安装：pip install uiautomation\n"
    "  桌面节点只在 Windows 上有意义；Linux/macOS 请改用网页或命令行方案。"
)

#: 表格类控件类型，按"越具体越先试"排列
_GRID_TYPES = ("DataGridControl", "TableControl", "ListControl", "TreeControl")


class UiaMissing(RuntimeError):
    """uiautomation 没装 / 导入失败。"""


def _uia() -> Any:
    """延迟导入。测试里 monkeypatch 这个函数即可完全脱机。"""
    try:
        import uiautomation as auto  # noqa: PLC0415 - 故意延迟
    except Exception as exc:  # noqa: BLE001 - ImportError / DLL 加载失败都算
        raise UiaMissing(f"{_MISSING_HINT}\n  实际错误: {type(exc).__name__}: {exc}") from exc
    return auto


# ---------------------------------------------------------------- 控件定位


def _find(auto: Any, target: Any, timeout_ms: int = 0) -> Any:
    """按 target 找控件。

    target 可以是：
      · 字符串 → 按 Name 精确找
      · dict   → {name / automation_id / class_name / control_type / handle / depth}
    找不到返回 None（由调用方决定 fail 信息）。
    """
    if isinstance(target, str):
        spec: dict[str, Any] = {"name": target}
    elif isinstance(target, dict):
        spec = target
    else:
        return None

    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    kwargs: dict[str, Any] = {
        "searchDepth": int(spec.get("depth") or 10),
    }
    if spec.get("name"):
        kwargs["Name"] = str(spec["name"])
    if spec.get("automation_id"):
        kwargs["AutomationId"] = str(spec["automation_id"])
    if spec.get("class_name"):
        kwargs["ClassName"] = str(spec["class_name"])
    if spec.get("control_type"):
        kwargs["ControlType"] = _control_type(auto, str(spec["control_type"]))

    while True:
        try:
            if spec.get("handle"):
                ctrl = auto.ControlFromHandle(int(spec["handle"]))
            else:
                ctrl = auto.PaneControl(**kwargs)
            if ctrl.Exists(0.2, 0.2):
                return ctrl
        except Exception:  # noqa: BLE001 - UIA 调用失败可能就是没找到
            pass
        if time.monotonic() >= deadline:
            return None


def _control_type(auto: Any, name: str) -> Any:
    """把 "DataGrid" / "ListControl" 之类统一成 uiautomation 的常量。"""
    key = name if name.endswith("Control") else f"{name}Control"
    return getattr(auto, key, None)


def _cell_text(cell: Any) -> str:
    """ValuePattern 优先，Name 兜底。"""
    try:
        value = cell.GetValuePattern().Value
        if value not in (None, ""):
            return str(value).strip()
    except Exception:  # noqa: BLE001 - 没有该 Pattern 是常态
        pass
    try:
        name = cell.Name
        if name not in (None, ""):
            return str(name).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _runtime_id(ctrl: Any) -> str:
    """GetRuntimeId 去重键。取不到就用对象 id 兜底（至少不会全塌成同一个）。"""
    try:
        rid = ctrl.GetRuntimeId()
        if rid:
            return "|".join(str(x) for x in rid)
    except Exception:  # noqa: BLE001
        pass
    return f"obj:{id(ctrl)}"


def _rows_of(grid: Any, max_rows: int = 5000) -> list[Any]:
    """抓一个容器控件的直接子行。"""
    try:
        children = grid.GetChildren()
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for child in children[:max_rows]:
        # DataGrid 的子树里通常还有一层 DataItem；直接取子元素更稳
        rows.append(child)
    return rows


def _row_cells(row: Any) -> list[Any]:
    try:
        return list(row.GetChildren())
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- 节点


@register("ui_table")
async def ui_table(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """UIA 批量抓 DataGrid / List。

    args:
      target      表格控件（字符串=Name，或 dict 见 _find）
      kind        "dicts"（默认，首行当表头）| "rows"（纯二维数组）
      save_as     变量名，默认 table
      scroll      True 时滚动加载，抓可视区外的行
      max_rounds  最多滚几屏，默认 10
      limit_ms    总预算（滚动加载时的硬上限）
    """
    try:
        auto = _uia()
    except UiaMissing as exc:
        return Result.fail(str(exc))

    target = ctx.resolve(args.get("target")) or args.get("name")
    if not target:
        return Result.fail("ui_table 缺 target（表格控件）")

    try:
        grid = _find(auto, target, int(args.get("limit_ms") or 0))
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"ui_table 定位控件失败: {exc}")

    if grid is None:
        return Result.fail(f"ui_table 找不到控件: {target!r}")

    # target 可能给的是窗口：往下钻一层找表格
    if _control_type_name(grid) not in _GRID_TYPES:
        inner = _dig_grid(auto, grid)
        if inner is not None:
            grid = inner

    seen: set[str] = set()
    rows: list[list[str]] = []
    rounds = 0
    max_rounds = int(args.get("max_rounds") or 10) if bool(args.get("scroll")) else 1
    budget = int(args.get("limit_ms") or 0) or 120_000
    deadline = time.monotonic() + budget / 1000

    while rounds < max_rounds and time.monotonic() < deadline:
        rounds += 1
        fresh = 0
        for row in _rows_of(grid):
            rid = _runtime_id(row)
            if rid in seen:
                continue
            seen.add(rid)
            cells = [_cell_text(c) for c in _row_cells(row)]
            if not any(cells):  # 表头占位 / 空行
                continue
            rows.append(cells)
            fresh += 1

        if not bool(args.get("scroll")) or fresh == 0:
            break
        if not _scroll_page(grid):
            break

    if not rows:
        return Result.fail(f"ui_table 控件找到了但没抓到数据行: {target!r}")

    kind = str(args.get("kind") or "dicts")
    if kind == "rows":
        data: Any = rows
    else:
        header = rows[0]
        body = rows[1:]
        # 列数不一致（合并单元格 / 末列空）时按表头宽度补齐
        data = [
            {header[i] if i < len(header) else f"col{i + 1}": (r[i] if i < len(r) else "")
             for i in range(max(len(header), len(r)))}
            for r in body
        ]

    name = str(args.get("save_as") or "table")
    ctx.set_var(name, data)
    return Result.done(
        f"ui_table 抓到 {len(data)} 行（{rounds} 屏）→ {name}", data=data
    )


def _control_type_name(ctrl: Any) -> str:
    try:
        return str(ctrl.ControlTypeName)
    except Exception:  # noqa: BLE001
        return ""


def _dig_grid(auto: Any, root: Any) -> Any:
    """在窗口里往下找第一个表格类控件（target 给的是窗口时的常见情况）。"""
    for type_name in _GRID_TYPES:
        ctrl_type = getattr(auto, type_name, None)
        if ctrl_type is None:
            continue
        try:
            ctrl = root.Control(searchDepth=6, ControlType=ctrl_type)
        except Exception:  # noqa: BLE001
            continue
        if ctrl is not None:
            return ctrl
    return None


def _scroll_page(grid: Any) -> bool:
    """翻一屏。返回 False 表示滚不动了（到头 / 没有 ScrollPattern）。"""
    try:
        pattern = grid.GetScrollPattern()
    except Exception:  # noqa: BLE001
        return False
    try:
        if not pattern.VerticallyScrollable:
            return False
        # ScrollAmount: 0=NoAmount 1=LargeIncrement（不依赖库里的常量名）
        pattern.Scroll(0, 1)
        time.sleep(0.2)  # 等虚拟列表把新行渲染出来
        return True
    except Exception:  # noqa: BLE001
        return False


@register("ui_text")
async def ui_text(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """控件读不到 `.Text` 时的唯一办法：聚焦 → Ctrl+A → Ctrl+C → 读剪贴板。

    args: target（控件）| kind="all"|"copy" | save_as=selected_text
    """
    try:
        auto = _uia()
    except UiaMissing as exc:
        return Result.fail(str(exc))

    target = ctx.resolve(args.get("target")) or args.get("control")
    if not target:
        return Result.fail("ui_text 缺 target（控件）")

    try:
        ctrl = _find(auto, target, int(args.get("limit_ms") or 0))
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"ui_text 定位控件失败: {exc}")
    if ctrl is None:
        return Result.fail(f"ui_text 找不到控件: {target!r}")

    try:
        ctrl.SetFocus()
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"ui_text 无法聚焦控件: {exc}")

    mode = str(args.get("kind") or "all")
    try:
        if mode == "all":
            auto.SendKeys("{Ctrl}a", waitTime=0.05)
        auto.SendKeys("{Ctrl}c", waitTime=0.15)
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"ui_text 发送按键失败: {exc}")

    try:
        text = auto.GetClipboardText() or ""
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"ui_text 读剪贴板失败: {exc}")

    if not text.strip():
        return Result.fail(
            "ui_text 剪贴板为空：控件可能不支持 Ctrl+A/C，"
            "或焦点被抢走了（先关掉输入法/弹窗再试）"
        )

    name = str(args.get("save_as") or "selected_text")
    ctx.set_var(name, text)
    return Result.done(f"选中 {len(text)} 字符 → {name}", data=text)


@register("ui_focus")
async def ui_focus(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """读当前键盘焦点控件的 AutomationId / Name / ClassName。

    args: save_as=focused_control | target 可选（给了就与焦点做一致性校验）
    """
    try:
        auto = _uia()
    except UiaMissing as exc:
        return Result.fail(str(exc))

    try:
        ctrl = auto.GetFocusedControl()
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"ui_focus 取焦点控件失败: {exc}")
    if ctrl is None:
        return Result.fail("ui_focus 当前没有焦点控件（窗口可能没激活）")

    def _attr(name: str) -> str:
        try:
            return str(getattr(ctrl, name, "") or "")
        except Exception:  # noqa: BLE001
            return ""

    info = {
        "automation_id": _attr("AutomationId"),
        "name": _attr("Name"),
        "class_name": _attr("ClassName"),
        "control_type": _attr("ControlTypeName"),
        "runtime_id": _runtime_id(ctrl),
    }

    expected = ctx.resolve(args.get("target"))
    if expected:
        hits = {info["automation_id"], info["name"], info["class_name"]}
        if str(expected) not in hits:
            return Result.fail(
                f"焦点控件不是期望的 {expected!r}（当前: "
                f"automation_id={info['automation_id']!r} name={info['name']!r}）"
            )

    name = str(args.get("save_as") or "focused_control")
    ctx.set_var(name, info)
    return Result.done(
        f"焦点 = {info['control_type']} name={info['name']!r} → {name}", data=info
    )
