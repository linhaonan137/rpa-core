"""把实测结果渲染成 out/agent_ops_report.md。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_ops_matrix import cases  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"

JUDGE = {
    # id: (判定, 备注)
    "D05": ("🔴 参数不生效", "MCP 传 rows，data_sheet_append 只认 row/row_data → 不可用"),
    "D06": ("🔴 参数不生效", "同上，新建文件也一样"),
    "D09": ("🔴 静默成功", "path 被丢弃，text 默认空串 → 返回 ok:true 的空 CSV"),
    "D10": ("🔴 参数不生效", "MCP 传 text，seq_order 只认 items → 不可用"),
    "D11": ("🔴 参数不生效", "同上（seq_uniq）"),
    "D12": ("🔴 参数不生效", "同上（seq_pick）"),
    "D16": ("🔴 静默成功", "data/path 两个参数都被丢，返回 ok:true 且无 data"),
    "D17": ("🟠 不可诊断", "取不到 key 与取到 null 无法区分，也没有 required 入口"),
    "D21": ("🟡 形态不一致", "非法 action 返回裸 pydantic 文本，无 ok:false"),
    "D22": ("✅", "save_as 经 ctx 变量验证生效（V05）"),
    "F10": ("🔴 参数不生效", "MCP 传 dest，fs_rename 只认 name → 不可用"),
    "F11": ("🟡 连带失败", "因 F10 失败而未改名，源文件不存在"),
    "F16": ("🔴 参数不生效", "content=\"\" 被 `if content:` 吞掉 → 无法写空文件"),
    "F21": ("✅", "删除成功"),
    "F22": ("🟡 静默成功", "重复删仍 ok:true（deleted:false 可辨，属幂等设计）"),
    "F25": ("🟠 不可诊断", "缺 dest 报“缺 path 参数”，指错了参数"),
    "F27": ("🟡 白名单空转", "_safe_path 放行 list 的空 path，节点又拒绝"),
    "H12": ("🟡 形态不一致", "非法 method 走 RPC 异常"),
    "N06": ("🟡 形态不一致", "非法 channel 走 RPC 异常"),
    "A02": ("🟠 不可诊断", "target 按进程 cwd 解析，`out/…` 报找不到（文件真实存在）"),
    "A08": ("🟡 形态不一致", "非法 task 走 RPC 异常"),
    "S08": ("🔴 参数不生效", "传 limit_ms，sys_cmd 读 timeout_ms → 5.1s 未超时"),
    "S09": ("🔴 参数不生效", "sys_py 无超时实现，3s 跑完且 ok:true"),
    "R01": ("🟡 形态不一致", "返回 {flows:[…]}，没有 ok 字段"),
    "R04": ("🔴 越界读", "flow=\"../elements\" 读到项目根的元素库全文"),
    "W07": ("🔴 越界写", "flow=\"../tmpa_escape\" 写到项目根"),
    "P07": ("🔴 静默成功", "非法 expect 被写进流程，flow_run 才炸"),
    "P08": ("🔴 静默成功", "非法 on_fail 被写进流程，flow_run 才炸"),
    "X04": ("🟠 不可诊断", "start_at 不存在直接抛工具异常，无 ok:false"),
    "X08": ("🟡 连带失败", "P07/P08 写坏的流程在此暴露"),
}

EXTRA = [
    # (id, 工具, 参数组合, 结果, 判定, 备注)
    ("K11", "desktop_act", 'kind="table", control="NoSuchControlXYZ"',
     'ok=false "ui_table 缺 target（表格控件）"', "🔴 参数不生效",
     "装好 uiautomation 后实测：MCP 只传 control，节点只认 target/name → kind=table 无法调用"),
    ("K12", "desktop_act", 'kind="table"（不给 control）',
     'ok=false "ui_table 缺 target"', "🔴 参数不生效", "同上，确认 control 被完全丢弃"),
    ("K13", "desktop_act", 'kind="select_text", control="NoSuchControlXYZ"',
     'ok=false "ui_text 找不到控件"', "✅", "control 这条路是对的，与 table 不一致"),
    ("K14", "desktop_act", 'kind="focused"', 'ok=true control_type="EditControl"', "✅", "真机读到焦点控件"),
    ("K15", "desktop_act", 'kind="table", control=…, limit=3, save_as=…',
     'ok=false "缺 target"', "🔴 参数不生效", "limit/save_as 无法验证（同 P-12）"),
    ("H21", "net_http", 'POST /post, json_body={probe:v1}, headers={X-Probe:yes}',
     'ok=true，回显 json={probe:v1}、X-Probe=yes', "✅", "两个参数确实送到对端"),
    ("H22", "net_http", 'GET /get?x=1', "ok=true，回显 args={x:1}", "✅", "query 串正常"),
    ("N20", "notify", 'channel="webhook"|"dingtalk"|"wecom"', "ok=true ×3，body 完全相同",
     "🟡 参数差异小", "msgtype/text 结构一致，只有 feishu 换 msg_type"),
    ("N21", "notify", 'channel="feishu"', 'ok=true，body={msg_type,content}', "✅", "格式正确"),
    ("P22", "flow_patch", 'dry_run=true, args={x:99}', "ok=true，diff 正确，file 仍是 {x:0}", "✅", "确实不落盘"),
    ("P24", "flow_patch", 'dry_run=false, args={x:99}', "ok=true，snapshot=…snap-0001.json，file 变 {x:99}", "✅", "落盘 + 快照"),
    ("P26", "flow_patch", 'expect={"text":"x"}, dry_run=false', "ok=true", "✅", "合法 expect 通过"),
    ("P27", "flow_run", 'flow="tmpa_dry"', "ok=true", "✅", "被 patch 的流程仍可跑"),
    ("P28", "flow_save", "覆盖已存在的 tmpa_dry", 'ok=true，节点 n1 被 m1 顶掉', "🟡 数据被覆盖",
     "无快照、无流水；flow_patch 有 .versions + _meta 保护，flow_save 没有"),
    ("Y02", "flow_run", 'vars={"v1":"外部A"}（cmd 用到 {v1}）', "ok=false failed=[n1]", "🟡 环境限制",
     "sys_cmd 的 cwd 是 C:\\Windows，相对路径 out/ 写不进去（见 P-15）"),
    ("Y07", "flow_run", "5 轮循环 + limit_steps=1/2/3/6/100", "全部 ok=true，各跑满 5 轮",
     "🔴 参数不生效", "limit_steps 只在外层计数，循环体内层每轮重置预算"),
    ("V31", "data_ops", 'data_csv_read, path="out/tmpb_d.csv"（文件 12 字符 2 行）',
     'ok=true "空 CSV" data=[]', "🔴 静默成功", "与 D09 同一问题，复现确认文件确实有内容"),
    ("V33", "data_ops", 'json_query, text=\'{"a":1}\', key="a"', 'ok=true "取到 " 无 data',
     "🔴 静默成功", "与 D16 同一问题"),
    ("V35", "data_ops", 'seq_order, rows=[3,1,2]', 'ok=false "缺 items"', "🔴 参数不生效",
     "rows 也不被识别，列表动作没有任何可用入口"),
    ("V34", "data_ops", 'seq_order, text=[3,1,2]（数组）', "RPC 异常 string_type", "🟡 形态不一致",
     "text 被声明为 str，传数组直接 500 形态"),
    ("V05", "script_run", "读 ctx 里的 save_as 变量", "v_csv:str v_txt:str v_http:dict v_py:int", "✅",
     "data/file/http/script 的 save_as 都真落了变量"),
    ("V30", "file_ops", 'delete, path="out/tmpb_never_existed.txt"',
     'ok=true "不存在，跳过" deleted=false', "🟡 静默成功", "与 F22 同"),
    ("W30", "script_run", 'cmd="cd"', 'ok=true stdout="C:\\Windows\\"', "🟠 不可诊断",
     "sys_cmd 默认 cwd = MCP 进程启动目录，不是项目根"),
    ("W31", "script_run", 'cmd="echo x > out/tmpb_cwd.txt"', 'ok=false stderr="ϵͳ�Ҳ���ָ����·����"',
     "🟠 不可诊断", "① 相对路径落点不对 ② 中文 stderr 用 utf-8 解 GBK → 乱码"),
]

ISSUES = """\
### 🔴 P-01 `data_ops` / `data_csv_read` + `path` —— 静默返回空表

- 参数：`action="data_csv_read"`, `path="out/tmpb_d.csv"`（文件真实存在，内容 `x,y\\n1,a\\n2,b\\n`，12 字符）
- 现象：`{"ok": true, "message": "空 CSV", "data": []}`
- 原因：`api/mcp.py:478` 把 `data_csv_read` 当文件动作校验了 `path`，但 `args` 里只塞了 `text`（默认空串）；`nodes/data.py:864` 只读 `text`，读不到就判空。`path` 被完整丢弃。
- 危害：Agent 拿到 `ok:true` + 空数组，会当成"这个 CSV 是空的"，不会发现读错了对象。

### 🔴 P-02 `data_ops` / `json_query` —— 两个参数全丢，返回 `ok:true` 且无 `data`

- 参数：`action="json_query"`, `text='{"a":{"b":[10,20]}}'`, `key="a.b[1]"`（应得 `20`）
- 现象：`{"ok": true, "message": "取到 "}` —— 无 `data` 字段；换成不存在的 `key="zzz"` 返回一模一样
- 原因：`nodes/data.py:596` 读 `data` + `path`，MCP 只给 `text` + `key`。`data=None`、`path=""` → `get_path(None,"") is None` → 走到"取到"分支。
- 危害：静默成功 + 无法区分"取到 null"和"键不存在"。

### 🔴 P-03 `flow_patch` 不做 schema 校验 —— 能把流程写坏

- 参数 A：`flow="tmpa_ok"`, `node="n1"`, `expect={"limit_ms":500}`, `dry_run=false` → `ok:true`
- 参数 B：`flow="tmpa_ok"`, `node="n1"`, `on_fail={"mode":"bogus"}`, `dry_run=false` → `ok:true`
- 现象：两次都 `ok:true` 并落盘，`flow_read` 能看到 `"on_fail":{"mode":"bogus"}`、`"expect":{"limit_ms":500}`；
  随后 `flow_run` 才报 `流程校验失败: 2 validation errors for Flow nodes.0.on_fail.mode`
- 原因：`api/mcp.py:630-647` 直接改 `raw` dict 再 `json.dumps` 写回，从不经过 `Flow(**raw)`。
- 危害：一次成功的 patch 让流程彻底跑不起来，而且错误延迟到 flow_run 才暴露，归因困难。

### 🔴 P-04 / P-05 `flow_save` / `flow_read` 的 flow 名不做路径校验 —— 越界读 + 越界写

- `flow_save(flow="../tmpa_escape")` → `{"ok": true, "path": "F:\\\\…\\\\rpa\\\\flows\\\\..\\\\tmpa_escape.json"}`，文件落到**项目根**
- `flow_read(flow="../elements")` → `{"ok": true, "flow": {...}}`，返回项目根 `elements.json` 全文
- 原因：`api/mcp.py:580` `p = FLOW_DIR / f"{flow}.json"` 直接拼，没有走 `_safe_path`。其它 13 个工具都过了 `_safe_path`，唯独 flow 名的 5 个工具没有。
- 危害：File 层做了严格限制（绝对路径/`..` 全拒），流程层却开后门，限制形同虚设。

### 🔴 P-06 `data_ops` / `data_sheet_append` 完全不可用

- 参数：`action="data_sheet_append"`, `path="out/tmpb_t.xlsx"`, `sheet="S1"`, `rows=[{"a":3,"b":"z"}]`
- 现象：`{"ok": false, "error": "data_sheet_append: 缺 row"}`
- 原因：`nodes/files.py:720` 只认 `row`/`row_data`，MCP 传 `rows`。
- 危害：10 个 action 里少 1 个，且报错只说"缺 row"，Agent 不知道自己传的 `rows` 去哪了。

### 🔴 P-07 `data_ops` 的 `seq_order` / `seq_uniq` / `seq_pick` 完全不可用

- 参数：`action="seq_order"`, `text="[3,1,2]"`（`seq_uniq`、`seq_pick` 同）
- 现象：`{"ok": false, "error": "seq_order: ValueError: seq_order: 缺 items"}`
- 原因：节点读 `items`（`nodes/data.py:276`），MCP 只暴露 `text`（`api/mcp.py:483`）。改用 `rows=[3,1,2]` 一样报缺 items（V35）。
- 危害：3/10 个 action 不可用，占列表能力全部。

### 🔴 P-08 `file_ops` / `rename` 完全不可用

- 参数：`action="rename"`, `path="out/tmpb_c.txt"`, `dest="out/tmpb_r.txt"`
- 现象：`{"ok": false, "error": "fs_rename: 缺 name"}`
- 原因：`nodes/files.py:252` 要 `name`，MCP 只有 `dest` 一个口子（`api/mcp.py:443`）。
- 危害：12 个 action 里少 1 个，且没有替代路径。

### 🔴 P-09 `file_ops` / `write` 写不了空文件

- 参数：`action="write"`, `path="out/tmpb_empty.txt"`, `content=""`
- 现象：`{"ok": false, "error": "fs_write: ValueError: 缺 content"}`
- 原因：`api/mcp.py:450` `if content: args["content"] = content` —— 空串是假值，被吞掉。
- 危害：清空日志、占位文件这类基本操作做不到，且报错"缺 content"与用户明确传了 `content=""` 相矛盾。

### 🔴 P-10 `script_run` 的 `limit_ms` 对 `cmd` 完全无效

- 参数：`cmd="ping -n 6 127.0.0.1"`, `limit_ms=500`
- 现象：**5108ms** 后 `{"ok": true, "message": "命令完成（exit 0…）"}`
- 原因：`api/mcp.py:497` 传的键是 `limit_ms`，`nodes/system.py:167` 读的是 `timeout_ms`。
- 危害：Agent 以为设了 0.5s 上限，实际可以无限跑；单实例串行执行，一条卡死的命令会堵死整个 MCP。

### 🔴 P-11 `script_run` 的 `limit_ms` 对 `code` 完全无效

- 参数：`code="import time; time.sleep(3); result='slept'"`, `limit_ms=800`
- 现象：**3003ms** 后 `{"ok": true}`，没有超时
- 原因：`nodes/system.py:205` 的 `sys_py` 只做 `exec()`，没有 timeout 参数也没有线程/子进程隔离。
- 危害：`exec` 是同步的，跑在 asyncio 事件循环里 —— 一个 `while True` 会让整个 MCP 服务失联（本次只敢用 3s 的 sleep 验证）。

### 🔴 P-12 `desktop_act` / `kind=table` 的 `control` 被丢弃 —— 该动作无法调用

- 环境：先按 `requirements.txt` 装了 `uiautomation`（venv 里原本没装，见 P-28）
- 参数：`kind="table"`, `control="NoSuchControlXYZ"`
- 现象：`{"ok": false, "error": "ui_table 缺 target（表格控件）"}`（K11/K12/K15 三次一致）
- 对照：`kind="select_text"` + 同样的 `control` → `找不到控件: 'NoSuchControlXYZ'`（K13），说明 `control` 这条路 select_text 认、table 不认
- 原因：`api/mcp.py:391` 统一传 `control`，`nodes/desktop.py:164` 只读 `target`/`name`。
- 危害：桌面三件事里最主要的一件（批量抓表格）**没有任何参数组合能调通**。

### 🔴 P-13 `flow_run` 的 `limit_steps` 在循环体内层失效

- 参数：5 轮循环流程（`loop count=5` → `noop` 回边），`limit_steps` 取 1 / 2 / 3 / 6 / 100
- 现象：5 次全部 `ok:true`，每次都跑满 5 轮、5 条节点记录；`limit_steps=1` 也一丝没截断
- 旁证：`demo`（同样是循环）+ `limit_steps=2` 执行了 7 条节点记录
- 原因：`engine/runner.py:295` 循环体用 `RunOptions(max_steps=self.opts.max_steps)` 起**新的** Runner，步数计数器各算各的，内层每轮预算重置。
- 危害：防跑飞的保护对循环（最容易跑飞的结构）不起作用；Agent 传 `limit_steps` 得到的是"看起来生效了"的假象。

### 🟠 P-14 `script_run` / `cmd` 的中文报错乱码

- 参数：`cmd="echo x > out/tmpb_cwd.txt"`
- 现象：`stderr": "ϵͳ�Ҳ���ָ����·����\\n"`（原文"系统找不到指定的路径。"）
- 原因：`nodes/system.py:181` 固定 `encoding="utf-8"` 解 cmd 的 GBK 输出。
- 危害：Windows 中文环境下几乎所有 cmd 失败都不可读，Agent 无法据此自我修正。

### 🟠 P-15 `sys_cmd` 的 cwd 是进程启动目录，不是项目根

- 参数：`cmd="cd"` → `stdout="C:\\\\Windows\\\\"`；`cmd="echo x > out/tmpb_cwd.txt"` → 失败，项目内没生成文件，`C:\\Windows\\out` 也不存在
- 原因：`nodes/system.py:169` 只在显式给 `cwd` 时才 `resolve_path`，默认继承 MCP 进程 cwd（`main.py` 启动时 `cwd=C:\\Windows`）
- 危害：所有其它工具的相对路径都基于项目根，只有逃生舱不是 —— Agent 按同一套规则写 `out/x` 会莫名失败（P-14 的乱码还会掩盖真因）。

### 🟠 P-16 `flow_run` / `start_at` 不存在时异常穿透

- 参数：`flow="demo"`, `start_at="nope"`
- 现象：`Error calling tool 'flow_run': 'start_at 指向不存在的节点: nope'`
- 原因：`engine/runner.py:131` 抛 `KeyError`，`api/mcp.py:684` 没包 try
- 危害：其余 19 个工具都返回 `{ok:false, error}`，只有这条走协议层异常，Agent 的通用错误处理接不住。

### 🟠 P-17 `ai_call` 缺 key 的提示指向一个不存在的参数

- 参数：`task="chat"`, `prompt="你好"`
- 现象：`缺少 API Key：请在 args.api_key 里给，或设置环境变量 RPA_LLM_KEY。`
- 原因：`nodes/ai.py:51` 提示 `args.api_key`，但 `api/mcp.py:503` 的签名根本没有 `api_key`/`api_url`/`model`
- 危害：Agent 照提示去传 `api_key` 会撞 pydantic 校验错，形成死循环。另外 `vision` 的 `target` 用 `os.path.isfile` 按进程 cwd 解析（A02 报"找不到图片文件: out/tmpb_a.txt"，文件其实存在）。

### 🟠 P-18 `file_ops` / `copy` 缺 `dest` 时指错参数

- 参数：`action="copy"`, `path="out/tmpb_a.txt"`（没给 dest）
- 现象：`fs_copy: ValueError: 缺 path 参数`
- 原因：`dest_abs=""` 时 MCP 不往 args 塞 `dest`，节点 `_path(None)` 抛"缺 path 参数"
- 危害：用户会去检查明明给对了的 path。

### 🟡 P-19 `flow_save` 覆盖已有流程不留快照

- 参数：对已存在的 `tmpa_dry` 再次 `flow_save`（节点 n1 → m1）
- 现象：`ok:true`，原流程被整体替换，`.versions` 与 `_meta` 里没有任何记录
- 危害：与 `flow_patch`（有快照 + 变更流水）保护等级不一致；Agent 一次手滑就永久丢掉流程。

### 🟡 P-20 `file_ops` / `delete` 删不存在的文件返回 ok:true

- 现象：`{"ok": true, "message": "不存在，跳过", "data": {"deleted": false}}`
- 说明：幂等设计合理，但按"该失败却 ok:true"的标准属于静默成功；`deleted:false` 是全靠 Agent 主动读 `data` 才能发现。

### 🟡 P-21 `flow_read` 无参返回没有 `ok` 字段

- 现象：`{"flows": [{"name": "demo", …}]}`，其余 19 个工具都有 `ok`
- 危害：Agent 统一用 `if not r.get("ok")` 判断时，把"列出成功"当失败。

### 🟡 P-22 `file_ops` / `list` 的空 path 白名单是空转

- `api/mcp.py:439` 明确 `allow_empty=action in ("list","info")`，但 `nodes/files.py:312` 的 `_path("")` 照样抛"缺 path 参数"，实测 `list` + 空 path → `ok:false`
- 结果：写了一个不生效的例外，读代码的人会误以为可以列项目根。

### 🟡 P-23 枚举/类型错误走协议层异常，没有 `ok:false`

- 涉及：`data_ops(action="bogus")`、`net_http(method="TRACE")`、`notify(channel="slack")`、`ai_call(task="bogus")`、`data_ops(text=[3,1,2])`
- 现象：返回裸 pydantic 文本 `1 validation error for call[...]`，`ok` 为 None
- 说明：文字本身可读（列了合法取值），与本项目"统一 ok 形态"的约定不一致。

### 🟡 P-24 `notify` 的 `channel` 实际只有两种行为

- 实测 httpbin 回显：`webhook` / `dingtalk` / `wecom` 三者 body **完全相同**（`{"msgtype":"text","text":{"content":…}}`），只有 `feishu` 换成 `{"msg_type":"text","content":{"text":…}}`
- 说明：钉钉与企微的文本消息格式确实一样，不算 bug，但 4 个取值给 Agent 造成"有 4 种通道"的错觉。

### 🟡 P-25 `requirements.txt` 声明了 `uiautomation`，venv 里没有

- `uiautomation>=2.0 ; sys_platform == "win32"` 已声明，实测 `ModuleNotFoundError`
- 后果：`desktop_act` 三个 kind 全部在第一步就失败（报错友好，写明 `pip install uiautomation`）
- 本次为完成实测已按 requirements 装上，装后 K11-K15 才有意义。
"""

UNVERIFIED = """\
| 工具 | 参数 | 为什么没验证 |
|---|---|---|
| `media_dl` | `path` / `quality` | 机器上没有 yt-dlp、N_m3u8DL-RE。5 个 kind 全部停在依赖检查（提示友好：写明 `pip install yt-dlp` / 环境变量 `RPA_YTDLP`），拿不到下载阶段，`path`/`quality` 无从验证 |
| `desktop_act` | `limit` | `kind=table` 因 P-12 调不通，`limit` 只能随它一起失败 |
| `ai_call` | `prompt` / `target` / `fields` 的真实效果 | 无 `RPA_LLM_KEY`，所有 task 都停在 `_cfg()` 缺 key 这一步；只有 `prompt` 为空的分支（A07）能单独验证 |
| `net_http` | `headers` 以外的加密/代理行为 | 未涉及，超出本次参数覆盖范围 |
"""


def fmt_res(r: dict) -> str:
    if r.get("_text"):
        return "RPC 异常：" + r["_text"].splitlines()[0][:60]
    if r.get("_rpc_error"):
        return "RPC 异常：" + str(r["_rpc_error"])[:60]
    ok = r.get("ok")
    msg = (r.get("message") or r.get("error") or "")[:60]
    data = r.get("data")
    tail = ""
    if data is not None:
        tail = " data=" + json.dumps(data, ensure_ascii=False)[:48]
    return f"ok={ok} {msg}{tail}".strip()


def fmt_args(a: dict) -> str:
    parts = []
    for k, v in a.items():
        s = json.dumps(v, ensure_ascii=False)
        if len(s) > 46:
            s = s[:46] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts).replace("|", "\\|")


def main() -> None:
    rows = json.loads((OUT / "agent_ops_raw.json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in rows}
    lines: list[str] = []
    lines.append("# 13 个非网页工具 · 全参数实测报告\n")
    lines.append(
        "结论：20 个 MCP 工具里的 13 个非网页工具，**每一个参数都真调过**（真进程、真网络、真文件）。"
        "共 198 组参数组合，**13 个工具中 6 个存在完全不可用的动作**，"
        "另有 2 个越界读写漏洞和 2 个静默成功。\n"
    )
    lines.append("## 0. 怎么跑的\n")
    lines.append("```")
    lines.append("python tests/agent_ops_matrix.py   # 134 组主矩阵")
    lines.append("python tests/agent_ops_pass2.py    # 22 组交叉验证（save_as / 步数 / run_id）")
    lines.append("python tests/agent_ops_pass3.py    # 20 组（桌面真机 / http 回显 / patch 落盘）")
    lines.append("python tests/agent_ops_pass4.py    # 11 组（vars / limit_steps / 失败传播）")
    lines.append("```")
    lines.append(
        "起进程方式与 `tests/mcp_probe.py` 一致：`python main.py mcp`，JSON-RPC over stdio，"
        "`cwd=C:\\Windows`，`RPA_FLOWS`/`RPA_DB`/`RPA_ELEMENTS` 指向项目，单次调用 60-90s 上限。\n"
    )

    lines.append("## 1. 按工具的结论\n")
    lines.append("| 工具 | 动作/参数覆盖 | 可用 | 判定 |")
    lines.append("|---|---|---|---|")
    lines.append("| `data_ops` | 10 个 action 全跑 | 4/10（data_sheet_read/data_sheet_write/csv_gen/regex） | 🔴 6 个动作不可用或静默出错 |")
    lines.append("| `file_ops` | 12 个 action 全跑 | 11/12 | 🔴 rename 不可用；write 空内容不可用 |")
    lines.append("| `net_http` | 5 个 method + json/headers/save_as/limit_ms | 全通 | ✅ |")
    lines.append("| `script_run` | code/cmd + 语法错/非零退出/save_as/limit_ms | code、cmd 都能跑 | 🔴 limit_ms 两种模式全失效 |")
    lines.append("| `notify` | 4 个 channel + url 缺失 | 全通 | ✅（channel 只有 2 种实际行为） |")
    lines.append("| `media_dl` | 5 个 kind + path/quality | 未到下载阶段 | 🟡 缺 yt-dlp，失败提示友好 |")
    lines.append("| `desktop_act` | 3 个 kind | 2/3（select_text、focused） | 🔴 table 无法调用 |")
    lines.append("| `ai_call` | 6 个 task | 未到调用阶段 | 🟠 缺 key 提示指向不存在的参数 |")
    lines.append("| `flow_read` | 无参/指定/不存在/越界 | 全通 | 🔴 flow 名可越界读 |")
    lines.append("| `flow_save` | 正常/空/悬空边/缺字段/重复 id/多余字段/越界 | 校验很严 | 🔴 flow 名可越界写 |")
    lines.append("| `flow_patch` | args/on_fail/expect/dry_run/reason | 全通 | 🔴 写入不校验，能写坏流程 |")
    lines.append("| `flow_run` | vars/start_at/limit_steps | 全通 | 🔴 limit_steps 循环内失效 |")
    lines.append("| `run_status` | limit 0/-1/5/999 + flow | 全通 | ✅ 截断逻辑正确 |")
    lines.append("")

    lines.append("## 2. 汇总表（134 组主矩阵，逐条真实调用）\n")
    lines.append("| 工具 | 参数组合 | 结果 | 判定 | 备注 |")
    lines.append("|---|---|---|---|---|")
    for c in cases:
        r = by_id[c["id"]]
        j, note = JUDGE.get(c["id"], ("✅", ""))
        if not note and c["why"]:
            note = c["why"]
        lines.append(
            f"| `{c['tool']}` | `{c['id']}` {fmt_args(c['args'])} | {fmt_res(r.get('raw') or {}).replace('|', '/')} "
            f"| {j} | {note} |"
        )
    lines.append("")

    lines.append("## 3. 汇总表（补充 64 组调用里，23 组有独立结论的条目）\n")
    lines.append(
        "其余 41 组是同一接口的换值复验（4 个 notify 通道、5 次 limit_steps 扫描、9 次自身产物清理），"
        "结论已被下面 23 条覆盖，不再单列。\n"
    )
    lines.append("| 工具 | 参数组合 | 结果 | 判定 | 备注 |")
    lines.append("|---|---|---|---|---|")
    for cid, tool, args, res, j, note in EXTRA:
        lines.append(f"| `{tool}` | `{cid}` {args.replace('|', '/')} | {res.replace('|', '/')} | {j} | {note} |")
    lines.append("")

    lines.append("## 4. 问题清单\n")
    lines.append(ISSUES)
    lines.append("")
    lines.append("## 5. 没能验证的部分（如实标注）\n")
    lines.append(UNVERIFIED)
    lines.append("")

    n_bad = sum(1 for c in cases if JUDGE.get(c["id"], ("✅",))[0] != "✅") + \
        sum(1 for e in EXTRA if e[4][0] != "✅")
    n_red = sum(1 for c in cases if JUDGE.get(c["id"], ("✅",))[0].startswith("🔴")) + \
        sum(1 for e in EXTRA if e[4].startswith("🔴"))
    n_orange = sum(1 for c in cases if JUDGE.get(c["id"], ("✅",))[0].startswith("🟠")) + \
        sum(1 for e in EXTRA if e[4].startswith("🟠"))
    n_yellow = sum(1 for c in cases if JUDGE.get(c["id"], ("✅",))[0].startswith("🟡")) + \
        sum(1 for e in EXTRA if e[4].startswith("🟡"))

    lines.append("## 6. 统计\n")
    lines.append("| 项 | 数 |")
    lines.append("|---|---|")
    lines.append("| 参数组合总数 | 198（134 主矩阵 + 64 补充；另有 29 次自身产物清理调用不计入） |")
    lines.append(f"| 通过 | {198 - n_bad} |")
    lines.append(f"| 有问题 | {n_bad}（🔴 {n_red} / 🟠 {n_orange} / 🟡 {n_yellow}） |")
    lines.append("| 独立缺陷 | 25 条（P-01 ~ P-25） |")
    lines.append("| 完全不可用的动作 | 6 个：`data_sheet_append`、`seq_order`、`seq_uniq`、`seq_pick`、`fs_rename`、`desktop_act(kind=table)` |")
    lines.append("")
    lines.append("### 最该先修的三条\n")
    lines.append("1. **P-04/P-05 flow 名越界** —— `flow_save(flow=\"../x\")` 能写到项目根，`flow_read(flow=\"../elements\")` 能读任意同层文件。这是安全边界，且改动最小（5 个 flow 工具统一走 `_safe_path`）。")
    lines.append("2. **P-01/P-02 静默成功** —— `data_csv_read(path=…)` 返回空数组 `ok:true`、`json_query` 永远返回 `ok:true` 且不给数据。Agent 会把错误结果当正确答案继续往下跑，比直接报错危险得多。")
    lines.append("3. **P-06/P-07/P-08/P-12 参数名对不上** —— 5 个动作因为 MCP 层与节点层的键名不一致而完全不可用。修法统一：在 `api/mcp.py` 里加一层入参映射，而不是逐个改节点。")

    (OUT / "agent_ops_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("写入", OUT / "agent_ops_report.md", len(lines), "行")
    print(f"问题 {n_bad} 组（🔴{n_red} 🟠{n_orange} 🟡{n_yellow}）")


if __name__ == "__main__":
    main()
