# 淘宝上架流程 · RPA 实操知识

> 面向千牛/淘宝商品发布页的**实测记录**，不是通用教程。写流程、改选择器、排查失败前先读这一页。
> 关联流程：`flows/qn_publish_category.json`（选类目，16 节点）、`flows/qn_publish_draft.json`（端到端存草稿，30 节点）。
> 通用等待策略见 [`WAITING.md`](WAITING.md)，元素库机制见 [`ELEMENT-LIB.md`](ELEMENT-LIB.md)。

## 0. 结论先行

| 项 | 现状 |
|---|---|
| 链路 | 工作台 → 商品 → 发布商品 → 选类目（3 级）→ 填表页 → 保存草稿 |
| 回放 | 🟢 `qn_publish_draft` 30/30 全过、24.2s，草稿箱确认新增 |
| 字段覆盖 | 🔴 25 个必填里只填了 5 个（标题/导购标题/颜色/尺码/价格），主图按要求跳过 |
| 元素库 | 🔴 4 条、全是豆瓣测试站、`hits/misses` 全 0、流程里 **0 个 `@el:` 引用** |
| 接 n8n | 🔴 还差幂等去重、事后断言、登录态常驻（见 §7）|

## 1. 流程地图

```
┌─ qn_publish_category（16 节点 / 11~13s）─────────────────────────────┐
│ n1  打开工作台                                                        │
│ n2  等「商品」菜单 → n3 点                                             │
│ n4  等「发布商品」→ n5 JS 点（浮层遮挡）                                │
│ n6  sleep [1200,2200]   等新标签开出来（唯一无元素可等处）              │
│ n7  web_tab 切到「商品发布」                                           │
│ n8~n11  等类目树 → 选一级 → 等 → 选二级                                 │
│ n12~n15 等品牌区块 → 点下拉 → 等选项 → 选品牌 004                       │
│ n16 点「确认，下一步」 → 同标签跳到 publish.htm                          │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌─ qn_publish_draft 后半（n17~n30）───────────────────────────────────┐
│ n17 等「宝贝标题」渲染                                                 │
│ n18/n19 宝贝标题 / 导购标题（web_type kind=type）                       │
│ n20~n23 颜色：点开 → 等面板 → 选白色系 → 选白色                         │
│ n24~n27 尺码：点开 → 等弹层 → 选 175/88A → 确定                        │
│ n28 价格（批量工具）                                                   │
│ n29 保存草稿（JS 点，先查 disabled）                                    │
│ n30 sleep 等请求返回                                                   │
└──────────────────────────────────────────────────────────────────┘
```

**主图上传**（已用 MCP 全程验证，按用户要求暂不纳入流程）见 §3.6。

## 2. 逐节点注意事项

| 节点 | 注意点 | 判据（做对了长什么样）|
|---|---|---|
| `n1 web_open` | `until=dom` 够用；`jitter=[500,1500]`，页面到位别 0ms 动手 | URL = 工作台 |
| `n2/n4 web_wait` | 等**下一步要点的那个元素**，别等 `body`/加载动画 | 元素 visible |
| `n3 web_click` | `a:has-text('商品')`；点完 `jitter` | 菜单展开 |
| `n5 web_js` | 浮层遮挡时 `locator.click()` 会报 `intercepts pointer events`，改用 JS `a.click()` | 新标签出现 |
| `n6 sleep` | **唯一**保留的 sleep：新页面还没创建，没有元素可等。必须用区间 `[1200,2200]` | 标签数 +1 |
| `n7 web_tab` | 按 `title` 含「商品发布」切。**先清理历史标签**，否则会切到旧页 | URL = category.htm |
| `n8/n10 web_wait` | 用 `text={一级类目}`，参数是变量，别写死「男装」 | 文本出现 |
| `n9/n11 web_js` | 类目项是 `p/span/li` **不是 a/button**，React 事件委托，DOM 上没 onclick。用 `textContent` 精确匹配 + `width>0` 过滤隐藏项 | 选中项高亮 |
| `n12 web_wait` | 等「正确填写品牌信息」，比等某个输入框稳 | 品牌区块出现 |
| `n13/n14` | 品牌下拉：选项元素是 **`div.options-item`** | 选项列表出现 |
| `n15 web_js` | 精确匹配品牌文本，`filter` 后取 `[0]`；别用 `text=004`（会匹配多个） | `span.next-select-values` 显示 `004` |
| `n16 web_click` | 点完进填表页是**同标签跳转**，不是新标签 | URL = publish.htm |
| `n17 web_wait` | 填表页是 SPA，等「宝贝标题」文案 | 表单渲染完 |
| `n18 web_type` | **必须 `kind=type`**，`fill` 对 React 受控组件无效（计数器永远 0/60） | 计数器变 `28/60` |
| `n19 web_type` | **导购标题限 10~15 汉字**，超了报「填写错误」且保存草稿被拦 | 无错误提示 |
| `n20~n23` | 颜色是两级：先「白色系」→ 再「白色」；用 JS 精确匹配，`text=白色` 会歧义 | 主色框值 = `白色` |
| `n24~n27` | 尺码弹层文本含「全不选」，选项 `175/88A` 用 JS 点，再点「确定」 | 尺码框值 = `175/88A` |
| `n28 web_type` | 价格是批量工具框，纯数字，中文会被过滤掉 | 值回读 = `99` |
| `n29 web_js` | 保存草稿按钮可能被 `disabled`：先查，再点。**错误面板为空**才算能存 | 草稿箱数量 +1 |
| `n30 sleep` | 等请求返回，用区间 | — |

## 3. 按能力分组的硬规则

### 3.1 等待与抖动

**能等元素就绝不固定 sleep**，元素出现后加随机抖动。三版实测：

| 版本 | 耗时 | 特点 |
|---|---|---|
| 固定 sleep | 20.1s | 机械，每次一样 |
| 纯等元素 | 10.6s | 快，但 0ms 反应 |
| 等元素 + 全节点抖动 | 12.5~13.8s | 有随机波动，拟人 |

**支持 `jitter` 的节点**：`web_open` / `web_tab` / `web_wait` / `web_click` / `web_js` / `web_type`。
**漏一个就是一处 0ms 反应**，页面切换后尤其重要。详见 [`WAITING.md`](WAITING.md)。

### 3.2 React 受控输入

```json
{"type": "web_type", "args": {"target": "input[placeholder*='30个汉字']",
                              "text": "{宝贝标题}", "kind": "type", "delay_ms": 70}}
```

- ❌ `web_fill`（`fill()`）：`input.value` 变了，React state 没变，字数计数器仍 `0/60`
- ❌ `dispatchEvent(new Event('input'))`：同样不生效
- ✅ `web_type(kind=type)` 走 `page.keyboard.type()`（CDP `insertText`）
- 数字框过滤中文，只填纯数字

### 3.3 Fusion 下拉（18 个 `placeholder=请选择`）

**这类是下拉选择，不是输入框，别往里 type。**

```
点开 input[placeholder='请选择']
 → 点 div.options-item          ← 真正选项；li/[class*=option] 会数到容器（虚高 17 vs 实际 14）
 → 读 .next-select-values       ← 选中值在这里；input.value 是搜索框，选中后仍为空
 → 多选下拉不自动关 → web_key(Escape)
```

⚠️ **「弹层能打开」≠「下拉已选上」**。曾误报"5 个下拉验证通过"，实际只点了弹层没选值。验证必须走完三步。
实测回填：面料=三醋酯纤维、上市年份季节=2026年冬季、产地=中国。

### 3.4 浮层遮挡

报错 `... intercepts pointer events` = 有浮层。两种解法：

```json
{"type": "web_click", "args": {"target": "#btn", "force": true}}     // 首选
{"type": "web_js",    "args": {"code": "() => document.querySelector('#btn').click()"}}
```

`web_js` 点完要加 `jitter`（节点的 `_settle` 会处理）。

### 3.5 iframe（不是 shadow DOM）

**全页 0 个 shadow DOM。** 真正难点是 **4 个跨域 iframe**（图片选择器 `#mainImagesGroup` 在里面）。

| MCP 工具 | 能否进 iframe |
|---|---|
| `web_click` / `web_fill` / `web_upload` / `web_read(kind=text|html|list)` | 🟢 走节点 `_locate` → `frame_locator` |
| `web_read(kind=eval)` | 🟢 已修：`_eval_root()` 用 `content_frame().evaluate` 跟随 frame 链 |
| `web_scan` | 🔴 只扫主文档 |

节点名：`web_switch(kind=iframe)` = `web_frame_in`，`web_switch(kind=main)` = `web_frame_out`。

### 3.6 主图上传（完整路径，已验证）

```
web_click(div.image-empty)                        打开「选择图片」弹层
 → web_switch(kind=iframe, target=iframe#mainImagesGroup)   跨域 iframe
 → web_click(text=本地上传)                      动态生成 input[type=file]
 → web_fill(kind=upload, target=input[type=file], value=图片路径)
 → web_click(text=完成)                         关上传结果页
 → web_click(css=.PicList_checkbox__769at >> nth=0)   勾选第一张（悬停后右上角选择框）
 → web_frame_out → 点弹层外部（如 text=基础上传要求）  ← **弹层内没有确定按钮**
```

判据：主图区出现 `img`、空位 5→4、弹层关闭。
要求：比例 1:1、≤20MB、png/jpg/jpeg、最多 5 张。图片空间里也可能有现成图可直接选。

### 3.7 键盘

`web_key`（CDP 原生按键）：`Escape` 关浮层、`Control+A` 全选、`kind=type` 逐字输入。
**合成 `KeyboardEvent` 关不掉 Fusion 浮层**（实测派发后弹层仍为 1）。

### 3.8 保存草稿

- 按钮可能 `disabled`，点之前先查（必填项没齐）
- 导购标题不足 10 汉字或超 15 汉字 → 错误面板「导购标题 填写错误」，保存被拦
- 保存后**不跳转**，URL 仍是 publish.htm；判据是去草稿箱看数量
- 草稿箱实测已累积 6 条测试草稿 → **没有幂等去重**

### 3.9 页面与标签

- `web_tab` 按标题切前**先清理历史 publish.htm / category.htm 标签**，否则切到旧页（曾导致 n9 找不到类目）
- HTTP `/run` 默认关掉本次新开的标签（防泄漏）；交互探索用 `?keep_pages=1` 保留结果页
- MCP `flow_run` 优先复用 `web_open(kind=attach)` 接管的浏览器；没设 `RPA_CDP` 也不会 launch 无登录态新浏览器

## 4. 填表页字段地图（实测）

```
8455 DOM 节点 / 0 shadow DOM / 4 跨域 iframe
83 个 input/textarea/select：58 可见 / 25 不可见 / 20 readonly / 7 disabled
18 个 placeholder=请选择（下拉）、7 个「请输入」、2 个「数字」
25 个必填标记、5 个上传区（无静态 input[type=file]）
```

必填项：当前类目、1:1 主图、宝贝标题、商品属性（版型/面料/品牌/上市年份季节/袖长/功能/领型/适用性别…）、采购地、一口价、总库存、库存扣减方式、上架时间、发货时间、提取方式、电脑端描述、宝贝详情。

## 5. MCP 工具覆盖复查

**结论：淘宝上架流程用到的节点，MCP 100% 可达，功能好用。** 49 个封装工具 + `call_node` 可直达全部 122 个节点。

| 流程节点 | MCP 入口 | 实测 |
|---|---|---|
| `web_open` | `web_open` | 🟢 |
| `web_wait` | `web_wait` | 🟢 |
| `web_click` | `web_click` | 🟢 |
| `web_js` | `web_read(kind=eval)` | 🟢 含 iframe 内 |
| `web_type` | `web_fill(kind=text)` / `call_node(web_type, kind=type)` | 🟢 React 回填 |
| `web_tab` | `web_switch(kind=tab)` | 🟢 |
| `web_frame_in/out` | `web_switch(kind=iframe/main)` | 🟢 |
| `web_upload` | `web_fill(kind=upload)` | 🟢 图片上传 |
| `web_key` | `web_key` | 🟢 Escape 关浮层 |
| `sleep` | ⚠️ **无同名工具** → `call_node("sleep", {"ms":...})` | 🟢 |
| 元素库 | `element_pick` / `element_list` / `element_verify` | 🟢 闭环可用 |

**没有 MCP 同名工具的两个**：`sleep`、`web_upload`（后者是 `web_fill(kind=upload)`）。别按节点名直接当工具名调。

之前用 Python 脚本直连 CDP 干的操作（递归字段扫描、下拉探测、草稿箱核对、真实键盘输入），MCP 全部能复现 —— 脚本是不必要的。

## 6. 元素库体检（2026-11-06）

| 指标 | 实测值 | 评级 |
|---|---|---|
| 条目数 | **4** | 🔴 |
| 来源 | 全部 `source=picker`，豆瓣公开测试站 | 🔴 |
| `hits` / `misses` | 全部 `0 / 0` —— 从未被定位过 | 🔴 |
| 流程里的 `@el:` 引用 | **0 个** | 🔴 |
| 真实业务元素（千牛/淘宝） | **0 个** | 🔴 |
| 文件 | `elements.json` 在 `.gitignore`，干净 clone 必然空库 | 🟡 设计如此 |

**不是"漏跑"，是没启用**：元素库是**手动拾取**机制（`element_pick`），流程跑多少次都不会自动入库。文档 `ELEMENT-LIB.md` 明确写了「干净 clone 必然得到空库」。目前 4 条都是早期做豆瓣示例时拾取的，跟淘宝流程完全无关。

**补强动作（建议）**：

1. 在填表页用 `element_pick` 拾取关键元素：发布商品链接、一级/二级类目项、品牌选项、确认下一步、宝贝标题、导购标题、主色框、尺码框、保存草稿。
2. 流程里把 CSS 换成 `@el:xxx`，让八层降级定位生效（现在全靠硬选择器，改版即废）。
3. 定期 `element_verify()` 体检，`degraded` 列表就是需要重拾的元素。
4. 想让库随流程自动长大，需要新增「定位成功后自动拾取」的钩子 —— 目前引擎没有。

## 7. 接 n8n 前必须补齐

| # | 缺口 | 说明 |
|---|---|---|
| 1 | 字段覆盖 100% | 25 个必填只填了 5 个；18 个下拉只验证了 5 个能回填 |
| 2 | 幂等去重 | 草稿箱已累积 6 条，跑两次就两个草稿。按外部单号查重 |
| 3 | 事后断言 | 30 节点里 9 个是 `web_js`，节点 ok ≠ 业务成功。用 `expect` 字段 |
| 4 | 登录态常驻 | Ant 手动开的浏览器 + `RPA_CDP=59493`；服务重启/浏览器关就断 |
| 5 | 必须有头 | `RPA_HEADLESS=false`，不能丢到无界面服务器 |
| 6 | 连跑 3 次稳定 | 只跑一次绿不算 100% |

n8n 入口：`POST http://127.0.0.1:8100/run/qn_publish_draft`（或 `/run/{name}/async` + `/task/{id}`）。

## 8. 排查速查

| 现象 | 按顺序查 |
|---|---|
| 找不到元素 | 选择器（`web_has`）→ frame（`web_frame_in`）→ 视口（滚动）→ 浮层遮挡 → 页面没渲染（等文案）|
| 填了但没生效 | React 受控：换 `web_type(kind=type)`；看字数计数器是否变化 |
| 下拉点开选不中 | 点的是容器 → 改 `div.options-item`；看 `.next-select-values` 不是 `input.value` |
| 保存草稿没反应 | 按钮 `disabled`？导购标题 10~15 汉字？错误面板是否为空？ |
| 流程切错页面 | 清理历史 publish/category 标签；`web_tab` 按标题切 |
| `flow_run` 跳登录 | 先 `web_open(kind=attach)` 接管已登录浏览器 |
| 回放产生重复草稿 | 没有幂等键，见 §7 |
