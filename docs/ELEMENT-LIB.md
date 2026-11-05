# 元素库：字段、采集、定位

> 一句话：**不存单一 CSS 选择器（改版即废）。一个元素存多维身份，定位时按稳定性逐级降级，命中多个就打分，分不清就报错而不是猜。**
> 环境：Playwright 1.64.1（`ariaSnapshot()` 可用；`page.accessibility` 已废弃，不要用）。

## 0. 仓库不带元素库

`elements.json` 在 `.gitignore` 里，代码也没有默认初始化——**干净 clone 必然得到空库**。

首次使用必须先 `element_pick` 拾取（或 `web_scan` 看页面上有什么，再挑）。任何"clone 下来直接跑"的流程只要写了 `@el:xxx`，都会定位失败。这是已知行为，不是 bug。

## 1. 字段：六维身份

| 层 | 字段 | 稳定性 | 失效条件 | 谁能填 |
|---|---|---|---|---|
| 1 语义 | `role` + `name` | ★★★★★ | 改文案 | MCP / 拾取器 / 自愈 |
| 2 属性 | `attrs`：`id` / `data-testid` / `name` / `type` | ★★★★ | 前端重写 | 拾取器 |
| 3 辅助 | `attrs`：`placeholder` / `aria-label` / `title` | ★★★ | 改辅助属性 | 拾取器 |
| 4 结构 | `path` + `frame` | ★★ | 改布局 / 改 iframe | 拾取器 |
| 5 邻居 | `siblings`（同父邻居文本，最多 5 个） | ★★ | 改周围文案 | 拾取器 / 自愈 |
| 6 几何 | `geometry`（**文档坐标** = 视口坐标 + 滚动量） | ★ | 加广告位 / 改窗口 | 拾取器 / 自愈 |

```jsonc
{
  "key": "submit_order",          // 主键，节点里写 @el:submit_order
  "role": "button",
  "name": "提交订单",
  "attrs": { "id": "submit-btn", "data-testid": "order-submit", "placeholder": null },
  "path": ["form#order-form:nth-child(3)", "div.actions:nth-child(2)", "button:nth-child(1)"],
  "frame": [],                    // iframe 链，空数组 = 主文档
  "siblings": ["取消", "保存草稿"],
  "geometry": { "x": 820, "y": 640, "w": 120, "h": 36, "scrollX": 0, "scrollY": 120 },
  "_heal": null,                  // 自愈痕迹 {"ts","from","why","by"}
  "_meta": { "created_at": "...", "source": "picker", "hits": 0, "misses": 0 }
}
```

**三条硬规则**：

1. **字段是超集，缺的写 `null`，不要省键** —— 否则读取侧要写一堆 `if key in dict`。
2. **`role` 和 `name` 至少一个非空** —— 都空说明档案无效，校验器直接拒。
3. **`geometry` 存文档坐标**，不是 `getBoundingClientRect()` 的原始值。

## 2. 采集：一次 evaluate 拿全

一次 `locator.evaluate()` 往返拿全部字段，比调六次 API 快。要点三条：

**① `name` 用简化版 accname**

```
aria-labelledby（解析引用元素文本）
  → aria-label
  → label[for=id]
  → 包裹的 <label>
  → placeholder / title / alt
  → 有值的 value（仅非空）
  → innerText
```

只写 `innerText` 会踩两个坑：空输入框的 `value` 是空串 → 生成 `role=textbox[name=""]`，匹配全页所有输入框；表单输入框的 `name` 全为空。

**② `role` 按 input type 细分**

```
checkbox / radio / submit|button|reset → button
search → searchbox     range → slider     file → button
hidden → null          其余 → textbox
```

一刀切 `INPUT → textbox` 会让 radio / checkbox 全部认错。标签推断同理要补全：`NAV→navigation`、`UL→list`、`LI→listitem`、`P→paragraph`、`DIALOG→dialog`。

**③ `path` 稳定化**

```
#id > [data-testid] > [name] > tag（同标签唯一）> tag:nth-of-type(n)
```

裸 `html:nth-child(1) > body:nth-child(2) > ...` 深度五六层，任何兄弟增删就崩。优先锚点，最后才 nth。

**iframe**：`frame` 链必须在**进入 iframe 之前**记好——iframe 内部的 `window` 拿不到外层信息。用 Playwright 的 `frame.frame_element()` 反查链（走 CDP，不受同源限制）；JS 的 `window.frameElement` 在 `file://` 和跨域下是 null，不能用。跨域 iframe 里 `evaluate` 会被同源策略拦，只能记 frame 链 + `frame_locator` 定位，标 `"crossOrigin": true` 并跳过第 2~5 层。

## 3. 定位：八层降级 + 加权打分

存的是六维，**定位实际走八层**——文本层与 label 关联层是采集时不单独存、定位时现算的：

| 层 | 依据 | 权重 | 说明 |
|---|---|---|---|
| 1 | `role` + `name` | 20 + 20 | 最稳，改文案才失效 |
| 1.2 | 表单 label 关联（上方约 24px 的标签文本） | — | 治大量同 placeholder 的输入框 |
| 1.5 | 文本内容 | — | 治无语义标签（`p` / `div` 当按钮） |
| 2 | `attrs` | testid 28 / id 30 / name 18 / type 8 | 锚点 |
| 3 | 显式存的 css | — | 手工兜底 |
| 4 | `path` | 15 | 稳定化后的父链 |
| 5 | `siblings` | 10 | 先锁父容器再找子元素 |
| 6 | `geometry` | 5 | 文档坐标最近（容差 40px）|

**命中多个时逐维打分**：最高分比第二名高 ≥10 分才用；差距小或并列 → **报歧义，列前 5 个候选（tag + 文本 + 分数）提示重新拾取**。绝不静默取第一个——`role=link[name="(about)"]` 在列表页能命中 10 个，点哪个全看页面顺序。

**位置完全重合的重复 DOM**（SSR 水合残留）自动放行。

**Playwright 的 accname 会规范化符号**：我们存 `Next →`，它算出 `Next` → 语义层 0 命中。要有一次 `_simplify_name()` 重试，否则最稳的一层会被无谓跳过。

**页面刚 `until=dom` 就检查会 0 命中**（内容还没画完）→ 全失败后等 1 秒重试一次。

## 4. 失败计数与体检

- 定位失败记一次 `miss`；**连续 3 次且 miss > hits → `is_stale`**，提示重拾。
- 只记 miss 不记 hit——hit 在循环里高频触发，每次落盘不值得。
- `element_verify()` 在当前页面逐个验证，报告"靠哪一层命中"。`degraded` 列表就是需要重拾的元素。
- `element_verify(keys=["submit"], detail=True)` 给单个元素的逐层命中数——判断是"歧义"还是"不存在"。
- `element_list` 显示 key / role / name / hits / misses / stale，不碰页面。

**这是判断"元素库还准不准"的最直接手段**：页面改版后跑一次 `element_verify()` 就知道哪些坏了，不用等流程跑挂。

## 5. Agent 侧的完整链路

```
web_scan        看页面上有哪些可点元素（含 iframe，已去重、已过滤不可见）
element_pick    一次调用拾取（text= 不用写 CSS，自动带 frame 链）
web_click       用 @el:key 操作（八层降级定位）
element_verify  定期体检（不传参=全库），看哪些退化了
```

`element_pick(text="Next", key="next")` 会先试属性组（placeholder / aria-label / title / name），再退回 `text=`——输入框这类元素没有可见文本，只有 placeholder。

**元素库必须能从 MCP 侧走完整闭环**（扫描 → 拾取 → 引用 → 体检），否则它只是引擎内部数据结构，Agent 用不上。

## 6. 已知未做

| 项 | 说明 |
|---|---|
| `path` 的 frame 链 | 抓到了 `frame`，但定位时未用于切 iframe |
| `siblings` 未参与主打分 | 只在降级层用，怕误伤 |
| `geometry` 只做兜底 | 坐标随分辨率/缩放变，不进主打分 |
| 元素库历史趋势 | `hits/misses` 存了，但没有"这个元素多常失效"的统计视图 |

## 7. 一个原则

**退到最后一层还找不到 → 报人，不许继续猜。**

这是 false heal 的第一道防线。自愈能力越强，越要有一条"停手比乱试便宜"的边界。
