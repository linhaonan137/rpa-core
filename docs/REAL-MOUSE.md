
| 节点类型 | 数量 | 说明 |
|---|---|---|
| **inject_javascript** | **60** | 44% 的操作靠注入 JS |
| **real_mouse_click** | **27** | 关键点击全用真实鼠标 |
| wait | 40 | 大量固定等待（页面响应不可预测）|
| scroll_page | 7 | |
| wait_element | 2 | 定位器只用 2 次 |
| open_page / window_focus | 各 1 | |

**结论：淘宝后台的实战解法不是"精确定位器点击"，而是"注入 JS + 真实鼠标坐标点击"。**

原因：
1. 淘宝用 next-design 等组件库，受控组件对 CDP 合成事件响应不稳定
2. CDP 的 `Input.dispatchMouseEvent` 有痕迹，真实鼠标没有

它的三段式：
```
注入 JS 算元素屏幕坐标 → 存进变量 {catX}/{catY}
   ↓
real_mouse_click(x={catX}, y={catY}, verifyText="确认，下一步")
   ↓
点之前用 verifyText 校验，不对就不点
```

## 二、移植到 rpa：`engine/real_mouse.py`

### 核心实现

用 `ctypes + user32.SendInput`（Windows 原生 API），不是 Playwright 的 CDP mouse。

**坑 1：必须用独立的 `WinDLL('user32')` 实例**

`ctypes.windll.user32` 是进程级单例。别的模块若用键盘版 `INPUT` 结构体改过
`SendInput.argtypes`，这里再传鼠标版会类型不匹配、**永久失败**。

**坑 2：必须设 DPI awareness**

不设的话 125%/150% 缩放下坐标整体偏。

**坑 3：换算系数是"系统缩放 / 浏览器DPR"，不是 DPR**

实测这台机器：
```
系统 AppliedDPI = 144（150% 缩放）
浏览器 devicePixelRatio = 1.0   ← Playwright 启的浏览器不是 Per-Monitor aware
```
所以系数 = 1.5 / 1.0 = **1.5**。只乘 DPR（=1）的话坐标偏 125px，点不到。

正确的换算：
```
物理坐标 = (screenX + 左边框 + 页面x) × (系统缩放 / 浏览器DPR)
```
浏览器本身 aware 时（DPR=1.5）系数自动变 1.0，正好抵消。

### 还有一个 P0：窗口必须在前台

硬件点击按**屏幕绝对坐标**投递，落到的是"当前前台窗口"。
不置前的话点击会打进终端/编辑器，页面一点反应都没有（实测两次都失败）。

而且 `page.bring_to_front()` **不够** —— 它只激活 CDP 标签页，不改 Windows 前台窗口。
必须走 Win32：

```python
AttachThreadInput(cur_tid, fg_tid, True)     # 绕过"只有前台进程能 SetForegroundWindow"
SetForegroundWindow(hwnd)
AttachThreadInput(cur_tid, fg_tid, False)
```

窗口句柄按页面标题找（`EnumWindows` + `GetWindowTextW`）。

## 三、用法

```python
# MCP
web_click(
    target="@el:submit",
    mode="real",           # cdp（默认）/ human（CDP 拟人）/ real（OS 真实鼠标）
    verify_text="确认下单",  # 点前校验，不符就拒绝
    hold_ms=80,
)
```

流程里：
```json
{"type": "click_element",
 "args": {"target": "@el:submit", "mode": "real", "verify_text": "确认下单"}}
```

## 四、实测

```
系统缩放: 1.5 | 浏览器 DPR: 1.0 | 系数 k = 1.5
元素中心 CSS=(75,623) → 物理=(139,1082)
真实鼠标点击 → 页面从 / 翻到 /page/2/   ✓✓

verify_text 不符：
  期望含「提交订单」，实际「Next →」→ 拒绝点击（可能定位退化了）   ✓
```

**注意**：`mode=real` 必须配**有头模式**（`RPA_HEADLESS=false`），
headless 没有真实窗口，坐标点击无效。

|---|---|---|---|
| OS 级真实鼠标 | ✅ SendInput | ✅ 已移植 | 含 DPI + 前台窗口两个坑 |
| 点前文本校验 | ✅ verifyText | ✅ `verify_text` | |
| 注入 JS 操作 | ✅ 60 个节点 | ✅ `js_script` | 但要现写 JS |
| 拟人轨迹 | ✅ | ✅ `human_click` 贝塞尔 | 之前写好了从没接进节点 |
| 固定等待 | 40 次 | ✅ `sleep` | |
| **组件库专题封装** | 有（下拉/日期/富文本）| ❌ 没有 | 淘宝 next-design 那套还得现写 JS |

## 六、还没做

1. **next-design 组件封装**：淘宝的下拉、日期选择器、富文本编辑器，直接 JS 操作比定位点击稳。
   要用就得对着真实页面写，现在没有。
   理想是"等某个 DOM 变化"，但现在只有 `wait_element` 几种条件。
   现在只按标题找，标题重复时会选错。
