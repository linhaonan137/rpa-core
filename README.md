# rpa-core

> **平台：Windows**（依赖 SendInput / uiautomation / Win32 窗口操作）

用 JSON 描述流程的 RPA 内核。**能被 AI Agent 直接调用**，也能脱离 Agent 独立跑（0 token）。

```
CLI / EXE    rpa.exe run flows\xxx.json      <- 人 / 定时任务
HTTP         POST /run/{name}                <- n8n / 其它系统
MCP          49 个工具                       <- AI Agent
                    |
              引擎（DAG 调度 + 122 节点 + 元素库）
                    |
              Playwright / 系统能力
```

## 能力

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
pytest tests -q                         # 579 项测试
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
      "fields": [{"name": "标题", "selector": "h3"}, {"name": "价格", "selector": ".price"}]
    }},
    {"id": "n3", "type": "data_sheet_write", "args": {"path": "out/result.xlsx", "rows": "{n2}"}}
  ],
  "edges": [
    {"id": "e1", "from": "n1", "to": "n2"},
    {"id": "e2", "from": "n2", "to": "n3"}
  ],
  "vars": [{"name": "keyword", "value": "手机"}]
}
```

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
element_check()                                           # 体检
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
api/            mcp.py（49 工具）/ http.py（10 路由）/ taskqueue 队列
heal/           失败分类 + 元素自愈候选
store/          SQLite（执行记录 + 耗时趋势）
flows/          示例流程（7 个，含真实站点）
tests/          579 项
```

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

`flows/` 里有 7 个可直接跑的示例：

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
| [`docs/MCP-DESIGN.md`](docs/MCP-DESIGN.md) | 工具粒度设计 |
| [`docs/MCP-TOOL-AUDIT.md`](docs/MCP-TOOL-AUDIT.md) | 工具审计（370 组实测 + 修掉的 53 个问题）|
| [`docs/ELEMENT-CAPTURE.md`](docs/ELEMENT-CAPTURE.md) | 元素六维字段定义与抓取实现 |
| [`docs/ELEMENT-LIB-FIX.md`](docs/ELEMENT-LIB-FIX.md) | 六维定位改造 |
| [`docs/REAL-MOUSE.md`](docs/REAL-MOUSE.md) | 真实鼠标实现与三个坑 |
| [`docs/REAL-SITE-TEST.md`](docs/REAL-SITE-TEST.md) | 真实站点验证记录 |
| [`docs/LESSONS.md`](docs/LESSONS.md) | 踩坑清单 |

## 规模

```
122 节点 · 49 MCP 工具 · 579 测试
引擎 3,656 行 / 测试 6,223 行
```

## License

MIT