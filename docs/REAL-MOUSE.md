# 真实鼠标：为什么要、怎么实现、四个坑

> 交付场景里，CDP 合成事件会被识别，硬件点击不会。这份文档记录 `engine/real_mouse.py` 的设计依据与踩过的坑。
> 前置：`mode=real` 必须配**有头模式**（`RPA_HEADLESS=false`）。headless 没有真实窗口，坐标点击无效。

## 1. 为什么不用 CDP 合成事件

来自一个真实电商后台的实战分布（137 次操作）：

| 节点 | 次数 | 说明 |
|---|---|---|
| `inject_javascript` | 60 | 近半数操作靠注入 JS |
| `real_mouse_click` | 27 | 关键点击全走真实鼠标 |
| `wait` | 40 | 大量固定等待（页面响应不可预测）|
| `scroll_page` | 7 | |
| `wait_element` | 2 | 定位器几乎用不上 |
| `open_page` / 窗口聚焦 | 各 1 | |

**结论：这类后台的实战解法不是"精确定位器点击"，而是"注入 JS 算坐标 + 真实鼠标点击"。**

两个原因：

1. 平台用 next-design 一类组件库，受控组件对 CDP 合成事件的响应不稳定——点了不触发。
2. CDP 的 `Input.dispatchMouseEvent` 有痕迹（`isTrusted`、坐标精度），真实鼠标没有。

三段式：

```
注入 JS 算出元素屏幕坐标 → 存进变量
   ↓
real_mouse_click(x, y, verify_text="确认，下一步")
   ↓
点之前用文本校验，不符就不点
```

第三步不是可选项。坐标点击一旦定位退化就会点到别的地方，**点前校验是唯一的拦截点**。

## 2. 实现：SendInput

`ctypes + user32.SendInput`（Windows 原生 API），不走 CDP。

```python
web_click(
    target="@el:submit",
    mode="real",            # cdp（默认）/ human（CDP 拟人轨迹）/ real（OS 真实鼠标）
    verify_text="确认下单",   # 点前校验，不符就拒绝
    hold_ms=80,
)
```

流程 JSON 里同样可用：

```json
{"type": "click_element",
 "args": {"target": "@el:submit", "mode": "real", "verify_text": "确认下单"}}
```

## 3. 四个坑（前三个任一踩中都是静默失败）

**坑 1：必须用独立的 `WinDLL('user32')` 实例**

`ctypes.windll.user32` 是进程级单例。别的模块若用键盘版 `INPUT` 结构体改过 `SendInput.argtypes`，这里再传鼠标版会类型不匹配，而且**永久失败**（后续所有调用都坏，不报错）。独立实例隔离掉这个污染。

**坑 2：必须设 DPI awareness**

不设的话 125% / 150% 缩放下坐标整体偏移。

**坑 3：换算系数是「系统缩放 / 浏览器 DPR」，不是 DPR**

实测一台机器：

```
系统 AppliedDPI = 144（150% 缩放）
浏览器 devicePixelRatio = 1.0    ← Playwright 启的浏览器不是 Per-Monitor aware
```

系数 = 1.5 / 1.0 = **1.5**。只乘 DPR（=1）的话坐标偏 125px，点不到。

正确换算：

```
物理坐标 = (screenX + 左边框 + 页面x) × (系统缩放 / 浏览器DPR)
```

浏览器本身 aware 时（DPR=1.5）系数自动变 1.0，正好抵消——所以这个公式在两种环境下都对。

**坑 4（P0）：窗口必须在前台**

硬件点击按**屏幕绝对坐标**投递，落到的是"当前前台窗口"。不置前，点击会打进终端/编辑器，页面毫无反应。

而且 `page.bring_to_front()` **不够**——它只激活 CDP 标签页，不改 Windows 前台窗口。必须走 Win32：

```python
AttachThreadInput(cur_tid, fg_tid, True)     # 绕过"只有前台进程能 SetForegroundWindow"
SetForegroundWindow(hwnd)
AttachThreadInput(cur_tid, fg_tid, False)
```

窗口句柄按页面标题找（`EnumWindows` + `GetWindowTextW`）。

## 4. 实测

```
系统缩放 1.5 | 浏览器 DPR 1.0 | 系数 k = 1.5
元素中心 CSS=(75,623) → 物理=(139,1082)
真实鼠标点击 → 页面从 / 翻到 /page/2/          ✓

verify_text 期望含「提交订单」，实际「Next →」
→ 拒绝点击（判定为定位已退化）                  ✓
```

## 5. 能力对照

| 能力 | 状态 | 备注 |
|---|---|---|
| OS 级真实鼠标 SendInput | ✅ | 含 DPI + 前台窗口两个坑 |
| 点前文本校验 | ✅ `verify_text` | 定位退化时唯一的拦截点 |
| 注入 JS 操作 | ✅ `js_script` | 需现写 JS |
| 拟人轨迹 | ✅ 贝塞尔 `human_click` | |
| 固定等待 | ✅ `sleep` | |
| 组件库专题封装 | ❌ | 下拉 / 日期 / 富文本还得现写 JS |

## 6. 还没做

1. **组件库封装**：下拉、日期选择器、富文本编辑器，直接 JS 操作比定位点击稳。要对着真实页面写，现在没有。
2. **等 DOM 变化的等待条件**：理想是"等某个 DOM 变化"，现在只有 `wait_element` 的几种固定条件。
3. **窗口定位精度**：现在按标题找窗口，标题重复时会选错。
