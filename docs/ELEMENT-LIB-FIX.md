# 元素库改造：让六维指纹真的用上（2026-11-03）

## 一、改之前的问题

设计上存了六维指纹，但**定位只用第 1 层（role+name）**，其余 5 维存着没人用。
而第 1 层恰好质量最差。

| # | 问题 | 实测证据 |
|---|---|---|
| 1 | **`name` 经常为空** | `input[name=custname]` → `role=textbox name=''`。空输入框的 `value` 是空串，跳过它之后 placeholder/title 都没有 → 生成 `role=textbox[name=""]`，匹配全页所有输入框 |
| 2 | **`role` 一刀切** | `input[type=radio]` → `role=textbox`（应该 radio）。`tagRole = {INPUT: "textbox"}` 没看 type |
| 3 | **歧义静默取第一个** | `role=link[name="(about)"]` 在 quotes 上命中 **10 个**，定位器直接 `.first`，点哪个全看页面顺序 |
| 4 | **path 太脆** | 全是 `html:nth-child(1) > body:nth-child(2) > ...`，深度 5-9 层，任何兄弟增删就崩 |
| 5 | 容器元素没法用 | `div.quote` → `role=None, name='“The world as we ha'`（截断 80 字） |

## 二、采集端（elements/capture.py）

### name 改用简化版 accname

```
aria-labelledby（解析引用元素的文本）
  → aria-label
  → label[for=id]
  → 包裹的 <label>
  → placeholder / title / alt
  → 有值的 value（仅非空）
  → innerText
```

**效果**：

| 元素 | 改前 | 改后 |
|---|---|---|
| `input[name=custname]` | `textbox` / `''` | `textbox` / **`'Customer name:'`** |
| `input[type=radio]` | `textbox` / `'small'` | **`radio`** / `'Small'` |
| `input[type=checkbox]` | `textbox` | **`checkbox`** / `'Bacon'` |

### role 按 input type 细分

```
checkbox / radio / submit|button|reset → button / search → searchbox
range → slider / file → button / hidden → null / 其余 → textbox
```

标签推断也补全：`NAV→navigation`、`UL→list`、`LI→listitem`、`P→paragraph`、`DIALOG→dialog` 等。

### path 稳定化

优先锚点，最后才 nth-of-type：

```
#id > [data-testid] > [name] > tag（同标签唯一）> tag:nth-of-type(n)
```

**效果**：

| 元素 | 改前 | 改后 |
|---|---|---|
| `input[name=custname]` | `html:nth-child(1) > body:nth-child(2) > ...`（6 层） | **`input[name="custname"]`** |
| 表单按钮 | 6 层 nth-child | `html > body > form > p:nth-of-type(6) > button` |

## 三、定位端（elements/resolve.py，新增 300 行）

### 六层降级链

| 层 | 依据 | 权重 | 说明 |
|---|---|---|---|
| 1 | role + name | 20+20 | 最稳，改文案才失效 |
| 2 | attrs | testid 28 / id 30 / name 18 / type 8 | 锚点 |
| 3 | css（显式存的） | — | 手工兜底 |
| 4 | path | 15 | 稳定化后的父链 |
| 5 | siblings | 10 | 先锁父容器再找子元素 |
| 6 | geometry | 5 | 文档坐标最近（容差 40px） |

### 命中多个时打分

用元素库里存的指纹逐维比对加分，取最高分：

- 最高分比第二名高 **≥10 分** → 直接用它
- 差距小或完全并列 → **报歧义**，列出前 5 个候选（tag + 文本 + 分数），提示重新拾取

**绝不静默取第一个**。

### 连续失败记 miss

定位失败时给元素库记一次 `miss`（连续 3 次 → `is_stale`，提示重拾）。
只记 miss 不记 hit —— hit 在循环里高频触发，每次落盘不值得。

## 四、验证

### 关键验证：10 个同名元素能否选中正确的

quotes 页面上有 10 个 `role=link[name="(about)"]`，name 完全相同。

```
拾取第 4 条的 about 链接 → @el:n_austen
用 @el:n_austen 定位 → href = /author/Jane-Austen   ✅ 正确的那个
（第 1 个是 /author/Albert-Einstein）
```

**靠 path + siblings + geometry 打分区分开的** —— 这就是"六维用上"的实证。

### 旧元素库兼容

你已有的 `baidu_search_box` / `baidu_submit`（旧格式指纹，path 全 nth-child）：

```
@el:baidu_search_box → ✅ 定位成功，真输入成功
@el:baidu_submit     → ✅ 定位成功
```

靠 role+name + path 降级兜底能工作。但旧条目质量差（`baidu_search_box` 的 name 抓到了热搜词），**建议重拾一遍**。

### 测试

```
576 passed（新增 7 个 resolve 单元测试）
```

## 五、还没做的

| 项 | 说明 |
|---|---|
| path 的 frame 链 | 抓到 `frame: []` 了，但定位时没用于切 iframe |
| siblings 打分没真正参与 | 只在降级层用，`_score_candidates` 里没算 siblings（怕误伤）|
| geometry 只用于兜底 | 没参与主打分（坐标会随分辨率/缩放变）|
| 元素库历史趋势 | `hits/misses` 存了，但没做"这个元素历史上多常失效"的统计视图 |

---

## 六、补：MCP 层终于能碰元素库了（原来完全碰不到）

**发现**：20 个 MCP 工具里**一个元素库工具都没有**（`grep pick_element` 零命中）。
元素库只在流程里能用（节点 `pick_element`），Agent 通过 MCP **完全够不着** ——
前面那些拾取验证都是我绕到内部 `_call()` 调的，不是 Agent 能用的路径。

### 新增三个工具

| 工具 | 作用 |
|---|---|
| `element_pick(selector, key)` | 拾取：一次调用把元素存进库，之后 `@el:key` 引用 |
| `element_list()` | 列库：key / role / name / hits / misses / stale |
| `element_check(keys?)` | **体检**：在当前页面逐个验证，报告"靠哪一层命中" |

`element_check` 是判断"元素库还准不准"的最直接手段——页面改版后跑一次就知道哪些坏了。

### 实测

**同页面（首次即检查）**：3/3 通过

```
p_next     layer=role+name(simplified)   ← 最稳的一层
p_author3  layer=text(scored)            ← 无 role，靠文本 + 打分
p_quote2   layer=path                    ← 纯文本容器，只能靠结构
```

**关页面重开（模拟跨会话）**：3/5 通过
（失败的是 `baidu_search_box`/`baidu_submit`——因为当前在 quotes 页面，不在百度，**检测正确**）

**换页面（模拟改版）**：0/2 全失败 —— 检测能力验证通过

**真用**：

```
@el:p_next   → href=/page/2/          ✅
@el:p_author3 → text='Albert Einstein' ✅
web_click(@el:p_next) → 真翻到 /page/2/ ✅
```

### 修的两个定位问题

1. **`role+name` 层 0 命中**：Playwright 的 accname 会规范化掉符号
   （我们存 `Next →`，它算出 `Next`）→ 加 `_simplify_name()` 重试，
   `p_next` 从降级到 path 变成 `role+name(simplified)`
2. **页面刚 dom 完就检查会 0 命中**：加"全失败后等 1 秒重试一次"
   （实测：`open_page until=dom` 返回时页面内容还没画完）

### 现在"方便/持久"的真实答案

| 问题 | 答案 |
|---|---|
| 定位一次方便吗 | 🟡 **一次调用**，但仍要写 CSS selector（没有"页面里点一下就拾取"的 UI）|
| 跨会话持久吗 | 🟢 存在 `elements.json`，关浏览器重开仍能定位 |
| 跨机器持久吗 | 🟢 文件跟项目走；geometry 会失效但只用于兜底 |
| 页面改版后 | 🟡 语义层命中就稳；降级到 path/text 说明已退化；全失败会报错 + 记 miss |
| 能提前发现退化吗 | 🟢 `element_check` 一次跑完，`degraded` 列表就是要重拾的元素 |

---

## 七、继续优化：不用写 CSS + iframe 支持

### 1. `web_scan`：先看页面上有什么，再决定拾取谁

Agent 读不到"页面上有哪些按钮"，只能靠猜 CSS。新增扫描节点（`scan_interactive`）+ MCP 工具 `web_scan`：

```
web_scan() → 36 个元素：
  link      Quotes to Scrape        (8,21)   233x36
  link      Login                   (8,80)   38x17
  link      (about)                 (856,114) 46x17
  ...
```

带 `role + name + 坐标 + 尺寸 + disabled`，已去重、已过滤不可见。

### 2. `element_pick(text=...)`：不用写 CSS

```
element_pick(text="Next", key="next")        → role=link
element_pick(text="iframe 里的输入框", ...)   → 兜到 placeholder 匹配
```

给 `text` 时先试属性组（placeholder / aria-label / title / name），再退回 `text=`——
输入框这类元素没有可见文本，只有 placeholder。

### 3. iframe 支持（这次踩了三个坑）

**坑 1**：`pick` 只在 `page.locator()` 找，iframe 里的元素永远超时
→ 改成主 frame 找不到就遍历 `page.frames`

**坑 2**：JS 的 `window.frameElement` 在 **file:// 协议**下是 null（每个文件都是独立源），
跨域 iframe 同理
→ 改用 Playwright 的 `frame.frame_element()`（走 CDP，不受同源限制）反查 frame 链

**坑 3**：`element_check` 没切 frame，iframe 内的条目全部误报失败
→ 体检时按 `fingerprint.frame` 逐层 `frame_locator`

**实测（本地 iframe 测试页）**：

```
web_scan → 3 个：外层按钮 / iframe 里的输入框 [iframe] / iframe 里的按钮 [iframe]

element_pick(text="iframe 里的输入框") → frame=[{"selector": "#inner"}]
@el:h_input 定位 → ok，layer=role+name     ← 最稳的一层
web_fill(@el:h_input, "回归通过") → 真填进去
回读 value → "回归通过"
element_check → 2/2 通过，layer 都是 role+name
```

### 现在的完整链路

```
web_open      打开页面
web_scan      看有哪些可点的（含 iframe）
element_pick  一次调用拾取（text= 不用写 CSS，自动带 frame 链）
web_click     用 @el:name 操作（六维降级定位）
element_check 定期体检，看哪些退化了
```
