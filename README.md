# rpa-core

> **平台：Windows**（SendInput / Win32 窗口操作）

用 JSON 描述流程的 RPA 内核。**能被 AI Agent 直接调用**，也能脱离 Agent 独立跑（0 token）。

```
CLI / EXE    rpa.exe run flows\xxx.json      <- 人 / 定时任务
HTTP         POST /run/{name}                <- n8n / 其它系统
MCP          工具层（见 docs/ENGINE.md）    <- AI Agent
                    |
              引擎（DAG 调度 + 节点 + 元素库）
                    |
              Playwright / 系统能力
```

**解决什么问题**：网页操作流程写成 JSON，Agent 生成、人也能改、失败可复盘；不用写死的脚本，不用手动点。

## 能力

| 域 | 覆盖 |
|---|---|
| `web_*` | 开页 / 接管 / 点击 / 输入 / 等待 / 截图 / 下载 / 上传 / iframe / 弹窗 / JS / 网络监听 / 元素扫描 |
| `fs_*` | 读写 / 追加 / 复制 / 移动 / 删除 / hash / 对比 / 目录 |
| `seq_*` | 排序 / 去重 / 过滤 / 展平 / 映射 / 分块 / 求和 / 交集 |
| `flow_*` | 流程管理：读写 / 存盘 / 改参 / 回滚 / 删除 / 执行 |
| 控制流（无前缀）| `if` / `while` / `loop` / `foreach` / `map_each` / `break` / `continue` / `subflow` / `sleep` / `set_var` / `fail` / `stop` |
| `data_*` | Excel 读写追加 / CSV / 正则 / 模板 / JSON |
| `map_*` | 嵌套取值 / 展平 / 合并 / 过滤 / 排序 |
| `sys_*` `net_*` | 系统命令 / Python 脚本 / HTTP 请求 |

准确数量用 `list_node_types` 查，本文不写会变的数字。

## 快速开始

```bash
pip install -r requirements.txt
playwright install chrome chromium

python main.py run flows/demo.json      # 跑流程
python main.py serve --port 8100        # HTTP 服务（含异步队列）
python main.py mcp                      # MCP 服务
pytest tests -q                         # 全量测试
```

### 浏览器怎么来

**手动开的浏览器程序连不进去**——它不带调试端口。默认行为是"接管一只已经开着调试端口的浏览器"，优先级：

1. `RPA_CDP=http://127.0.0.1:9333`（显式指定）
2. `out/_last_cdp.txt`（上次接管过的，自动记录）
3. **自动扫 9222~9400 找开调试端口的浏览器**（跳过 Electron 应用）

都没有就报错，并告诉你怎么办。三种解法：

| 场景 | 做法 |
|---|---|
| 要登录态（千牛/淘宝） | 用调试端口启动 Chrome，登录一次后续都带着：<br>`chrome.exe --remote-debugging-port=9333 --user-data-dir="D:\chrome-rpa"` |
| 指纹浏览器 | 设 `RPA_CDP=` 它给出的地址 |
| 公开站点，不要登录态 | 设 `RPA_ALLOW_LAUNCH=1`，程序自己开一只 |

`RPA_ALLOW_LAUNCH=1` 时再加 `RPA_LAUNCH_PORT=9333`，自建的那只会开放调试端口——下次启动直接接管同一只，登录态不用重登。

> 纯数据流程（没有 `web_*` 节点）不会碰浏览器，不需要任何配置。
>
> **一只浏览器只给一个客户端。** 同时被两个自动化工具接管时，弹窗会被另一方自动关掉，
> `web_dialog` 会报 `No dialog is showing`。调试请另开端口 + 独立 profile。详见 `docs/PITFALLS.md` 的 B11。

## 流程格式

```json
{
  "name": "抓取并落表",
  "nodes": [
    {"id": "n1", "type": "web_open", "args": {"url": "https://example.com"}},
    {"id": "n2", "type": "web_extract", "args": {
      "item": "div.item",
      "fields": [{"name": "标题", "selector": "h3"}],
      "save_as": "rows"
    }},
    {"id": "n3", "type": "data_sheet_write", "args": {"path": "out/r.xlsx", "rows": "{rows}"}}
  ],
  "edges": [
    {"id": "e1", "from": "n1", "to": "n2"},
    {"id": "e2", "from": "n2", "to": "n3"}
  ]
}
```

四条必知规矩：

1. **节点输出必须 `save_as` 存成变量**，再用 `{变量名}` 引用。`{n2}` 不是合法引用。
2. **`type` 是节点名，不是 MCP 工具名**。写流程前先 `list_node_types` 确认。不同名的几个：`web_read`→`web_elem_info`/`web_matches`/`web_js`；`web_switch`→**`web_tab`**；`web_fill`→`web_type`；`web_network`→`web_net`。
3. **`extra="forbid"`**——键名写错直接报错，不会静默忽略。
4. **弹层能打开 ≠ 下拉已选上**。验证要"点开 → 点选项 → 读回填结果"。

节点级错误处理：

```json
{"id": "n2", "type": "web_click", "args": {"target": "@el:submit"},
 "on_fail": {"mode": "retry", "attempts": 2, "backoff_ms": 800, "rollback_to": "n1"},
 "expect": {"target": "@el:success", "state": "visible", "limit_ms": 5000}}
```

## 元素库：六维指纹 + 逐层降级

不存单一 CSS 选择器（改版即废）。定位按稳定性降级：

| 层 | 依据 |
|---|---|
| 1 | `role` + `name`（可访问性语义）|
| 1.2 | 表单 label 关联 |
| 1.5 | 文本内容 |
| 2 | `id` / `data-testid` / `name` / `type` |
| 3-6 | css → path → 邻居文本 → 坐标 |

命中多个时加权打分，分差过小**报歧义并列出候选**，绝不静默点第一个。

> ⚠️ 仓库不带元素库（`elements.json` 在 `.gitignore`）。首次使用需 `element_pick` 拾取，否则 `@el:` 流程必失败。

```python
element_pick(role="link", text="提交订单", key="submit")
web_click(target="@el:submit")
```

## 异步执行

浏览器是独占资源，队列并发度 1，任务串行跑。

```python
r = flow_run_async(flow="淘宝发品", vars={"title": "商品A"})  # -> task_id
flow_task(task_id="t-xxx")    # queued/running/done/failed/cancelled
flow_cancel(task_id="t-xxx")  # 排队中、执行中都能取消
```

HTTP 等价：`POST /run-async/{name}` → `GET /task/{id}`。

## 失败现场

流程失败自动存：URL / 标题 / 可交互元素 / 表单字段 / 截图 / 每节点错误，保留最近 30 份。

```python
read_failure()              # 最近一次失败概要
read_failure(level="dom")   # 带当时的 DOM 清单
```

## 架构

```
engine/      schema（模型+校验）/ graph（图解析）/ scheduler（波次调度）
             runner（执行 + 单步 / 起始节点）/ context（变量）/ middleware（页面中间件）
             real_mouse（OS 级鼠标）/ stealth（反检测）
nodes/       节点实现，按域分文件，动态加载
elements/    元素库（六维指纹 + 降级定位）
api/         mcp.py（工具层）/ http.py（路由）/ taskqueue（队列）
store/       SQLite（执行记录 + 耗时趋势）
flows/       示例流程
```

## 已定决策（别推翻）

**引擎五条硬约束**，少一条退化成玩具：

1. 图解析：nodes/edges → 前驱后继 + 起始节点 + 孤立节点告警
2. 调度：join 等待集用 set（不用计数）+ 死路可达性重算（治分支汇合死锁）
3. 注册表：`type → 函数`，重复注册启动即报错
4. postAssert：动作成功 ≠ 事情成了（`expect` 断言）
5. errorPolicy：`on_fail.mode` = `retry` / `back` / `skip` / `stop`

**做与不做**：

| 决策 | 理由 |
|---|---|
| 落盘 SQLite 优先，Excel 只在交付要格式时导出 | Excel 无真追加、并发写坏文件、104 万行上限 |
| 元素库 = 全局库 + 流程内 `elements` manifest | 全局保证"改一处全生效"，manifest 保证流程可独立交付 |
| 定位 ARIA 优先 | 改版通常只改样式不改语义 |
| **不做可视化画布** | JSON + Agent 生成足够 |
| 浏览器单实例串行 | 「哪个流程在哪个浏览器」答案唯一 |

## 规模与验证

```
105 节点 · 43 MCP 工具 · 448 测试全绿（+6 条实时冒烟默认跳过）
核心 13,557 行 / 测试 7,013 行
```

> 本节是全文唯一维护数字的地方。核对口径：`list_node_types`、`api/mcp.py` 的 `@mcp.tool` 计数、`pytest --collect-only`。

| 层 | 覆盖什么 | 怎么重跑 |
|---|---|---|
| 单元测试 | 引擎 / 节点 / 元素库 / 存储 | `pytest tests -q` |
| **实时冒烟** | 43 个工具的 happy path，端到端（需浏览器+外网）| `RPA_SMOKE=1 RPA_CDP=http://127.0.0.1:9333 pytest tests/test_smoke_live.py` |
| MCP 矩阵 | 全部工具，正常/边界/应报错三类 | `python tests/mcp_matrix.py` |
| 真实站点 | 豆瓣首页、Top250 | `python main.py run flows/douban_top250.json` |

> 实时冒烟为什么单独一层：MCP 工具层是薄封装（参数映射、路径校验、结果转换），
> 单元测试全在 mock 层面覆盖不到。`cdp` 读错属性、截图不补扩展名、标签下标不一致
> 这类问题，单元测试全绿时照样存在——只有这一层能拦住。

> **未覆盖**：长流程（>60s）、高并发、需登录态站点、强反爬站点。这些不是"应该没问题"，是**没验证过**。

## 文档

| 文档 | 什么时候看 |
|---|---|
| [`docs/GUIDE.md`](docs/GUIDE.md) | **写流程**：等待策略、十条硬规矩、淘宝发品实测要点 |
| [`docs/ENGINE.md`](docs/ENGINE.md) | **改引擎**：元素库定位机制、工具层设计、真实鼠标 |
| [`docs/PITFALLS.md`](docs/PITFALLS.md) | **排障**：踩坑经验 + 已知未修问题清单 |

## License

MIT
