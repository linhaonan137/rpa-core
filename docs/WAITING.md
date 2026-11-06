# 等待策略：等元素，别猜时间

写流程时**最容易犯的错**就是 `sleep(3000)`。写流程前先读这一页。

## 一句话

**能等元素就等元素；元素出现后加 0.3~1 秒随机抖动，再操作。**

```json
{"type": "web_wait", "args": {"target": "a:has-text('发布商品')", "jitter": [400, 1100]}}
```

只有**没有元素可等**的地方（比如等新标签页出现）才用 `sleep`，而且要用**区间**：

```json
{"type": "sleep", "args": {"ms": [1200, 2200]}}
```

## 为什么不能固定 sleep

| 问题 | 说明 |
|---|---|
| 慢了就浪费 | 页面 0.7 秒就绪，固定等 3 秒 = 每条流程白丢 2.3 秒。10 个步骤就是 20 秒 |
| 快了就崩 | 网络抖动时固定 1 秒不够，元素还没出来就点 → 找不到元素。而且**时快时慢，最难复现** |
| 节奏机械 | 每次都精确在同一个毫秒数上动作，这是最明显的机器特征 |

实测对比（千牛发品选类目，同一套流程）：

```
固定 sleep：  20.1 秒
等元素+抖动： 10.6 秒
```

## 三种等待对象

| 等什么 | 参数 | 例子 |
|---|---|---|
| 等元素到某状态 | `target` + `kind` | `{"target": "a:has-text('商品')", "kind": "visible"}` |
| 等页面出现文字 | `text` | `{"text": "正确填写品牌信息"}` |
| 等地址变化 | `url` | `{"url": "/publish.htm"}` |

`kind` 可选：`visible`（可见，最常用）/ `attached`（在 DOM 里即可）/ `hidden` / `detached`

`limit_ms` 是**最长**等待时间（默认 30 秒）。它只是保险，不是实际耗时会等满。

## 抖动（jitter）

元素一出现就立刻点，反应时间是 0ms —— 真实用户不会这样。

```json
{"jitter": [200, 800]}     // 随机停 200~800ms
{"jitter": 500}            // 固定停 500ms
```

**建议**：
- 普通点击/输入后：`[300, 900]`
- 切换页面/提交后：`[500, 1500]`
- 敏感操作（下单、发布）：`[800, 2000]`

## 支持 jitter 的节点（别漏）

不只是 `web_wait`，**所有会让页面状态变化的节点都支持**。漏一个就是一处 0ms 反应：

| 场景 | 节点 | 建议 jitter |
|---|---|---|
| 打开页面后 | `web_open` | `[500, 1500]` |
| 切换标签后 | `web_tab` | `[400, 1000]` |
| 等到元素后 | `web_wait` | `[300, 900]` |
| 普通点击后 | `web_click` | `[300, 900]` |
| 提交/确认后 | `web_click` | `[600, 1500]` |
| JS 点击后 | `web_js` | `[300, 800]` |

```json
{"type": "web_open",  "args": {"url": "...", "jitter": [500, 1500]}},
{"type": "web_tab",   "args": {"kind": "title", "title": "商品发布", "jitter": [400, 1000]}},
{"type": "web_click", "args": {"target": "#submit", "jitter": [600, 1500]}},
{"type": "web_js",    "args": {"code": "...", "jitter": [300, 800]}}
```

**页面切换后尤其重要**：新页面刚加载完就立刻操作，是自动化最明显的破绽。实测同一套流程跑两次，带 jitter 是 13.8s / 12.5s（有波动），纯等元素是 10.6s（每次都一样）。

## 什么时候真的得用 sleep

只有一种情况：**目标还没存在于任何页面里**。

典型：点「发布商品」后新开一个标签页，此时新页面还没创建，你没法在旧页面上等"新页面里的元素"。

```json
{"type": "web_js", "args": {"code": "...a.click()..."}},
{"type": "sleep", "args": {"ms": [1200, 2200]}},   ← 等标签开出来
{"type": "web_tab", "args": {"kind": "title", "title": "商品发布"}},
{"type": "web_wait", "args": {"text": "类目", "jitter": [400, 1000]}}  ← 新页面里等元素
```

即使这种情况，也要用**区间**而不是固定值。

## 完整示例（千牛发品选类目）

```json
{"id": "n1", "type": "web_open",  "args": {"url": "https://myseller.taobao.com/home.htm/QnworkbenchHome/", "until": "dom"}},
{"id": "n2", "type": "web_wait",  "args": {"target": "a:has-text('商品')", "kind": "visible", "limit_ms": 20000, "jitter": [300, 900]}},
{"id": "n3", "type": "web_click", "args": {"target": "a:has-text('商品')"}},
{"id": "n4", "type": "web_wait",  "args": {"target": "a:has-text('发布商品')", "kind": "visible", "limit_ms": 15000, "jitter": [400, 1100]}},
{"id": "n5", "type": "web_js",    "args": {"code": "() => {...a.click()...}"}},
{"id": "n6", "type": "sleep",     "args": {"ms": [1200, 2200]}},
{"id": "n7", "type": "web_tab",   "args": {"kind": "title", "title": "商品发布", "match": "contains"}},
{"id": "n8", "type": "web_wait",  "args": {"text": "{一级类目}", "limit_ms": 20000, "jitter": [400, 1000]}}
```

每个 `web_wait` 后面都跟抖动，每个 `limit_ms` 只是上限。

## 选择器要写在哪个元素上

**等"下一步要操作的那个元素"**，不要等一个无关的加载动画。

- ❌ `{"text": "加载中"}` —— 加载动画消失不代表内容到了
- ❌ `{"target": "body"}` —— 永远可见，等于没等
- ✅ `{"target": "a:has-text('发布商品')"}` —— 下一步就点它

## 反例清单

```json
// ❌ 固定 5 秒，页面 0.7 秒就好了
{"type": "sleep", "args": {"ms": 5000}}

// ❌ 等一个永远存在的元素，等于没等
{"type": "web_wait", "args": {"target": "body"}}

// ❌ 等"加载中"消失 ≠ 内容加载完
{"type": "web_wait", "args": {"text": "加载中", "kind": "hidden"}}

// ❌ 元素出现就立刻点，0ms 反应
{"type": "web_wait", "args": {"target": "#btn"}},
{"type": "web_click", "args": {"target": "#btn"}},

// ✅ 正确
{"type": "web_wait", "args": {"target": "#btn", "jitter": [300, 900]}},
{"type": "web_click", "args": {"target": "#btn"}},
```

## 排查："为什么还是找不到元素"

按顺序查：

1. **选择器对不对** —— 用 `web_has` 立刻验，别靠猜
2. **元素在不在当前 frame** —— 可能要先 `web_frame_in`
3. **元素在不在视口** —— 有些站要滚动才渲染，用 `web_click(kind="scroll")`
4. **是不是浮层遮挡** —— 报错里有 `intercepts pointer events` 就是这个，改用 `web_js` 直接 `element.click()`
5. **页面根本没渲染** —— 有些 SPA 要等接口返回，用 `web_wait(text="某段固定文案")`

---

# 写流程的工程技巧（千牛实测沉淀）

等待只是第一课。下面这些是写/录流程时反复踩出来的规则，违反任何一条都会在回放时炸。

## 技巧 1：元素库先行 —— 先拾取，再写流程

**别上来就写硬 CSS**。先跑一遍拾取流程（`flows/qn_pick_elements.json` 的做法：走一遍导航，`element_pick` 把关键元素扫进库），然后节点里写 `@el:名字`。

```json
{"type": "web_click", "args": {"target": "@el:qn_save_draft", "jitter": [600, 1500]}}
```

为什么：硬 CSS 改版即废；`@el:` 走八层降级定位（语义 → 属性 → label → 路径 → 邻居 → 几何），改版时大概率还能中。千牛流程 11 处硬 CSS 换成 `@el:` 后回放全绿。

**什么样的元素不适合入库**：文本随参数变化的（类目名、品牌名）—— 语义定位的 `name` 会跟着变，保持硬选择器 + `{变量}`。

**防点错**：关键点击加 `verify_text`，定位退化到错误元素时直接拒绝：

```json
{"type": "web_click", "args": {"target": "@el:qn_save_draft", "verify_text": "保存草稿"}}
```

## 技巧 2：React 受控输入必须真实键盘

`fill()` 对 React 受控组件无效（`input.value` 变了、React state 没变，字数计数器纹丝不动）：

```json
{"type": "web_type", "args": {"target": "@el:qn_title_input", "text": "{标题}", "kind": "type", "delay_ms": 70}}
```

`kind=type` 走 CDP `insertText`（真实键盘事件）。`dispatchEvent(new Event('input'))` 也不行。

## 技巧 3：Fusion 下拉的三步判据

`请选择` 是下拉**不是输入框**。「弹层能打开」≠「选上了」：

```
点开 input[placeholder='请选择']
 → 点 div.options-item          （li/[class*=option] 会数到容器，虚高）
 → 读 .next-select-values       （input.value 是搜索框，选中后仍为空）
 → 多选下拉不自动关 → web_key("Escape")
```

## 技巧 4：切标签前先清理历史标签

`web_tab` 按 `title` 匹配会撞旧标签 —— 千牛发品流程曾因此切到三天前的填表页。回放前把 `publish.htm` / `category.htm` 的历史标签关掉；HTTP `/run` 默认自动关（`?keep_pages=1` 才保留）。

## 技巧 5：关键点击用真实鼠标

淘宝/千牛的受控组件对 CDP 合成事件响应不稳，且拟人度是硬需求：

```json
{"type": "web_click", "args": {"target": "@el:qn_save_draft", "mode": "real", "hold_ms": 80}}
```

`mode=real` 是操作系统级 SendInput（窗口必须置前）；`mode=human` 是 CDP 层贝塞尔轨迹。发布、保存这类不可逆/敏感操作用 `real`，浏览类用默认 `cdp`。详见 [`REAL-MOUSE.md`](REAL-MOUSE.md)。

## 技巧 6：用 expect 断言"业务真的成功了"

节点返回 ok 只代表"脚本跑完了"。点保存后页面没跳、填了字但没进 React state —— 都是 ok 但业务失败。用 `expect` 做事后断言：

```json
{"id": "n29", "type": "web_click", "args": {"target": "@el:qn_save_draft"},
 "expect": {"url": "publish.htm", "limit_ms": 5000}}
```

## 技巧 7：易碎节点配 on_fail

等弹层、切标签这类偶发失败的节点，配重试而不是整条流程报废：

```json
{"id": "n7", "type": "web_tab", "args": {...}, "on_fail": {"mode": "retry", "attempts": 2, "backoff_ms": 1500}}
```

可选动作：`retry` / `skip`（容忍失败继续走）/ `back`（回退重跑区间）。**不可逆操作（提交、支付）永远不要配盲目 retry** —— 先查是否已成功。

## 技巧 8：节点输出用 save_as 显式落变量

`{n2}` 不是合法引用 —— 引擎不会把节点输出注册成同名变量。要拿输出必须：

```json
{"type": "web_elem_info", "args": {"target": "@el:qn_price_input", "attr": "value", "save_as": "cur_price"}},
{"type": "web_click",      "args": {"target": "@el:qn_save_draft", "verify_text": "{cur_price}"}}
```

## 技巧 9：浮层遮挡的两个解法

报错带 `intercepts pointer events` = 有浮层。先试 `force`，不行再 JS：

```json
{"type": "web_click", "args": {"target": "#btn", "force": true}}
{"type": "web_js",    "args": {"code": "() => document.querySelector('#btn').click()", "jitter": [300, 800]}}
```

## 技巧 10：iframe 的工具边界

`web_scan` 只扫主文档；`web_click` / `web_fill` / `web_read(kind=text|html|list|eval)` 跟随 frame 链。进 iframe 用 `web_switch(kind=iframe)`，出来用 `kind=main`。eval 现在也能进 iframe（`_eval_root` 解析真实 Frame）。

## 排查顺序（流程失败时）

1. `read_failure`（run_id）看失败节点 + 现场截图 —— 先看 `page_url` 是不是预期页面
2. 选择器（`web_has`）→ frame（`web_frame_in`）→ 视口（滚动）→ 浮层（`force`/JS）→ 渲染（等文案）
3. 元素库退化：`element_verify` 看 `degraded` 列表，重拾
4. 完整字段级排查见 [`TAOBAO-PUBLISH.md`](TAOBAO-PUBLISH.md) §8

---

# 流程功能性技巧（业界最佳实践 × 本引擎落地）

来源：RPA 落地实践（CSDN）、UiPath 脆弱性与漂移检测（Industrial Monitor）、无人值守桌面自动化（iTechGuides/Microsoft 指南）。左边是业界共识，右边是本引擎的对应落地。

## 1. 三层异常处理

| 层 | 业界做法 | 引擎落地 |
|---|---|---|
| ① 前置校验 | 关键操作前先验前置条件 | `web_wait`（等下一步要点的元素）+ `web_click(verify_text=...)`（点前验文本）|
| ② 容错重试 | 失败等几秒重试 3 次再进异常分支 | `on_fail: {"mode":"retry","attempts":3,"backoff_ms":2000}` |
| ③ 上报与人工 | 通知负责人、路由人工队列 | `notify` 节点 + n8n 失败分支（见 N8N 方案）|

## 2. 区分"可重试"与"不可逆"步骤

把每个步骤标记为**可逆**（导航、填表、开关浮层 —— 失败了重跑无害）或**外部承诺**（提交订单、发布商品 —— 产生外部副作用）。规则：

- 可逆步骤 → `on_fail=retry`
- 外部承诺步骤 → **绝不盲目重试**，先查是否已成功（幂等检查）再决定
- 千牛场景：保存草稿前先查草稿箱是否已有同名草稿（去重）

## 3. 检查点与取证

每个关键动作后留证据，失败时能回答"机器人当时看到了什么"：

- 引擎已有：失败自动存现场（DOM 摘要 + 表单字段 + 截图，`read_failure` 取回）
- 补充做法：关键节点后配 `expect` + 必要时 `web_capture` 截图；run_id 贯穿日志
- 业界提醒：截图只留必要的，别把敏感数据存进取证文件

## 4. 模块分层（Init / Main / End）

| 模块 | 职责 | 千牛对应 |
|---|---|---|
| Init | 定位环境、检查登录态、准备数据 | `web_open(kind=attach)` 接管浏览器 + `web_wait` 验登录 |
| Main | 核心业务 | 选类目 → 填表 → 保存 |
| End | 结果输出、清理临时状态 | 关历史标签、`element_verify` 体检、`save_as` 回传结果 |

分层后半年后排查能直接定位模块，不用从头读。引擎的对应物：拾取流程（Init）与主流程（Main）分开，不要混。

## 5. 选择器漂移检测

业界把"选择器失败次数"当作**漂移指标**来监控。引擎已内置：

- 定位失败记 `miss`，连续 3 次且 miss > hits → 元素标 `is_stale`
- `element_verify()` 体检：`degraded` 列表 = 需要重拾的元素；`layer` 列 = 现在靠哪一层命中（从 `role+name` 退化到 `path` 就是漂移前兆）
- 建议：每次改版后 / 每 N 次回放后跑一次体检

## 6. 数据、导航、业务规则分离

- **数据**（商品标题、价格、SKU）→ 流程 `vars` / `data_sheet_read`（Excel）→ 由 n8n 注入，不写死在节点里
- **导航**（点哪、等什么）→ 节点序列 + `@el:`
- **业务规则**（价格范围校验、必填判断）→ `if` / `expect` 显式分支，别藏在长 JS 里

## 7. 环境锁定（无人值守前置）

无人值守失败的大头在"逻辑步骤与物理会话的边界"：分辨率/缩放变了坐标点击全废、会话锁定时流程挂起、交互式弹窗卡死。落地清单：

- 专用执行机，**RPA 别和日常工作站混用**（或错峰）
- 固定分辨率/缩放/窗口布局，记入部署配置
- 阻止休眠锁屏；有头浏览器（`RPA_HEADLESS=false`）不能丢
- 登录态浏览器常驻（Ant + `RPA_CDP`），服务重启能自动找回（已实现 `out/_last_cdp.txt`）

## 8. 监控与告警阈值

业界基线，接 n8n 后直接可用：

- 连续 2 次失败 → 告警暂停调度
- 错误率周环比 +5% → 升级处理
- 平均耗时偏离基线 >30% → 检查页面改版
- 选择器失败次数上升 = 漂移，跑 `element_verify`
