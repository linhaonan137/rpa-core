# MCP 工具粒度设计（84 节点 → 19 个工具）

> 结论先给：**不需要注册 200 个工具。一个 MCP 工具本来就应该包住多个节点。**
> 配套文档：`NODES-USEFUL.md`（84 个节点）、`NODES.md`（全量 577 个）。

## 1. 为什么不能一个节点一个工具

| 项 | 数字 | 后果 |
|---|---|---|
| 单个 MCP 工具的 schema | 250~400 token | — |
| 200 个工具常驻上下文 | **50k~80k token** | 吃掉一半以上窗口 |
| LLM 在 200 个工具里的选择准确率 | 显著下降 | 选错工具 = 流程跑错 |
| 你机器上历史缓存 | 304 工具 / 181 KB ≈ 53k token | 实测感受过 |

**工具越多，Agent 越笨。** 这不是猜测，是上下文预算与选择准确率的双重约束。

## 2. 三层结构（工具在第二层，节点沉到第三层）

```
L1  元工具（2 个）       list_flows / run_flow          ← 流程级的开关
L2  任务工具（14 个）      web_fill / desktop_table / ...  ← 一个工具 = 一个意图
L3  节点（100+ 个）       不暴露，藏在 L2 内部            ← 实现细节
```

Agent 看到的是 L1+L2（共 19 个），**永远看不到 577 个节点**。

## 3. 节点 → 工具映射（84 个节点压成 19 个工具）

| MCP 工具 | 覆盖的节点 | 内部行为 |
|---|---|---|
| `web_open` | open_page / use_opened_page / switch_tab / close_page | 开页、切标签、关页 |
| `web_click` | click_element / hover_element / scroll_page / drag_element | 点击前自动 wait_for + postAssert |
| `web_fill` | input_text / fill_by_label / fill_form / select_dropdown / set_checkbox / upload_file | 填完自动回读校验 |
| `web_read` | get_element_info / get_child_elements / get_sibling_elements / inject_javascript / js_script | 取文本/属性/列表 |
| `web_wait` | wait_element / wait_page_load / page_load_complete / element_exists / element_visible / wait | 统一成「等到某个条件成立」 |
| `web_capture` | screenshot / download_file / save_image | 截图与下载 |
| `web_network` | network_monitor_start / wait / stop | 一个调用内部启停+等待，只返回响应 JSON |
| `desktop_table` | desktop_extract_table | **重写版**，见第 5 节。桌面其余能力用 desktop-touch，**不设自己的工具** |
| `file_ops` | read/write_text_file、copy/move/delete/rename_file、list_files、file_exists、get_file_info、create_folder | CRUD 合成一个带 action 参数的工具 |
| `data_ops` | excel_read_dicts / read_range / write_dicts / append_row / read_cell、json_parse、base64、md5、timestamp | 表格与编码 |
| `script_run` | python_script / run_command | **逃生舱**：覆盖剩下 500 多个节点 |
| `ai_call` | ai_chat / ai_vision / ai_extract / ai_normalize | 一个工具 + `task` 参数 |
| `notify` | 17 个通知节点 + send_email → webhook POST / SMTP | 只留一个工具，`channel` 参数分流 |
| **`http_request`** | api_request / webhook_request | Agent 调外部 API 的唯一入口 |
| `flow_read` | — | 无参=列全部流程；带 `flow`=读单个（list 与 get 合并） |
| **`flow_save`** | — | 新建/覆盖整个流程（有 schema 校验） |
| **`flow_patch`** | — | **改单个节点的参数**（默认 dry_run，带 reason + 快照） |
| `flow_run` | condition / foreach / loop / subflow | 跑流程；流程控制不单独出工具 |
| **`run_status`** | — | **查现在有哪些浏览器实例、谁在占用、最近执行记录** |

**14 个业务工具 + 5 个流程工具 = 19 个。** 84 个节点全被覆盖。

**为什么必须有 `flow_patch`**：没有它，调参只能手动改 JSON 文件——那是你明确说过"很痛苦"的事。它是第 2 轮（调优）的唯一入口。

## 4. 设计原则（三条）

1. **按意图聚合，不按实现镜像**
   `web_fill` 一个工具能填输入框、下拉框、复选框、上传文件——因为它们对 Agent 是同一个意图："把这个表单填上"。
2. **参数用 `action` 分流，不用工具分裂**
   `file_ops(action="copy", src=..., dst=...)` 而不是 `file_copy` / `file_move` / `file_delete` 三个工具。
3. **必须有逃生舱**
   `script_run` 一条工具，等于剩下 500 多个节点 + 无限扩展。**覆盖 80% 高频后，剩下的不要为它做工具。**

## 5. 已定决策：桌面只保留 1 个能力

| 决定 | 内容 |
|---|---|
| **保留** | 桌面表格批量抓取（原 `desktop_extract_table`） |
| **不保留** | 其余 39 个 desktop_* 节点 |
| **理由** | 其余被 desktop-touch-mcp 全覆盖且更安全（租约 + 窗口校验 + 视觉兜底）；只有「表格批量抓取」它没有 |
| **技术要点** | `GetRuntimeId()` 去重（虚拟列表滚动会重复）；`GetValuePattern().Value` 比 `Name` 准；`scroll=True` 才能抓可视区外 |

骨架见 `NODES-USEFUL.md` 末尾或后续 `desk_table.py`。

## 6. 最终形态

```
Agent
 ├─ web_*        (7 个)   → Playwright
 ├─ desktop_do   (1 个)   → desktop-touch-mcp
 ├─ desktop_table(1 个)   → 自写 uiautomation 封装
 ├─ file_ops     (1 个)   → pathlib / shutil
 ├─ data_ops     (1 个)   → sqlite3 / openpyxl
 ├─ script_run   (1 个)   → 逃生舱
 ├─ ai_call      (1 个)   → LLM
 ├─ notify       (1 个)   → requests.post
 └─ flow_*       (5 个)   → 调度 + 改参数
                    ≈ 19 个工具，约 5.8k token
```

对比 200 个工具的 50k+ token：**省 90% 上下文，选择准确率反而更高。**

## 7. 单 server 还是拆成两个（web / desktop）

**结论：自己的 19 个工具不拆，desktop-touch 保持独立。**

### 为什么"拆开注册"省不到上下文

MCP 客户端的工具列表是**会话启动时快照**（实测：注册 n8n 后必须重启会话才看得到工具）。

| 方案 | 常驻工具数 | 常驻 token | 切换成本 |
|---|---|---|---|
| 一个 server 全开 | 18 | ~5k | 无 |
| 两个 server **都启用** | **仍然 18** | **仍然 ~5k** | 无（白拆） |
| 两个 server 只启用一个 | 9 | ~2.5k | **改配置 + 重启会话** |
| 一个 server + 环境变量选组 | 9 | ~2.5k | 重启 server（快得多） |

**拆 server 只有"只启用其中一个"时才省 token，而代价是每次切换都要重启会话。** 省 2.5k 换一次重启，不划算。

### 真正该拆的理由不是上下文

| 理由 | 是否成立 |
|---|---|
| 省 token | ❌ 都启用就不省 |
| **依赖不同**（Playwright vs uiautomation） | ✅ 成立：只用网页的会话不必装/加载 UIA |
| **启动成本** | ✅ 成立：Playwright 启动慢，UIA 快 |
| **运行时隔离** | ✅ 成立：两个进程同时抢桌面焦点会互相干扰 |
| 工具数量影响选择准确率 | ⚠️ 19 个还不至于 |

### 落地方式

```
server A  rpa-web        web_* + file_ops + data_ops + script_run + flow_*   ≈ 17 个
server B  rpa-desktop    desktop_table（自写）+ 交给 desktop-touch           ≈ 1 个
server C  desktop-touch  第三方，32 个工具，本来独立                         按需启用
```

- **平常只启用 A**（网页/文件/数据是高频）
- 需要操作桌面软件时再启用 B/C —— 这时的切换成本是真实需求触发的，不是随机的
- A 里保留 `script_run`，临时要动桌面可以写代码兜底，不必切 server

### 比拆 server 更有效的做法

| 手法 | 收益 |
|---|---|
| **砍到 8 个核心** + `script_run` 兜底 | 18 → 8，省 2.8k，**且不用重启** |
| 工具描述写短（只写"什么时候用"，不写实现） | 每个工具省 50~100 token |
| 不为低频节点做工具 | 天然少即是多 |

**判断阈值：工具总数 < 25 不必拆；> 40 再按依赖拆。**

## 8. 等待类能力怎么暴露

`nodes` 里有 6 个等待相关节点：`wait_element` / `wait_page_load` / `page_load_complete` / `element_exists` / `element_visible` / `wait`。
但**不能 6 个都做成工具**，也不能让 Agent 自己决定等多久。

### 三层归属

| 等待类型 | 谁负责 | 暴露给 MCP 吗 |
|---|---|---|
| 点击/输入前「元素可交互」 | **中间件内建**（M07 三态等待） | ❌ 工具内部做，Agent 不用管 |
| 页面加载（load / domcontentloaded） | 节点参数 `waitUntil` | ❌ 写在流程 JSON 里 |
| 等文本出现 / 元素消失 / URL 变化 | `web_wait` 工具 | ✅ |
| 等某个接口返回 | `web_network` 工具 | ✅ |
| **固定 sleep 3 秒** | 不建议 | ⚠️ **默认不暴露** |

### 为什么固定 sleep 不给 Agent

模型最容易犯的错就是"睡一下应该就好了"。固定 sleep 有两个问题：
- 快了失败，慢了浪费（每次多 3 秒，一天 200 次就是 10 分钟）
- 掩盖真实问题（该等条件，却在等时间）

**如果流程里确实需要固定间隔**（比如给服务端节流），写在 JSON 的 `wait` 节点里，由人设定，不给 Agent 临时决定。

### `web_wait` 的工具签名（一个工具覆盖 4 种场景）

```python
web_wait(
    condition: "text" | "selector" | "url" | "gone",   # 等什么
    target: str,                                        # 等的内容
    timeout: int = 30,                                  # 默认够用，很少需要改
    state: "visible" | "attached" | "hidden" = "visible"
) -> {"ok": bool, "elapsed_ms": int, "matched": str}
```

**参数只有 4 个，且 3 个有默认值。** 参数越多，模型填错概率越高——这是 MCP 工具的通用原则。

### 关于「参数谁来改」

这里要分清两个层次，它们不是一回事：

| 层次 | 存在哪 | 谁改 | 例子 |
|---|---|---|---|
| **流程定义** | 流程 JSON 的 `node.data` | **人 / Agent 都能改**，可版本化 | `"timeout": 30000` |
| **工具调用** | MCP 请求参数 | 运行时由 Agent 填，不持久 | `web_click(selector, timeout=30)` |

所以「不能手动改参数」不成立——**JSON 就是给人改的**。只是：

- 常用参数（超时、重试次数）**应该有合理默认值**，90% 情况不用改
- 需要调的时候改 JSON，会进版本快照和变更流水
- MCP 工具只暴露「这次调用真的要变的东西」，其余走默认

## 9. 参数分三类 + 两轮工作流

### 三层结构（确认）

```
MCP Server（1 个）
 └─ Tools（19 个）
     └─ Params（每个工具 3~6 个，其中 2~5 个有默认值）
```

### 参数分三类，只有第一类该由 Agent 填

| 类型 | 例子 | 有默认吗 | 谁填 |
|---|---|---|---|
| **语义必需** | `selector` / `url` / `text` / `element` | ❌ 必须给 | **Agent 第一轮** |
| **调优类** | `timeout` / `retries` / `waitState` / `delay` | ✅ 按动作分档 | **引擎**（不填就用默认） |
| **断言类** | `postAssert` / `assertText` | ✅ 默认无 | 人 / Agent 第二轮（可选） |

**准确说法不是"Agent 不改参数"，而是"Agent 只填语义必需的，不碰调优的"。**

默认值按动作类型分档（写在引擎里，不给 Agent 看）：

| 动作 | 默认 timeout | 默认重试 |
|---|---|---|
| 打开页面 | 60s | 1 |
| 点击 / 输入 | 30s | 2（退避 1s、2s） |
| 读取元素 | 15s | 1 |
| 等接口返回 | 30s | 1 |
| 文件 / 数据处理 | 10s | 0 |

### 两轮工作流（可行，但要改一处）

| 轮次 | 目标 | 输入 | 产出 |
|---|---|---|---|
| **第 1 轮：生成** | 把任务变成"能跑的流程" | 用户意图 + 页面结构 | JSON（只填语义必需参数，其余走默认） |
| **第 2 轮：调优** | 让它"稳定、不超时、不误判" | 执行记录 + 失败上下文 | 参数调整 + 断言补充 |

**但要改一处：第 2 轮不该让 Agent 猜参数，应该让引擎给建议。**

### 参数建议由数据算，不由模型拍

```
每次执行记录 node.elapsed_ms  →  D06
        ↓
统计该节点 P95
        ↓
if P95 > timeout × 0.8      → 建议提高（如 30s → 60s）
if 连续 3 次 < timeout × 0.1 → 建议降低（如 60s → 30s）
        ↓
写进 changes.jsonl 的 suggestion 字段（不自动改）
        ↓
第 2 轮只做一件事：看一眼建议，点确认
```

**为什么必须这样**：模型没有任何依据知道"这个按钮要等 45 秒"，它只能猜 30 或 60。而执行记录里的 P95 是实测值。

### 工具返回值里带建议（这样第 2 轮才有信息）

```json
{
  "ok": true,
  "elapsed_ms": 1240,
  "suggestion": {
    "timeout": {"current": 30, "recommended": 15, "reason": "近 20 次 P95=6.2s"}
  }
}
```

**没有 suggestion 字段，第 2 轮就是纯猜。** 这个字段比多暴露 10 个工具更有价值。

## 10. 调参怎么落地（不手改 JSON）

### 三层：规则表 → 引擎算 → Agent 执行

```
① tuning_rules.yaml      你的工程经验，写成可执行规则（一次写，永久生效）
        ↓
② 引擎按规则 + 执行记录求值   → 产出 suggestion（不需要 LLM）
        ↓
③ Agent 调 flow_patch        → 只做「确认 + 应用」，不猜数值
```

**关键**：经验落在 ① 的文件里，不是每次对话重讲。

### `flow_patch` 的签名

```python
flow_patch(
    flow: str,          # 流程名 / ID
    node: str,          # 节点 id
    data: dict,         # 只允许 data 里的键（参数）
    reason: str,        # 必填，写进 changes.jsonl
    dry_run: bool = True  # 默认只预览 diff，不落盘
) -> {"ok": bool, "diff": {...}, "snapshot": "snap-0042", "blocked": [...]}
```

三条硬限制：

| 限制 | 原因 |
|---|---|
| 只能改 `data` 里的参数，**不能改 `type` / `id` / `edges`** | 结构改动要重新生成整个流程，那是 `flow_save` 的事 |
| `dry_run` 默认 `True` | 改流程是危险操作，先看 diff |
| `reason` 必填 | 审计靠它；`changes.jsonl` 每条都要能回答"为什么改" |

### `tuning_rules.yaml`（你的经验，写成规则）

```yaml
rules:
  - id: R01
    name: 慢加载页面提高超时
    when: {node_types: [open_page], p95_ms: ">= 20000", runs: ">= 5"}
    then: {timeout_ms: "ceil(p95_ms*2/1000)*1000"}

  - id: R02
    name: 长期秒开就收紧超时
    when: {p95_ms: "< 3000", runs: ">= 20", success_rate: ">= 0.98"}
    then: {timeout_ms: "max(ceil(p95_ms*3/1000)*1000, 5000)"}

  - id: R03
    name: 上传/下载类加重试
    when: {node_types: [upload_file, download_file]}
    then: {retries: 3, retry_backoff_ms: [2000, 5000]}

  - id: R04
    name: 循环内节点降重试（避免整体卡太久）
    when: {in_loop: true}
    then: {retries: 1}

  - id: R05
    name: 风控敏感站点提高拟人度
    when: {domain_in: [".gov.cn", "taobao.com", "pinduoduo.com"]}
    then: {humanize: true, delay_ms: [300, 900]}
```

### 你说的"我告诉你怎么调，你直接改"怎么跑

| 你说的 | 我做的 |
|---|---|
| "这个页面加载慢，超时给 60 秒" | 写成 R01 那样的规则，或直接 `flow_patch(timeout_ms=60000, reason="...")` |
| "上传老失败，多重试几次" | 写 R03；或 `flow_patch(retries=3)` |
| "循环里的节点别重试太多次" | 写 R04 |

**规则进 YAML，个别情况进 `flow_patch`。** 两者都留审计，都不需要你打开 JSON 文件。

## 11. 覆盖核查：19 个工具覆盖 84 个节点

**60 个节点由工具直连，10 个靠参数覆盖，7 个不暴露，7 个在 `flow_run` 内部。**

### 84 个节点的去向

| 组 | 节点 | 状态 |
|---|---|---|
| 入口与触发 | 5 | **不是工具**：webhook→HTTP 端点，定时→cron |
| 网页操作 | 35 | 工具 27 + **参数 8** |
| 接口抓包 | 3 | `web_network` |
| 流程控制 | 7 | `flow_run` 内部，不单独暴露 |
| 变量与脚本 | 3 | `script_run` 2 + `set_variable` 不需要（流程内联） |
| 通用工具 | 20 | `http_request` / `file_ops` / `data_ops` / `notify` 共 19 + `print_log` 不暴露 |
| AI 能力 | 6 | 工具 4 + **参数 2** |
| 表格读写 | 5 | `data_ops` |

### 10 个靠参数覆盖（不加工具）

| 节点 | 塞进哪个参数 |
|---|---|
| `switch_iframe` / `switch_to_main` | `web_open(action="iframe" / "main")` |
| `go_back` / `go_forward` / `refresh_page` | `web_open(action="back" / "forward" / "refresh")` |
| `handle_dialog` | 任一动作工具的 `on_dialog="accept" \| "dismiss"` |
| `assert_checkpoint` | 任一动作工具的 `postAssert={...}` |
| `snapshot_verify` | `web_fill(verify="snapshot")` |
| `ai_vision_act` | `ai_call(task="click_by_vision")` |
| `ai_element_selector` | `ai_call(task="find_selector")` |

**这些不做成独立工具是有意的**：它们不是独立意图，是「某个动作的附加行为」。做成独立工具会让 Agent 多一次往返。

**HTTP 请求**（`api_request` / `webhook_request`）由 `http_request` 一个工具承担——它是 Agent 调外部 API 的唯一入口。

### 引擎功能暴露对照

| 引擎能力 | 暴露给 MCP | 说明 |
|---|---|---|
| 调度（并行 / 循环 / 子流程） | ❌ 隐式 | `flow_run` 内部，Agent 不需要知道 |
| 断点 / 单步 | ❌ | 人调试用 |
| `postAssert` | ✅ 参数 | 每个动作工具都支持 |
| `errorPolicy` | ⚠️ 流程里配 | Agent 不传，默认 `retry-self` |
| 自愈 L0 / L1 | ❌ 自动 | 引擎自己干，不打扰 Agent |
| 自愈 L1.5 诊断结论 | ✅ `flow_run` 返回值 | Agent 据此决定下一步 |
| 自愈 L2 改结构 | ✅ `flow_patch` / `flow_save` | |
| 元素库 | ✅ 隐式 `@el:` | |
| 执行记录 / 参数建议 | ✅ 返回值 `suggestion` | |
| 触发器 | ❌ 不是工具 | webhook→HTTP，定时→cron |
| 打包 EXE | ❌ | 离线任务，不走 MCP |

### 兜底逻辑（这是"够用"的真正保障）

```
19 个工具覆盖 80% 场景
        ↓ 不够用
script_run 写 Python（覆盖剩下 500+ 节点 + 无限扩展）
        ↓ 还不够
http_request 直接打自己的服务
```

**判断标准**：如果某个需求需要新增工具，先问"能不能用 `script_run` 三行解决"。能就别加工具——**加工具的成本是永久的上下文开销**。

## 12. 浏览器实例怎么管（"这个流程跑在哪个浏览器里"）

### 三个漂移，是难管的根源

| 漂移 | 表现 | 后果 |
|---|---|---|
| **端口漂移** | 指纹浏览器每次启动的 CDP 端口不同 | 写死 `9222` 就连不上 |
| **身份漂移** | 实例里还留着上个流程的 profile | 数据跑到错的账号里 |
| **僵尸实例** | 后端挂了但浏览器进程还在 | 下次启动抢端口失败 |

### 浏览器实例必须有"身份证"

```python
{
  "id": "b-3f2a",                    # 实例唯一 id（进程内生成）
  "cdp_url": "http://127.0.0.1:9222",# 从 launch 响应里取，不写死
  "pid": 12345,                      # 判活
  "profile": "shop-a",               # 指纹环境名
  "mode": "ant" | "self_launch",     # 接管 or 裸启
  "launched_at": "...",
  "pages": 3,                        # 当前页面数
  "occupied_by": "r-8821" | null,    # 谁在占用（执行 id）
  "lease_until": "..." | null        # 租约到期
}
```

### 生命周期：acquire → 用 → release

```python
acquire(run_id) -> Instance
  ├─ 有空闲实例：校验 profile 匹配 → 占用 → 返回
  ├─ 无空闲：启动（或接管）→ 从响应取 cdp_url → 记录 → 返回
  └─ 已有实例被占：排队 or 拒绝（取决于配置）

release(instance, clean=True)
  ├─ 关掉本次新开的页面（保留主页）
  ├─ 清 occupied_by / lease
  └─ 异常退出：标记 dirty，下次 acquire 前先 reset（重连 CDP + 关所有页）
```

### 第一版只做单实例串行（强烈建议）

| 模式 | 优点 | 代价 |
|---|---|---|
| **单实例串行（推荐）** | "哪个流程在哪个浏览器"不需要回答——**答案唯一** | 不能并发 |
| 多实例并发 | 吞吐高 | 抢焦点、抢端口、profile 混淆，三倍调试量 |

**你的场景（每日任务、几条流程）完全不需要并发。** 单实例下这个问题自动消失。

但**必须做的事**一条不能少：

| 必须 | 为什么 |
|---|---|
| 每次执行**记录** `run_id → browser_id → cdp_url → profile` | 出问题唯一能追溯的线 |
| 执行结束**清理页面** | 否则下次跑在残留标签页上 |
| 异常退出标记 **dirty** | 防止下次在脏实例上跑 |
| 失败快照里带 **cdp_url** | Agent 能连上去看现场（第 8.4 节的前提） |

### `run_status` 返回什么

```json
{
  "browsers": [
    {"id": "b-3f2a", "cdp_url": "http://127.0.0.1:9222",
     "profile": "shop-a", "pid": 12345, "pages": 3,
     "occupied_by": "r-8821", "dirty": false}
  ],
  "runs": [
    {"run_id": "r-8821", "flow": "invoice_check", "status": "running",
     "browser_id": "b-3f2a", "started_at": "2026-11-03T03:56:12"}
  ]
}
```

**一工具两用**：既能回答"现在有几个浏览器、谁在用"，也能回答"最近跑了什么"。

### 这个工具为什么值得多加一个

前面说"不为低频需求加工具"，但这条是例外：

| 场景 | 没有它 |
|---|---|
| 流程跑出了错账号的数据 | 查不出跑在哪个 profile 上 |
| 浏览器卡死、流程全挂 | 不知道是哪个实例、能不能连上去 |
| Agent 要接手修流程 | 找不到 cdp_url，看不到现场 |

**它是"出问题时的唯一抓手"，不是一个功能。**

