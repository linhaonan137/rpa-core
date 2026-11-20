# 引擎内部：定位机制 / 工具层设计 / 真实鼠标

> 改引擎、排查定位问题、调工具粒度时读这一页。

---

# 一、元素库：六维身份 + 八层降级

## 核心思路

**不存单一 CSS 选择器（改版即废）。一个元素存多维身份，定位按稳定性降级，命中多个就打分，分不清就报错而不是猜。**

`elements.json` 在 `.gitignore` 里，干净 clone 必然是空库。首次使用必须先 `element_pick`，否则任何 `@el:` 流程都会定位失败——已知行为，不是 bug。

## 六维身份

| 层 | 字段 | 稳定性 | 失效条件 |
|---|---|---|---|
| 1 语义 | `role` + `name` | ★★★★★ | 改文案 |
| 2 属性 | `id` / `data-testid` / `name` / `type` | ★★★★ | 前端重写 |
| 3 辅助 | `placeholder` / `aria-label` / `title` | ★★★ | 改辅助属性 |
| 4 结构 | `path` + `frame` | ★★ | 改布局 |
| 5 邻居 | `siblings`（最多 5 个）| ★★ | 改周围文案 |
| 6 几何 | `geometry`（**文档坐标**）| ★ | 加广告位 / 改窗口 |

三条硬规则：字段是超集（缺的写 `null`）、`role`/`name` 至少一个非空、`geometry` 存文档坐标不是 `getBoundingClientRect()`。

## 定位：八层（存六维，用八层）

| 层 | 依据 | 权重 |
|---|---|---|
| 1 | `role` + `name` | 20 + 20 |
| 1.2 | 表单 label 关联（上方约 24px）| — |
| 1.5 | 文本内容（治 `p`/`div` 当按钮）| — |
| 2 | `attrs` | testid 28 / id 30 / name 18 / type 8 |
| 3 | 显式 css | — |
| 4 | `path` | 15 |
| 5 | `siblings` | 10 |
| 6 | `geometry` | 5（容差 40px）|

**命中多个时逐维打分**：最高分比第二名高 ≥10 分才用；差距小或并列 → **报歧义，列前 5 个候选**，绝不静默取第一个。

两个坑：
- Playwright 的 accname 会规范化符号（存 `Next →` 算出 `Next`）→ 要有 `_simplify_name()` 重试
- 页面刚 `until=dom` 就检查会 0 命中 → 全失败后等 1 秒重试

## 失败计数与体检

- 定位失败记 `miss`；连续 3 次且 miss > hits → `is_stale`，提示重拾
- 只记 miss 不记 hit（hit 高频，每次落盘不值得）
- `element_verify()` 的 `degraded` 列表 = 需重拾的元素
- `element_verify(keys=[...], detail=True)` 看单元素逐层命中，区分"歧义"还是"不存在"

**页面改版后跑一次体检就知道哪些坏了，不用等流程跑挂。**

## 一个原则

**退到最后一层还找不到 → 报人，不许继续猜。** 停手比乱试便宜。

---

# 二、MCP 工具层：为什么这么设计

## 为什么不做"一个节点一个工具"

单个工具 schema 约 250~400 token。200 个工具吃掉半个上下文，而且**模型在长列表里选择准确率下降——选错工具 = 跑错流程**。

## 三层结构

```
L1  流程级    flow_read / flow_run / run_status      ← 整流程起停
L2  意图级    web_fill / element_pick / file_ops     ← 一工具 = 一意图
L3  节点      （不暴露）                              ← 实现细节
```

## 三条设计原则

1. **按意图聚合，不按实现镜像**：`web_fill` 一个工具覆盖输入框/下拉/复选/上传。
2. **用 `action` / `kind` 分流，不分裂工具**。
3. **必须有逃生舱**：`script_run` 覆盖长尾。想加工具先问"能不能三行解决"。

## 参数只有三类

| 类型 | 例子 | 谁填 |
|---|---|---|
| 语义必需 | 选择器 / URL / 文本 / 元素 key | Agent |
| 调优类 | `limit_ms` / 重试次数 | 引擎默认值 |
| 断言类 | `expect` | 可选 |

**Agent 只填语义必需的，不碰调优的。**

## 按意图选工具（最容易出错）

| 意图 | 用 | 别用 |
|---|---|---|
| 第一次进页面，看能点什么 | `web_scan` | snapshot / fields |
| 读内容型页面 | `web_snapshot` | read |
| 表单字段（无 id 输入框）| `web_fields` | scan |
| 已知元素在哪，取内容 | `web_read` | dom_inspect |
| 抓重复结构列表 | `web_extract` | web_read(list) |
| 看元素真实结构 | `dom_inspect` | web_read |
| 立即查状态 | `web_check` | web_wait（那是等）|
| 等条件成立 | `web_wait` | 固定 sleep |
| 验证定位 | `element_verify` | web_check |

## 两个可见性口径

| 口径 | 工具 | 含义 |
|---|---|---|
| 可操作可见 | `web_check` / `web_visible` | 能不能点（不排除 `opacity:0`）|
| 视觉可见 | `element_verify` / `dom_inspect` | 人眼看得到吗 |

## flow_patch 三条限制

只能改 `args`（不能改 type/id/edges）、`dry_run` 默认 True、`reason` 必填。

**参数建议由数据算**：统计节点 P95，`P95 > 超时×0.8` 建议提高、`连续 3 次 < 超时×0.1` 建议降低，写进 `suggestion` 但不自动改。

## 浏览器实例

三个漂移：端口漂移（每次端口不同）、身份漂移（残留上个 profile）、僵尸实例。

实例带身份证：`id` / `cdp_url`（从响应取，不写死）/ `pid` / `profile`。生命周期 `acquire → 用 → release`。

**单实例串行是刻意选择**：并发下"哪个流程在哪个浏览器"变成必须回答的问题，三倍调试量。

## 三类高危缺陷（加工具时自检）

| 模式 | 现象 | 判据 |
|---|---|---|
| 幽灵节点 | 工具在、节点没实现 | 启动检查 `nodes.failures()` |
| 静默成功 | 返回 ok 但没副作用 | 问"这个成功有证据吗" |
| 参数名映射 | MCP 参数名 ≠ 节点 args 键 | 改参数必须同步改映射表 |

---

# 三、真实鼠标

## 为什么要

CDP 合成事件两个问题：受控组件（next-design 这类）点了不触发；`Input.dispatchMouseEvent` 有痕迹（`isTrusted`、坐标精度）。

三段式，第三步不是可选：

```
注入 JS 算元素屏幕坐标 → real_mouse_click(x, y, verify_text="确认") → 点前校验
```

**坐标点击一旦定位退化就会点到别处，点前校验是唯一拦截点。**

## 三档

| mode | 实现 | 用在 |
|---|---|---|
| `cdp`（默认）| CDP 合成 + 亚像素坐标 | 浏览类 |
| `human` | CDP 层贝塞尔轨迹 | 需拟人 |
| `real` | Windows `SendInput` | 发布、保存、支付 |

## 四个坑（前三个踩中都是静默失败）

**坑 1**：必须用独立 `WinDLL('user32')` 实例。`ctypes.windll.user32` 是进程级单例，别的模块改过 `SendInput.argtypes` 会类型不匹配，**永久失败且不报错**。

**坑 2**：必须设 DPI awareness，否则 125%/150% 缩放下坐标偏移。

**坑 3**：换算系数是「系统缩放 / 浏览器 DPR」，不是 DPR。

实测：系统 AppliedDPI=144（150%），浏览器 DPR=1.0（Playwright 启的不是 Per-Monitor aware）→ 系数 1.5。只乘 DPR 会偏 125px。

```
物理坐标 = (screenX + 左边框 + 页面x) × (系统缩放 / 浏览器DPR)
```

**坑 4（P0）**：窗口必须在前台。硬件点击按屏幕绝对坐标投递，落到"当前前台窗口"。

`page.bring_to_front()` **不够**——只激活 CDP 标签页，不改 Windows 前台窗口。必须：

```python
AttachThreadInput(cur_tid, fg_tid, True)
SetForegroundWindow(hwnd)
AttachThreadInput(cur_tid, fg_tid, False)
```

## 没做

组件库专题封装（下拉/日期/富文本还得现写 JS）、按标题找窗口的唯一性校验。
