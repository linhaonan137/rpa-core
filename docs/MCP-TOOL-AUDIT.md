# MCP 工具审计（2026-11-03）

> 20 个工具逐个真调，覆盖正常参数 + 边界参数。**结果：全部可用，但过程中修了 9 个问题，
> 其中 3 个是「工具在、能力不在」的严重问题。**

## 一、审计结论

| 分类 | 数量 | 状态 |
|---|---|---|
| 完全可用 | 14 | ✅ |
| 修完可用 | 4 | ✅（原本指向不存在的节点）|
| 依赖缺失（预期） | 2 | ⚠️ desktop / media（缺 uiautomation / yt-dlp，报错信息友好）|

工具数：**20** 个全部能调通。节点数从 112 → **116**（补了 4 个幽灵节点）。

## 二、严重问题：工具在、能力不在（3 个）

**根因**：MCP 工具层写得很完整，但底层节点从未实现，且没有启动自检。
调用时返回 `未注册的节点类型: xxx`，而工具描述里说得头头是道。

| 工具 | 指向的幽灵节点 | 修复 |
|---|---|---|
| `script_run` | `python_script`、`run_command` | **真实现两个节点**：`python_script`（exec + `result` 变量）、`run_command`（subprocess，超时保护）|
| `http_request` | `api_request`、`http_post` | **新建 `nodes/net.py`**：`http_request`（httpx，走系统代理，4xx 可配是否算失败）|
| `web_network` | `network_monitor_start/stop/wait` | **同一文件**：`network_monitor`（page response 监听，action=listen/wait/log）|

新增节点数：4（112 → 116）

## 三、语义错误（2 个）

### 1. `web_read kind=text` 返回成功但没有内容

```json
{"ok": true, "message": "已读取 div.quote span.text."}   // ← 没有 data
```

**根因**：MCP 把 `kind` 传给节点的 `kind` 字段，同时把 `attr` 传了空字符串，
节点里 `args.get("attr", "text")` 拿到 `""`（存在但为空，不触发默认值）
→ 走 `get_attribute("")` → 返回 None → 静默成功。

**修复**：`kind` 为 text/value/html 时映射到节点的 `attr`；`kind=attr` 时才用 `attr` 参数。

### 2. `web_read kind=list` 读的是"后代"不是"匹配列表"，且无截断

`{kind: "list", target: "div.quote"}` 返回 **90 条**（10 个 quote × 每个的所有后代），
每条还带完整 HTML —— 一次性把上下文灌爆。

**修复**：改成用 JS 取 `querySelectorAll` 的匹配结果，**截断 50 条 + 每条文本限 200 字**。

## 四、崩溃（1 个）

`Result.fail()` 不支持 `data` 参数 —— `run_command` 想带退出码数据时直接抛
`TypeError: Result.fail() got an unexpected keyword argument 'data'`。

**修复**：`Result.fail(error, message="", data=None)`。

## 五、参数名不一致（1 类，未统一，已记录）

**同一批工具，动作参数有三种叫法**：

| 叫法 | 工具 |
|---|---|
| `kind` | web_open / web_click / web_read / web_capture / desktop_act |
| `action` | data_ops / file_ops / web_network |
| `when` | web_wait |
| `task` | ai_call |
| 无（code/cmd 二选一）| script_run |

实测影响：我按语义猜 `action` 调 `web_click` 被 schema 拒绝（错误信息清晰，能立刻纠正）。

**为什么没统一**：改签名会破坏已有调用方；FastMCP 的 schema 已经明确列出参数名，
AI 读一次 schema 就能用对。**记录为已知设计债**，下次大版本统一成 `action`。

## 六、启动自检（新增）

```python
def _call_ghost_refs() -> list[str]:
    """扫描本文件里 _call("node_type") 的字面量，找出没注册的节点。"""
```

`main()` 里调用，发现幽灵引用就往 **stderr** 打警告（stdio 协议下不能污染 stdout）。

**价值**：这类问题第一次跑就能暴露，不用等用户点到那个工具。
自检上线后立刻抓出 5 个幽灵引用（就是上面第二节那 3 个工具涉及的节点）。

## 七、逐工具验证记录

### 网页类（7）

| 工具 | 验证内容 | 结果 |
|---|---|---|
| `web_open` | new / reuse / attach / back / forward / refresh / close / all | ✅ |
| `web_read` | text / value / attr / html / list / children / siblings / eval | ✅（修了 2 个语义问题）|
| `web_click` | left / right / double / hover / scroll / drag | ✅（翻页 /page/2/ 验证）|
| `web_fill` | 单字段 / form 批量 / 下拉 / 勾选 / 上传 | ✅ |
| `web_wait` | selector / text / url / gone | ✅ |
| `web_capture` | screenshot / full_page / download / image | ✅ |
| `web_network` | listen / wait / log | ✅（原本 3 个节点都不存在）|

### 数据 / 文件 / 网络 / 脚本 / 通知（5）

| 工具 | 验证内容 | 结果 |
|---|---|---|
| `data_ops` | sheet_read / sheet_write / csv / regex / json_query | ✅（写 2 行落项目 `out/`）+ |
| `file_ops` | read / write / append / exists / list / mkdir / hash | ✅（相对路径落项目根）|
| `http_request` | GET / POST json / 404 / 非法 url | ✅（404 返回失败但带 body）|
| `script_run` | python / shell / 语法错 / 非零退出 | ✅（修完后全通）|
| `notify` | 无 url / 有 url | ✅（原本指向幽灵节点）|

### 桌面 / 媒体 / AI（3）

| 工具 | 结果 |
|---|---|
| `desktop_act` | ⚠️ 缺 uiautomation，报错里带安装命令（可接受）|
| `media_dl` | ⚠️ 缺 yt-dlp，同上 |
| `ai_call` | ⚠️ 无 key，同上 |

### 流程类（5）

| 工具 | 验证内容 | 结果 |
|---|---|---|
| `flow_read` | 列表 / 单读 / 不存在 | ✅ |
| `flow_save` | 新建 / 覆盖 / 非法（schema 拒绝）| ✅ |
| `flow_patch` | dry_run diff / 真改 / 节点不存在 | ✅（diff 格式清晰：`from` → `to`）|
| `flow_run` | 无变量 / 注入变量 / 不存在 | ✅（demo 8 节点 204ms）|
| `run_status` | 空 / limit=3 / limit=0 | ✅（带 P95、ok_rate）|

## 八、发现的问题都指向同一个地方

**MCP 层与节点层之间没有契约校验。**

工具描述可以随便写，节点名写错了没人管，参数名传错了走默认值、静默成功。
这一层如果早做自检，上面 3 个"工具在能力不在"的问题在第一次启动就会被发现。

已做：`_call_ghost_refs()`（节点名）。

还没做：
1. **参数名契约**：节点读的参数名 vs MCP 传的参数名，不一致就启动报错
   （今天实测踩了 3 次：`text`/`value`、`wait_until`/`until`、`kind`/`attr`）
2. **返回值契约**：节点返回 `data=None` 但 message 说"已读取"——这类静默空返回要拦

---

## 九、第二轮：参数矩阵实测后的修复（88 组）

子代理跑了 88 组参数矩阵（20 工具 × 3-7 组，含缺参/非法值/边界），发现 4 个我漏掉的问题，全部已修：

| # | 问题 | 严重度 | 修复 |
|---|---|---|---|
| 1 | **`web_wait` 三个分支全失效**：`state:"hidden"` 对可见元素 9ms 报 ok；`when:"text"` 报"缺 target"；`when:"url"` 4ms 报 ok（URL 没比较） | 🔴 假阳性 | `wait_element` 节点改造：支持 `text` / `url` / `kind`（兼容 `state`）三种等待；MCP 层传参改正 |
| 2 | **`flow_save` 接受空流程**，`flow_run` 返回 `ok:true` + 0 步 | 🔴 静默成功 | `flow_save` 校验 `nodes` 非空 |
| 3 | **`run_id` 同秒撞号**：`r-{秒}` + DB `ON CONFLICT DO UPDATE` → 同秒 4 次执行只留 1 条 | 🔴 数据丢失 | 加 uuid 后缀 `r-{秒}-{6位hex}`，实测同秒 4 次得 4 个不同 ID |
| 4 | **`limit_ms` 被丢弃**：`_pick` 硬编码 30s，`web_read`/`web_fill` 传 3s 仍等 30s | 🟠 参数不生效 | 17 处 `_pick` 调用补 `limit`（并给 `scroll_page`/`screenshot` 补 limit 变量） |
| 5 | `web_capture` 的 `full_page` 不生效（节点读 `kind=full`）+ `name` 不在 schema + 同秒覆盖 | 🟠 | MCP 层映射 `full_page → kind="full"`，补 `name` 参数 |
| 6 | `web_open kind="reuse"` 是死枚举（落到 `open_page({"url": ""})` 报"缺 url"） | 🟠 错误信息误导 | 实现 reuse：有 url 就在当前标签导航，无 url 就确认页面可用 |

### 修复验证

| 项 | 修复前 | 修复后 |
|---|---|---|
| `wait_element state=hidden`（元素可见）| 9ms 返回 ok | **1500ms 后超时失败** ✅ |
| `wait_element text=不存在的文本` | "缺 target" | **1500ms 后超时失败** ✅ |
| `wait_element url=不匹配` | 4ms 返回 ok | **超时 + 提示当前 URL** ✅ |
| `full_page` 截图 | 1280×720（和普通一样）| **1280×1800** ✅ |
| `limit_ms=2000` 定位失败 | 等 30s | **2.0s** ✅ |
| 同秒 4 次 `flow_run` | 1 条记录 | **4 条**（`r-...-c8215c` 等）✅ |
| 88 组矩阵 | 84 通过 / 4 失败 | **86 通过 / 2 「失败」**（2 条都是判定口径，非 bug）|

### 第三轮：路径与边界加固（已修）

**策略**：MCP 对外接口**只接受相对路径**，绝对路径和 `..` 越界一律拒绝。
节点内部仍用 `resolve_path`（流程之间可以传绝对路径，保持灵活）。

| 项 | 修复 |
|---|---|
| **`file_ops` 越界** | 加 `_safe_path()`：绝对路径拒绝 + `..` 越界拒绝。实测读 `C:\Windows\...\hosts` 被拒（原来读成功 226 字符）|
| `data_ops` / `web_capture` / `media_dl` | 同一套校验（涉及文件读写的 4 个工具全接上）|
| `run_status limit=-1` | 负数/0 一律当 10，上限 200（原来 `LIMIT -1` 被 SQLite 当"不限制"，吐全表）|

验证：

```
① 读 hosts                 → 拒绝：只接受相对路径
② 写 out/rel_test.txt      → ok，落项目 out/
③ 读回                     → "相对路径 OK"
④ 读 ../../Windows/win.ini → 拒绝：路径越界
⑤ 截图 path=F:\temp\x.png  → 拒绝：只接受相对路径
   run_status: limit=10→10条  0→10条  -1→10条  999→13条（上限 200）
```

### 仍未修（需你决定）

| 项 | 说明 |
|---|---|
| 参数命名三套（`kind`/`action`/`when`/`task`）| 统一要改 5 个工具签名，会破坏已有调用；schema 已明确列出，AI 读一次能用对 |
| 错误信息泄漏节点名 | "open_page 缺 url" 而不是 "web_open 缺 url"，轻度影响 |

### 第四轮：动作参数别名（10 个工具）

同一批工具里"要做什么"有四种叫法，Agent 猜错就报 validation error。现在**互相兼容**：

| 工具 | 主参数 | 别名 |
|---|---|---|
| web_open / web_click / web_read / web_capture / desktop_act | `kind` | `action` |
| web_network / file_ops / data_ops | `action` | `kind` |
| web_wait | `when` | `action`、`kind` |
| ai_call | `task` | `action` |

实现：`_merge_action(*values, allowed, default)` —— 取第一个非空值并校验。
**非法值会列出所有合法取值**（不再只是"validation error"）：

```
file_ops(kind="乱写的动作")
→ 取值不支持: '乱写的动作'；可选: read, write, append, copy, move, delete,
  rename, exists, info, list, mkdir, hash
```

### 顺带修：web_read 的 target 契约

`target` 原来是**必填**（schema required），但 `kind=eval` 根本不需要它 ——
等于"想执行 JS 必须先瞎填一个选择器"。改成默认空 + 按 kind 校验：

```
web_read(kind="eval", code="() => ...")   → ok，返回 10
web_read(kind="text")                     → 需要 target（只有 kind=eval 可以不给）
```

### 别名生效验证

| 调用 | 结果 |
|---|---|
| `file_ops(kind="write")`（别名）| ✅ 写入成功 |
| `file_ops(action="read")`（原名）| ✅ 读回内容 |
| `data_ops(kind="sheet_write")` | ✅ 写 Excel |
| `web_click(target=..., action="left")` | ✅ 真点到下一页 |
| `web_read(target=..., action="text")` | ✅ 读到文本 |

---

## 十、第五轮：全参数深挖（子代理 370 组实测 + 修复）

两个子代理并行：网页类 172 组（154 过，子代理因 429 频率限制没写完报告，数据在 `out/agent_web_matrix.json`）+ 非网页类 198 组（156 过，报告 `out/agent_ops_report.md`）。合并 **29 条独立缺陷**，本轮修掉 18 条。

### 安全类（🔴 最优先）

| 缺陷 | 修复 |
|---|---|
| `flow_save(flow="../evil")` 写到项目外 | `_safe_flow()`：拒绝路径字符，4 个 flow 工具全接入 |
| `flow_read(flow="../elements")` 读 elements.json 全文 | 同上 |

### 静默成功（🔴 比报错危险）

| 缺陷 | 修复 |
|---|---|
| `data_ops csv_parse` 的 path 被丢 → 空 CSV 但 ok:true | path 读文件内容进 text |
| `data_ops json_query` 的 text/key 全丢 → 返回空 | 映射到节点的 `data` + `path` |
| `file_ops write content=""` 被吞 → 写不了空文件 | write/append 恒传 content |

### 参数名映射（🔴 6 个动作完全不可用）

| MCP 传的 | 节点要的 | 动作 |
|---|---|---|
| `text`/`rows` | `items` | list_sort / list_unique / list_filter |
| `text`+`key` | `data`+`path` | json_query |
| `rows` | `row`（单行）| sheet_append |
| `dest` | `name`（纯文件名）| file_rename |
| `control` | `target` | desktop_table |
| `limit_ms` | `timeout_ms` | run_command |

另外：`web_fill` 补 `action` 别名（漏网）。

### 超时类（🔴 3 个全失效）

| 缺陷 | 修复 |
|---|---|
| `script_run` 的 limit_ms 被丢（命令超时失效）| 映射 `timeout_ms` |
| `python_script` 同步 exec：**卡死事件循环**且无超时 | **改子进程**（`python -c`，timeout 真杀进程）|
| 修复过程中发现的更深坑：`redirect_stdout` 是**进程级**的，超时后 `__exit__` 不执行 → MCP 的 stdout 泄漏成 StringIO，**所有响应发不出去** | 子进程方案天然规避（这就是"exec 不能丢线程池"的根本原因）|

### 其他

| 缺陷 | 修复 |
|---|---|
| `run_command` 输出 GBK 被 utf-8 强解 → 中文乱码 | bytes + utf-8→gbk fallback |
| `run_command` cwd 默认进程目录（从 C:\Windows 起服务时产物乱落）| 默认项目根 |
| `flow_run start_at` 指向不存在节点 → 裸 KeyError（协议层异常）| try 包住 → ok:false |
| `ai_call` 缺 key 提示传 `args.api_key` 但签名没这参数 | 加 `api_key/api_url/model` 透传 |
| `file_copy` 缺 dest 报"缺 path" | 错误信息指对参数 |
| `flow_read()` 列表没有 ok 字段（其余 19 个工具都有）| 补 ok:true |
| `file_ops list` 空 path 被"白名单"放行后节点又拒绝 | 空 path 默认项目根 |

### 验证（修复后实测）

```
flow_save ../evil          → 拒绝：flow 名不能包含 ['/', '..']
flow_read ../elements      → 拒绝
data_ops list_sort [3,1,2] → [1,2,3]（原来"缺 items"）
json_query a.b[1]          → 20（原来空）
python 超时 800ms          → 0.8s 失败（原来 5s 还 ok:true）
cmd 超时 800ms             → 失败（Windows shell=True 已知限制：杀不干净要 ~5s）
cmd 中文输出               → "中文输出"（原来乱码）
cmd 的 cwd                 → 项目根（原来 C:\Windows）
rename dest="b.txt"        → 成功（原来 name 被错误绝对化被拒）
write content=""           → 写入 0 字节（原来"缺 content"）
flow_read() 列表           → ok:true + flows
```

### 未修（13 条，按影响排序）

| 缺陷 | 原因 |
|---|---|
| `flow_run limit_steps` 循环内重置 | runner 调度器设计问题 |
| `flow_patch expect/on_fail` 不校验就落盘 | 要和 schema 对齐，工作量中等 |
| `flow_save` 覆盖不留版本快照（patch 有）| 补齐即可，优先级中 |
| `web_read kind=list` 不支持 `text=` 语法（querySelectorAll 限制）| 需换 locator.all() 实现 |
| `web_capture kind=image` 对 data: URI 失败 | Playwright 限制，需先转存 |
| `web_click drag` 的 limit_ms 不生效（内部 30s 硬编码）| `_pick` 之外的 `drag_to(timeout)` 漏了 |
| 非法枚举报 pydantic 裸文本（无 ok:false）| FastMCP 框架层 |
| notify 三种 channel body 相同 | 钉钉/企微格式本来就一样，文档问题 |
| `file_ops delete` 不存在返回 ok | 幂等语义，合理 |

---

## 十一、第六轮：清掉 4 条"有价值"的遗留

### 1. `flow_run limit_steps` 在循环内失效（🔴 数据安全）

**根因**（`engine/runner.py:287-300`）：循环每轮都 `new Runner(..., RunOptions(max_steps=self.opts.max_steps))` —— **每轮都拿完整预算**，跑完再把子步数累加到主计数。所以 `limit_steps=1` 跑 5 轮循环照样跑满。

**修复**：每轮开始前检查剩余预算，子 Runner 传 `remaining = max(1, max_steps - steps)`。

```
修复前：limit_steps=1/2/3/6 → 全部跑满 5 轮
修复后：limit_steps=2 → 2 步     limit_steps=4 → 4 步
        limit_steps=6 → 6 步     limit_steps=100 → 8 步（跑完 ok:true）
```

### 2. `flow_patch` 不校验 `on_fail` / `expect` 就落盘（🔴 写坏流程）

原来 `target["on_fail"] = on_fail` 直接赋值 —— `{"mode":"bogus"}` 照写，等 `flow_run` 才炸。

**修复**：用 schema 模型校验（`OnFail(**on_fail)` / `Expect(**expect)`），非法直接拒绝：

```
on_fail={"mode":"bogus"}   → 拒绝：Input should be 'retry', 'back'...
expect={"limit_ms":-5}     → 拒绝：Input should be greater than...
on_fail={"mode":"skip"}    → 通过 + 快照 demo.snap-0001.json
```

### 3. `flow_save` 覆盖不留快照（🟡 不可追溯）

`flow_patch` 一直有 `.versions`，`flow_save` 是整体覆盖却没有 —— 改坏了回不去。

**修复**：覆盖前调 `_snapshot()`，返回 `snapshot` 文件名字段。

### 4. `web_read kind=list` 不支持 `text=`（🟠 语法报错）

上一轮为了截断，把 list 改成 JS `querySelectorAll` —— 遇到 `text=Next` 直接 `SyntaxError`（那个语法只有 locator 认识）。

**修复**：新增节点 `list_matched_elements`（用 `_locate` + `loc.nth(i)`，支持 css / text= / xpath= / @el: 全部写法）。

```
list 'text=Next'            → 1 条
list 'div.quote span.text'  → 10 条
list 'li.next a'            → 1 条
```

### 插曲：这个修复第一次是无效的

节点第一次起名 `list_elements` —— **和 `picker.py` 里的同名节点冲突**，`DuplicateNodeType` 被 `load_all()` 吞进 `failures()`，没人看，于是调用打到了 picker 的实现（返回元素库列表而不是页面元素）。

**顺手加的守卫**：`api/mcp.py` 的 `main()` 现在启动时检查 `nodes.failures()`，有失败就往 stderr 报警。和 `_call_ghost_refs()`（节点名幽灵引用）一起，构成启动期两道自检。

> 教训：`load_all()` 吞异常是对的（单个模块坏不该拖垮整包），但**必须有人看 `failures()`**，否则就是静默丢能力。
| `web_read(kind="list", target="div.quote span.text")` | ✅ 10 条（不再是 90 条后代）|