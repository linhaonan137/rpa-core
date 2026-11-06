# rpa-core

> **平台：Windows**（依赖 SendInput / uiautomation / Win32 窗口操作）

用 JSON 描述流程的 RPA 内核。**能被 AI Agent 直接调用**，也能脱离 Agent 独立跑（0 token）。

```
CLI / EXE    rpa.exe run flows\xxx.json      <- 人 / 定时任务
HTTP         POST /run/{name}                <- n8n / 其它系统
MCP          工具层（见 docs/MCP-TOOLS.md）    <- AI Agent
                    |
              引擎（DAG 调度 + 节点 + 元素库）
                    |
              Playwright / 系统能力
```

## 能力

> 各域节点数随版本变动，准确值以 `list_node_types` 为准；总量只在文末「规模」一节维护。

| 域 | 节点 | 覆盖 |
|---|---|---|
| `web_*` | 45 | 开页 / 点击 / 输入 / 等待 / 截图 / 下载 / 上传 / iframe / 弹窗 / JS / 网络监听 / 元素扫描 / 表单字段发现 |
| `fs_*` | 14 | 读写 / 追加 / 复制 / 移动 / 删除 / 改名 / hash / 对比 / 目录 |
| `seq_*` | 14 | 排序 / 去重 / 过滤 / 展平 / 映射 / 分块 / 求和 / 交集 / 笛卡尔积 |
| `flow_*` | 13 | if / while / loop / foreach / break / continue / subflow / sleep |
| `data_*` | 8 | Excel 读写追加 / CSV / 正则 / 模板 / JSON |
| `llm_*` | 6 | 对话 / 看图 / 字段抽取 / 归一化 / 分类 / 找选择器 |
| `map_*` | 6 | 嵌套取值 / 展平 / 合并 / 过滤 / 排序 / 遍历 |
| `media_*` | 5 | 视频 / 音频 / 播放列表 / m3u8 / 元信息 |
| `trig_*` | 5 | 时间 / API / 文件 / 热键 / webhook |
| `ui_*` | 3 | 桌面表格批量抓取 / 控件文字 / 焦点控件 |
| `sys_*` | 2 | 系统命令 / Python 脚本（子进程隔离，超时真杀）|
| `net_*` | 1 | HTTP 请求 |

## 快速开始

```bash
pip install -r requirements.txt
playwright install chrome chromium   # chromium 作为无系统 Chrome 时的回退

python main.py run flows/demo.json      # 跑流程
python main.py serve --port 8100        # HTTP 服务（含异步队列）
python main.py mcp                      # MCP 服务
pytest tests -q                         # 全量测试
```

打包单文件：

```bash
pyinstaller rpa.spec
rpa.exe run flows\demo.json
```

## 流程格式

```json
{
  "name": "抓取并落表",
  "nodes": [
    {"id": "n1", "type": "web_open", "args": {"url": "https://example.com", "until": "dom"}},
    {"id": "n2", "type": "web_extract", "args": {
      "item": "div.item",
      "fields": [{"name": "标题", "selector": "h3"}, {"name": "价格", "selector": ".price"}],
      "save_as": "rows"
    }},
    {"id": "n3", "type": "data_sheet_write", "args": {"path": "out/result.xlsx", "rows": "{rows}"}}
  ],
  "edges": [
    {"id": "e1", "from": "n1", "to": "n2"},
    {"id": "e2", "from": "n2", "to": "n3"}
  ],
  "vars": [{"name": "keyword", "value": "手机"}]
}
```

⚠️ **节点输出必须用 `save_as` 显式存成变量**，再用 `{变量名}` 引用。
`{n2}`（拿节点 id 当变量名）**不是合法引用** —— 引擎不会自动把节点输出注册成同名变量，写了会报"变量未定义"。

⚠️ **`type` 是节点名，不是 MCP 工具名**。写流程前先 `list_node_types` / `inspect_node` 确认，别拿工具名直接填。
已知不同名的（都是实测踩出来的）：

| MCP 工具 | 节点名 |
|---|---|
| `web_read`（text/value/html/attr）| `web_elem_info` |
| `web_read`（list）| `web_matches` |
| `web_read`（eval）| `web_js` |
| `web_read`（children / siblings）| `web_children` / `web_siblings` |
| `web_switch` | **`web_tab`** |
| `web_fill` | `web_type` |
| `web_network` | `web_net` |

`web_open` / `web_click` / `web_extract` / `web_wait` / `web_key` 是同名的。

⚠️ `sleep` 和 `web_upload` **没有同名 MCP 工具**：前者用 `call_node("sleep", {"ms": ...})`，后者用 `web_fill(kind=upload)`。

### iframe、键盘、流程回放的三条边界（实测）

| 场景 | 正确做法 |
|---|---|
| 进了 iframe 要跑任意 JS | `web_switch(kind=iframe)` 之后 `web_read(kind=eval)` 会在 **iframe 内** 执行（内部走 `content_frame().evaluate`）。`web_scan` 仍只扫主文档 |
| 关 Fusion 浮层、全选、逐字输入 | 用 `web_key`（`Escape` / `Control+A` / `kind=type`）。**合成 `KeyboardEvent` 关不掉弹层**，必须 CDP 原生按键 |
| `flow_run` 回放 | 优先复用 `web_open(kind=attach)` 接管的浏览器。MCP 服务没设 `RPA_CDP` 也不会再 launch 一个无登录态的新浏览器（否则千牛直接跳登录页）|
| **判断 Fusion 下拉是否选中** | 读 `.next-select-values` 的文本，**不要读 `input.value`**（那个 input 是搜索框，选中后仍为空）。弹层里的选项元素是 `div.options-item`，点它才回填；`li/[class*=option]` 会数到容器，虚高 |

⚠️ 「弹层能打开」≠「下拉已选上」。验证下拉必须走完 `点开 → 点 div.options-item → 读 .next-select-values`，只读弹层存在会误判成通过。

### 顶层契约

`extra="forbid"` —— **键名写错直接报错**，不会静默忽略。

| 键 | 必填 | 说明 |
|---|---|---|
| `name` | ✓ | 流程名 |
| `version` | — | 格式版本，默认 1 |
| `nodes` | — | `id` / `type` / `args` / `label` / `off` / `on_fail` / `expect` / `budget_ms` |
| `edges` | — | `id` / `from` / `to` / `exit`（`"true"` / `"false"` 表示条件分支出口，普通边留空）|
| `vars` | — | 流程变量，节点参数里写 `{名}` 插值 |
| `elements` | — | **流程内元素 manifest**，是全局元素库的覆盖层（见「元素库」节）|

`_` 开头的键是元数据，引擎忽略（自愈痕迹写在这里）。

参数化：节点里写 `{变量名}`，跑时 `--var keyword=平板` 覆盖。

节点级可选项：

```json
{
  "id": "n2",
  "type": "web_click",
  "args": {"target": "@el:submit"},
  "on_fail": {"mode": "retry", "attempts": 2, "backoff_ms": 800, "rollback_to": "n1"},
  "expect": {"target": "@el:success", "state": "visible", "limit_ms": 5000},
  "budget_ms": 15000,
  "off": false
}
```

## 元素库：六维指纹 + 逐层降级

> ⚠️ **仓库不带元素库**：`elements.json` 在 `.gitignore` 里，首次使用需先 `element_pick` 拾取。任何"clone 下来直接跑"且写了 `@el:` 的流程都会定位失败，这是已知行为。

不存单一 CSS 选择器（改版即废）。每个元素存六维身份，定位时按稳定性降级：

| 层 | 依据 | 抗什么变化 |
|---|---|---|
| 1 | `role` + `name`（可访问性语义）| 改 class、改结构 |
| 1.2 | 表单 label 关联（上方 24px 的标签文本）| 大量同 placeholder 的输入框 |
| 1.5 | 文本内容 | 无语义标签（`p` / `div` 按钮）|
| 2 | `id` / `data-testid` / `name` / `type` | 文案改版 |
| 3 | 显式 css | — |
| 4 | 稳定 path | — |
| 5 | 邻居文本 | — |
| 6 | 文档坐标最近 | — |

命中多个时加权打分，分差过小**报歧义并列出候选**，绝不静默点第一个。位置完全重合的重复 DOM（SSR 水合残留）自动放行。

```python
element_pick(role="link", text="提交订单", key="submit")   # 拾取
web_click(target="@el:submit")                            # 使用
element_verify()                                          # 体检（不传参=全库）
```

## 录制

```python
record_start(variables={"title": "测试商品"})
web_open(kind="attach", cdp="http://127.0.0.1:9222", url="example.com")
element_pick(role="button", text="发布", key="publish")
web_click(target="@el:publish")
record_stop(flow="我的流程")
```

自动处理：前置补接管页节点 / 导航后补等待 / `{{变量}}` 转 `{变量}`。

## 真实鼠标

`web_click(mode="real")` 走 OS 级 `SendInput`，不是 CDP 合成事件。三个已踩的坑：

1. 独立的 `WinDLL('user32')` 实例（防 argtypes 交叉污染）
2. DPI awareness（150% 缩放下坐标偏移）
3. 窗口必须在前台（`AttachThreadInput` + `SetForegroundWindow`）

## 反检测自检

`navigator.webdriver` / 插件 / GPU / 窗口几何 / 事件 `isTrusted` / `pointerdown` 坐标精度——逐项可查。CDP 点击默认带亚像素抖动（真人点击在高 DPI 屏上必然带小数）。

## 架构

```
engine/          引擎（约 3,650 行）
  schema.py        流程/节点/边模型 + 校验
  graph.py         图解析（后继/前驱/可达/循环体）
  scheduler.py     调度（波次执行 + 死路剪枝）
  runner.py        执行器（节点驱动 + 断点续跑）
  context.py       变量作用域 + 模板解析
  middleware.py    页面中间件（拟人点击/懒滚动/弹窗）
  real_mouse.py    OS 级真实鼠标
  recorder.py      操作录制
  stealth.py       反检测

nodes/          122 个节点（12 个域）
elements/       元素库（六维指纹 + 降级定位）
api/            mcp.py（工具层）/ http.py（HTTP 路由）/ taskqueue 队列
heal/           失败分类 + 元素自愈候选
store/          SQLite（执行记录 + 耗时趋势）
flows/          示例流程（含真实站点）
tests/          单元测试
```

## 已定决策（别推翻）

**引擎五条硬约束** —— 少一条都会退化成玩具：

1. **图解析**：nodes / edges → 前驱后继表 + 起始节点 + 孤立节点告警
2. **调度**：引用计数 join + 死路可达性重算（治分支汇合死锁 / 重复执行）
3. **注册表**：`type → 函数`，重复注册启动即报错
4. **postAssert**：动作成功 ≠ 事情成了
5. **errorPolicy**：`retry-self` / `skip`（循环里逐行容错必用）

**定位与数据**：

| 决策 | 一句话理由 |
|---|---|
| 落盘 SQLite 优先，Excel 只在交付要格式时导出 | Excel 无真追加、并发写坏文件、104 万行上限 |
| 元素库 = 全局库 + 流程内 `elements` manifest | 全局保证"改一处全生效"，manifest 保证流程能独立交付 |
| 定位 ARIA 优先（role+name → attrs → path → 邻居 → 坐标）| 改版通常只改样式不改语义 |
| 元素库不入库 | 含页面指纹与登录上下文，且干净 clone 应为空库 |

**边界**：

| 决策 | 一句话理由 |
|---|---|
| 桌面只留"表格批量抓取" | 其余桌面能力被 desktop-touch 全覆盖且更安全，不自建 |
| 不做可视化画布 | JSON + Agent 生成足够，画布是纯成本 |
| 自愈不走 MCP | 同进程直接调内核函数，绕协议是纯开销 |
| 浏览器单实例串行 | 「哪个流程在哪个浏览器」答案唯一，管理问题自动消失 |

## 异步执行 + 队列

长流程不必阻塞调用方。**浏览器是独占资源**，所以队列默认并发度 1，任务串行跑。

```python
# 提交（立刻返回）
r = flow_run_async(flow="淘宝发品", vars={"title": "商品A"})
# -> {"task_id": "t-xxx", "status": "queued", "queue_position": 2}

# 查进度
flow_task(task_id="t-xxx")     # queued / running / done / failed / cancelled
flow_tasks()                   # 队列概览：并发度 + 各状态计数 + 最近任务
flow_cancel(task_id="t-xxx")   # 排队中 / 执行中都能取消
```

HTTP 等价：

```
POST /run-async/{name}   入队，返回 task_id
GET  /task/{task_id}     查状态（含 run_id、失败节点）
GET  /tasks              队列概览
```

取消行为：排队中直接标记取消；执行中调用 `task.cancel()`，流程在最近的 `await` 点停下（实测 0.3 秒内生效）。页面可能停在操作中途——和节点报错留下的状态一样，用 `read_failure` 看现场。

n8n 用法：触发 → 拿 task_id → 轮询 `/task/{id}` → 或直接不管（队列会跑完，记录留在 SQLite）。

## 失败现场

流程失败时自动存快照：URL / 标题 / 可交互元素 / 表单字段 / 截图 / 每节点错误。保留最近 30 份。

```python
read_failure()               # 最近一次失败概要
read_failure(level="dom")    # 带当时的 DOM 清单
```

## 示例流程

`flows/` 里有可直接跑的示例：

| 文件 | 节点 | 演示什么 |
|---|---|---|
| [`demo.json`](flows/demo.json) | 5 | 循环 + 变量 + 数据落盘 |
| [`demo_live.json`](flows/demo_live.json) | 4 | 抓 quotes 摘句并落表 |
| [`real_1_quotes.json`](flows/real_1_quotes.json) | 8 | 分页抓取（静态站）|
| [`real_2_saucedemo.json`](flows/real_2_saucedemo.json) | 10 | 登录 + 购物车 + 断言（React SPA）|
| [`real_3_httpbin.json`](flows/real_3_httpbin.json) | 9 | 原生表单提交（无 JS）|
| [`real_4_demoqa.json`](flows/real_4_demoqa.json) | 10 | 复杂 SPA 表单（React + 广告 iframe）|

```bash
python main.py run flows/real_1_quotes.json
```

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/MCP-TOOLS.md`](docs/MCP-TOOLS.md) | 工具层：粒度设计、参数约定、已知缺陷 |
| [`docs/ELEMENT-LIB.md`](docs/ELEMENT-LIB.md) | 元素库：字段、采集、八层降级定位 |
| [`docs/SELF-HEAL.md`](docs/SELF-HEAL.md) | 自愈五层、诊断表、变更记录、验证方式 |
| [`docs/REAL-MOUSE.md`](docs/REAL-MOUSE.md) | 真实鼠标：实现与四个坑 |
| [`docs/LESSONS.md`](docs/LESSONS.md) | 踩坑台账（按「再犯一次会疼多久」排序）|
| [`docs/WAITING.md`](docs/WAITING.md) | **等待策略 + 写流程的工程技巧 + 业界功能性技巧（异常三层/可逆分类/漂移检测/环境锁定）** |
| [`docs/TAOBAO-PUBLISH.md`](docs/TAOBAO-PUBLISH.md) | **淘宝上架流程实操知识：逐节点注意事项、MCP 覆盖、元素库体检** |
| [`docs/RPA-N8N-PLAN.md`](docs/RPA-N8N-PLAN.md) | **接入 n8n 方案：定时上架架构、未用功能落地映射（P0/P1/P2）、验收标准** |
| [docs/KNOWN-ISSUES.md](docs/KNOWN-ISSUES.md) | **问题台账：45 条已知问题的「已修/延后/不改」+ 必须修的时机** |

> 文档纪律：**会变的数字只在下方「规模」一节维护**，其余文档一律指向代码（`list_node_types` / `api/mcp.py`），避免同一事实在多处腐烂。

## 规模

```
122 节点 · 48 MCP 工具 · 579 测试
引擎 3,656 行 / 测试 6,223 行
```

> **本节是全文唯一维护数字的地方。** 数字变化只更新这里；`docs/` 下不再重复任何会变的数字。
> 核对口径：`list_node_types`（节点数）、`api/mcp.py` 中 `@mcp.tool` 计数（工具数）、`pytest --collect-only`（测试数）。

## 验证状态

| 层 | 覆盖什么 | 怎么重跑 |
|---|---|---|
| 单元测试 | 引擎 / 节点 / 元素库 / 自愈 / 存储 | `python -m pytest tests -q` |
| MCP 工具矩阵 | **全部工具**，每个都有「正常 / 边界 / 应报错」三类用例 | `python tests/mcp_matrix.py` |
| HTTP 端点 | 全部路由（TestClient，不起服务） | `pytest tests/test_misc.py` |
| 真实站点 | 豆瓣首页、豆瓣电影 Top250（抓 25 部落 Excel，换 `vars.url` 跑第 2 页） | `python main.py run flows/douban_top250.json` |

矩阵判定只有三种：`PASS`（该成功）、`EXP-FAIL`（该报错）、`INFO`（信息性）。
**出现 `UNEXPECTED-OK` / `UNEXPECTED-FAIL` 就是回归**，别当成"差不多"。

> 未覆盖的场景：长流程（>60s）、高并发触发、需登录态的站点、强反爬站点。
> 这些不是"应该没问题"，是**没验证过**。

## License

MIT