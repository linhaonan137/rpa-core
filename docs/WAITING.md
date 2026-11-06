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
