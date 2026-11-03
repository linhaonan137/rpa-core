"""13 个非网页工具的全参数实测用例矩阵。

    python tests/agent_ops_matrix.py

结果写 out/agent_ops_raw.json，原始明细写 out/agent_ops_trace.jsonl
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_ops_probe import MCP, ROOT  # noqa: E402

OUT = ROOT / "out"
TRACE = OUT / "agent_ops_trace.jsonl"
RESULT = OUT / "agent_ops_raw.json"

cases: list[dict] = []


def C(cid: str, tool: str, args: dict, expect: str = "ok", why: str = "", timeout: float = 70) -> None:
    cases.append({"id": cid, "tool": tool, "args": args, "expect": expect, "why": why, "timeout": timeout})


# ================================================================ data_ops
C("D01", "data_ops", {"action": "data_sheet_write", "path": "out/tmpb_t.xlsx", "sheet": "S1",
                       "rows": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]}, "ok", "写表")
C("D02", "data_ops", {"action": "data_sheet_read", "path": "out/tmpb_t.xlsx", "sheet": "S1"}, "ok", "读表验证数据")
C("D03", "data_ops", {"action": "data_sheet_read", "path": "out/tmpb_t.xlsx", "sheet": "NoSuch"}, "fail", "不存在的 sheet")
C("D04", "data_ops", {"action": "data_sheet_read", "path": "out/tmpb_none.xlsx"}, "fail", "不存在的文件")
C("D05", "data_ops", {"action": "data_sheet_append", "path": "out/tmpb_t.xlsx", "sheet": "S1",
                       "rows": [{"a": 3, "b": "z"}]}, "ok", "追加行")
C("D06", "data_ops", {"action": "data_sheet_append", "path": "out/tmpb_t2.xlsx", "sheet": "S1",
                       "rows": [{"a": 1}]}, "ok", "追加到新文件")
C("D07", "data_ops", {"action": "csv_gen", "rows": [{"x": 1, "y": "a"}, {"x": 2, "y": "b"}]}, "ok", "生成 CSV")
C("D08", "data_ops", {"action": "data_csv_read", "text": "x,y\n1,a\n2,b\n"}, "ok", "解析 CSV 文本")
C("D08b", "file_ops", {"action": "write", "path": "out/tmpb_d.csv",
                        "content": "x,y\n1,a\n2,b\n"}, "ok", "造一个 CSV 供下一条读")
C("D09", "data_ops", {"action": "data_csv_read", "path": "out/tmpb_d.csv"}, "ok", "从文件解析 CSV（path）")
C("D10", "data_ops", {"action": "seq_order", "text": "[3,1,2]"}, "ok", "列表排序")
C("D11", "data_ops", {"action": "seq_uniq", "text": "[1,1,2]"}, "ok", "列表去重")
C("D12", "data_ops", {"action": "seq_pick", "text": "[1,2,3]"}, "ok", "列表过滤")
C("D13", "data_ops", {"action": "regex", "text": "订单 A123 金额 99", "pattern": r"[A-Z]\d+"}, "ok", "正则抽取")
C("D14", "data_ops", {"action": "regex", "text": "abc", "pattern": "([a-z"}, "fail", "非法正则")
C("D15", "data_ops", {"action": "regex", "text": "abc", "pattern": "zzz"}, "ok", "无匹配应 ok 且空")
C("D16", "data_ops", {"action": "json_query", "text": '{"a":{"b":[10,20]}}', "key": "a.b[1]"}, "ok", "字典取值")
C("D17", "data_ops", {"action": "json_query", "text": '{"a":1}', "key": "zzz"}, "any", "取不到的键")
C("D18", "data_ops", {"kind": "csv_gen", "rows": [{"k": "v"}]}, "ok", "kind 别名互认")
C("D19", "data_ops", {"action": "data_sheet_read", "path": "C:/Windows/win.ini"}, "fail", "绝对路径必须拒绝")
C("D20", "data_ops", {"action": "data_sheet_read", "path": "../rpa.db"}, "fail", ".. 越界必须拒绝")
C("D21", "data_ops", {"action": "bogus"}, "fail", "非法 action")
C("D22", "data_ops", {"action": "data_sheet_write", "path": "out/tmpb_t.xlsx", "sheet": "S2",
                       "rows": [{"q": 1}], "save_as": "tmpb_saved"}, "ok", "save_as 写变量")
C("D23", "data_ops", {"action": "data_sheet_write", "rows": [{"a": 1}]}, "fail", "缺 path 必须失败")
C("D24", "data_ops", {"action": "csv_gen", "rows": []}, "any", "空 rows")

# ================================================================ file_ops
C("F01", "file_ops", {"action": "write", "path": "out/tmpb_a.txt", "content": "hello"}, "ok", "写文件")
C("F02", "file_ops", {"action": "read", "path": "out/tmpb_a.txt"}, "ok", "读回来")
C("F03", "file_ops", {"action": "append", "path": "out/tmpb_a.txt", "content": " world"}, "ok", "追加")
C("F04", "file_ops", {"action": "read", "path": "out/tmpb_a.txt", "save_as": "tmpb_txt"}, "ok", "读回并验追加")
C("F05", "file_ops", {"action": "copy", "path": "out/tmpb_a.txt", "dest": "out/tmpb_c.txt"}, "ok", "复制")
C("F06", "file_ops", {"action": "exists", "path": "out/tmpb_c.txt"}, "ok", "存在性")
C("F07", "file_ops", {"action": "exists", "path": "out/tmpb_zzz.txt"}, "ok", "不存在也要回答 false")
C("F08", "file_ops", {"action": "info", "path": "out/tmpb_c.txt"}, "ok", "元信息")
C("F09", "file_ops", {"action": "hash", "path": "out/tmpb_c.txt"}, "ok", "摘要")
C("F10", "file_ops", {"action": "rename", "path": "out/tmpb_c.txt", "dest": "out/tmpb_r.txt"}, "ok", "改名")
C("F11", "file_ops", {"action": "move", "path": "out/tmpb_r.txt", "dest": "out/tmpb_m.txt"}, "ok", "移动")
C("F12", "file_ops", {"action": "list", "path": "out"}, "ok", "列目录")
C("F13", "file_ops", {"action": "list", "path": "out/tmpb_nodir"}, "fail", "列不存在的目录")
C("F14", "file_ops", {"action": "mkdir", "path": "out/tmpb_dir"}, "ok", "建目录")
C("F15", "file_ops", {"action": "write", "path": "out/tmpb_dir/inner.txt", "content": "x"}, "ok", "建父目录后写")
C("F16", "file_ops", {"action": "write", "path": "out/tmpb_empty.txt", "content": ""}, "ok", "写空文件")
C("F17", "file_ops", {"action": "write", "path": "out/tmpb_noc.txt"}, "fail", "缺 content 必须失败")
C("F18", "file_ops", {"action": "read", "path": "out/tmpb_none.txt"}, "fail", "读不存在的文件")
C("F19", "file_ops", {"action": "read", "path": "C:/Windows/win.ini"}, "fail", "绝对路径必须拒绝")
C("F20", "file_ops", {"action": "write", "path": "../tmpb_escape.txt", "content": "x"}, "fail", ".. 越界必须拒绝")
C("F21", "file_ops", {"action": "delete", "path": "out/tmpb_m.txt"}, "ok", "删文件")
C("F22", "file_ops", {"action": "delete", "path": "out/tmpb_m.txt"}, "any", "重复删（判断是否静默成功）")
C("F23", "file_ops", {"action": "delete", "path": "out/tmpb_dir"}, "ok", "删目录")
C("F24", "file_ops", {"kind": "write", "path": "out/tmpb_k.txt", "content": "kind别名"}, "ok", "kind 别名互认")
C("F25", "file_ops", {"action": "copy", "path": "out/tmpb_a.txt"}, "fail", "copy 缺 dest")
C("F26", "file_ops", {"action": "read", "path": ""}, "fail", "空 path")
C("F27", "file_ops", {"action": "list", "path": ""}, "any", "list 空 path 落点")

# ================================================================ net_http
C("H01", "net_http", {"url": "https://httpbin.org/get"}, "ok", "GET")
C("H02", "net_http", {"url": "https://httpbin.org/post", "method": "POST",
                           "json_body": {"a": 1}}, "ok", "POST json")
C("H03", "net_http", {"url": "https://httpbin.org/put", "method": "PUT", "json_body": {"b": 2}}, "ok", "PUT")
C("H04", "net_http", {"url": "https://httpbin.org/delete", "method": "DELETE"}, "ok", "DELETE")
C("H05", "net_http", {"url": "https://httpbin.org/patch", "method": "PATCH", "json_body": {"c": 3}}, "ok", "PATCH")
C("H06", "net_http", {"url": "https://httpbin.org/status/404"}, "fail", "404 应失败但带 data")
C("H07", "net_http", {"url": "https://httpbin.org/get", "headers": {"X-Probe": "yes"}}, "ok", "自定义 header 回显")
C("H08", "net_http", {"url": "https://api.github.com/zen"}, "ok", "真实 API")
C("H09", "net_http", {"url": "https://httpbin.org/delay/3", "limit_ms": 800}, "fail", "limit_ms 应触发超时")
C("H10", "net_http", {"url": "notaurl"}, "fail", "非法 url")
C("H11", "net_http", {"url": "https://httpbin.org/get", "save_as": "tmpb_resp"}, "ok", "save_as")
C("H12", "net_http", {"url": "https://httpbin.org/get", "method": "TRACE"}, "fail", "非法 method")

# ================================================================ script_run
C("S01", "script_run", {"code": "result = 1 + 1"}, "ok", "python 正常")
C("S02", "script_run", {"code": "result = 1 +\n"}, "fail", "python 语法错")
C("S03", "script_run", {"code": "raise ValueError('boom')"}, "fail", "python 运行时异常")
C("S04", "script_run", {"code": "print('hi'); result = None"}, "ok", "print 捕获")
C("S05", "script_run", {"cmd": "echo hello"}, "ok", "cmd 正常")
C("S06", "script_run", {"cmd": "exit 3"}, "fail", "cmd 非零退出")
C("S07", "script_run", {"cmd": "nosuchcommand_xyz"}, "fail", "cmd 不存在")
C("S08", "script_run", {"cmd": "ping -n 6 127.0.0.1", "limit_ms": 500}, "fail",
  "cmd 的 limit_ms 是否生效（应 0.5s 超时）", timeout=80)
C("S09", "script_run", {"code": "import time; time.sleep(3); result='slept'", "limit_ms": 800}, "fail",
  "code 的 limit_ms 是否生效（应 0.8s 超时）", timeout=80)
C("S10", "script_run", {"save_as": "tmpb_sv", "code": "result = 42"}, "ok", "save_as")
C("S11", "script_run", {}, "fail", "两者都不给")

# ================================================================ notify
C("N01", "notify", {"message": "hi", "channel": "webhook", "url": "https://httpbin.org/post"}, "ok", "webhook")
C("N02", "notify", {"message": "hi", "channel": "dingtalk", "url": "https://httpbin.org/post"}, "ok", "dingtalk body")
C("N03", "notify", {"message": "hi", "channel": "wecom", "url": "https://httpbin.org/post"}, "ok", "wecom body")
C("N04", "notify", {"message": "hi", "channel": "feishu", "url": "https://httpbin.org/post"}, "ok", "feishu body")
C("N05", "notify", {"message": "hi"}, "fail", "缺 url")
C("N06", "notify", {"message": "hi", "channel": "slack", "url": "https://httpbin.org/post"}, "fail", "非法 channel")

# ================================================================ media_dl
C("M01", "media_dl", {"url": "https://httpbin.org/image/png", "kind": "video"}, "fail", "非视频源")
C("M02", "media_dl", {"url": "https://httpbin.org/image/png", "kind": "audio"}, "fail", "audio")
C("M03", "media_dl", {"url": "https://httpbin.org/image/png", "kind": "info"}, "fail", "info")
C("M04", "media_dl", {"url": "https://httpbin.org/image/png", "kind": "playlist"}, "fail", "playlist")
C("M05", "media_dl", {"url": "https://httpbin.org/image/png", "kind": "m3u8"}, "fail", "m3u8")
C("M06", "media_dl", {"url": "https://httpbin.org/image/png", "path": "C:/Windows/tmp"}, "fail", "绝对路径必须拒绝")
C("M07", "media_dl", {"url": "https://httpbin.org/image/png", "path": "../tmpb", "quality": "1080"}, "fail", "越界必须拒绝")

# ================================================================ desktop_act
C("K01", "desktop_act", {"kind": "table", "control": "Notepad"}, "fail", "table 抓不到控件")
C("K02", "desktop_act", {"kind": "select_text", "control": "Notepad"}, "fail", "select_text")
C("K03", "desktop_act", {"kind": "focused"}, "any", "读焦点控件")
C("K04", "desktop_act", {"kind": "table", "control": "Notepad", "save_as": "tmpb_tb", "limit": 5},
  "fail", "limit + save_as 组合")

# ================================================================ ai_call
C("A01", "ai_call", {"task": "chat", "prompt": "你好"}, "fail", "chat 无 key")
C("A02", "ai_call", {"task": "vision", "target": "out/tmpb_a.txt", "prompt": "看图"}, "fail", "vision 无 key")
C("A03", "ai_call", {"task": "extract", "target": "标题：A 价格：9", "fields": ["标题", "价格"]}, "fail", "extract")
C("A04", "ai_call", {"task": "normalize", "target": "2024/1/2"}, "fail", "normalize")
C("A05", "ai_call", {"task": "classify", "target": "买了一个手机", "fields": ["数码", "食品"]}, "fail", "classify")
C("A06", "ai_call", {"task": "find_selector", "prompt": "登录按钮"}, "fail", "find_selector")
C("A07", "ai_call", {"task": "chat", "prompt": ""}, "fail", "空 prompt")
C("A08", "ai_call", {"task": "bogus", "prompt": "x"}, "fail", "非法 task")

# ================================================================ flow_read
C("R01", "flow_read", {}, "ok", "列流程")
C("R02", "flow_read", {"flow": "demo"}, "ok", "读 demo")
C("R03", "flow_read", {"flow": "tmpb_nosuch"}, "fail", "不存在的流程")
C("R04", "flow_read", {"flow": "../elements"}, "fail", "flow 名路径穿越（../ 逃出 flows）")

# ================================================================ flow_save
GOOD = {"name": "探针流程", "nodes": [{"id": "n1", "type": "noop", "args": {}},
                                       {"id": "n2", "type": "noop", "args": {}}],
        "edges": [{"id": "e1", "from": "n1", "to": "n2"}]}
C("W01", "flow_save", {"flow": "tmpa_ok", "content": GOOD}, "ok", "正常保存")
C("W02", "flow_save", {"flow": "tmpa_empty", "content": {"name": "空", "nodes": []}}, "fail", "空 nodes 必须拒绝")
C("W03", "flow_save", {"flow": "tmpa_badedge",
                        "content": {"name": "悬空边", "nodes": [{"id": "n1", "type": "noop"}],
                                    "edges": [{"id": "e1", "from": "n1", "to": "n9"}]}}, "fail", "悬空边必须拒绝")
C("W04", "flow_save", {"flow": "tmpa_noname", "content": {"nodes": [{"id": "n1", "type": "noop"}]}},
  "fail", "缺 name 必须拒绝")
C("W05", "flow_save", {"flow": "tmpa_dup",
                        "content": {"name": "重复 id", "nodes": [{"id": "n1", "type": "noop"},
                                                                 {"id": "n1", "type": "noop"}]}},
  "fail", "重复节点 id 必须拒绝")
C("W06", "flow_save", {"flow": "tmpa_extra",
                        "content": {"name": "多余字段", "nodes": [{"id": "n1", "type": "noop", "bogus": 1}]}},
  "fail", "未知字段必须拒绝")
C("W07", "flow_save", {"flow": "../tmpa_escape", "content": GOOD}, "fail", "flow 名路径穿越")

# ================================================================ flow_patch
C("P01", "flow_patch", {"flow": "tmpa_ok", "node": "n1", "args": {"x": 1}}, "fail", "缺 reason 必须拒绝")
C("P02", "flow_patch", {"flow": "tmpb_none", "node": "n1", "args": {"x": 1}, "reason": "t"}, "fail", "流程不存在")
C("P03", "flow_patch", {"flow": "tmpa_ok", "node": "n9", "args": {"x": 1}, "reason": "t"}, "fail", "节点不存在")
C("P04", "flow_patch", {"flow": "tmpa_ok", "node": "n1", "args": {"x": 1}, "reason": "试改"}, "ok",
  "dry_run 默认 true，只出 diff")
C("P05", "flow_patch", {"flow": "tmpa_ok", "node": "n1", "args": {"x": 1}, "reason": "试改", "dry_run": False},
  "ok", "落盘 + 快照")
C("P06", "flow_patch", {"flow": "tmpa_ok", "node": "n1", "on_fail": {"mode": "retry", "attempts": 3},
                         "reason": "改重试", "dry_run": False}, "ok", "改 on_fail")
C("P07", "flow_patch", {"flow": "tmpa_ok", "node": "n1", "expect": {"limit_ms": 500},
                         "reason": "非法 expect", "dry_run": False}, "fail",
  "expect 没有任何断言字段，应被校验拒绝")
C("P08", "flow_patch", {"flow": "tmpa_ok", "node": "n1", "on_fail": {"mode": "bogus"},
                         "reason": "非法 on_fail", "dry_run": False}, "fail", "非法 on_fail 应拒绝")
C("P09", "flow_patch", {"flow": "tmpa_ok", "node": "n1", "args": {"x": 1}, "reason": "t", "dry_run": True},
  "ok", "dry_run 不应落盘")

# ================================================================ flow_run
C("X01", "flow_run", {"flow": "demo"}, "ok", "跑 demo")
C("X02", "flow_run", {"flow": "demo", "vars": {"unused": 1}}, "ok", "vars 注入")
C("X03", "flow_run", {"flow": "demo", "start_at": "n5"}, "ok", "从 n5 起跑（跳过上游）")
C("X04", "flow_run", {"flow": "demo", "start_at": "nope"}, "fail", "start_at 不存在应失败")
C("X05", "flow_run", {"flow": "demo", "limit_steps": 1}, "fail", "limit_steps=1 应截断")
C("X06", "flow_run", {"flow": "demo", "limit_steps": 0}, "fail", "limit_steps=0")
C("X07", "flow_run", {"flow": "tmpb_none"}, "fail", "流程不存在")
C("X08", "flow_run", {"flow": "tmpa_ok"}, "ok", "跑临时流程")

# ================================================================ run_status
C("T01", "run_status", {}, "ok", "默认 10 条")
C("T02", "run_status", {"limit": 0}, "ok", "limit=0 应回默认")
C("T03", "run_status", {"limit": -1}, "ok", "limit=-1 应回默认")
C("T04", "run_status", {"limit": 999}, "ok", "limit=999 应封顶 200")
C("T05", "run_status", {"flow": "demo"}, "ok", "按流程过滤")
C("T06", "run_status", {"flow": "tmpb_none"}, "any", "不存在的流程")


def brief(v, n=220) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    TRACE.write_text("", encoding="utf-8")
    m = MCP()
    rows: list[dict] = []
    try:
        for c in cases:
            t0 = time.time()
            try:
                r = m.call(c["tool"], c["args"], timeout=c["timeout"])
            except Exception as exc:  # noqa: BLE001
                r = {"_probe_error": f"{type(exc).__name__}: {exc}"}
            ms = round((time.time() - t0) * 1000)
            ok = r.get("ok")
            verdict = "PASS"
            if c["expect"] == "ok" and ok is not True:
                verdict = "MISMATCH"
            elif c["expect"] == "fail" and ok is not False:
                verdict = "MISMATCH"
            row = {
                "id": c["id"], "tool": c["tool"], "expect": c["expect"], "why": c["why"],
                "ms": ms, "ok": ok, "verdict": verdict,
                "error": r.get("error") or r.get("_rpc_error") or r.get("_probe_error") or "",
                "message": (r.get("message") or "")[:300],
                "data_brief": brief(r.get("data")) if r.get("data") is not None else "",
                "raw": r,
            }
            rows.append(row)
            with TRACE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"id": c["id"], "ms": ms, "result": r}, ensure_ascii=False) + "\n")
            flag = "  " if verdict == "PASS" else "**"
            print(f"{flag}{c['id']:<4} {c['tool']:<13} {c['why'][:34]:<34} ok={str(ok):<5} "
                  f"{ms:>6}ms {brief(row['error'] or row['message'], 90)}", flush=True)
    finally:
        m.kill()

    RESULT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    bad = [r for r in rows if r["verdict"] != "PASS"]
    print(f"\n共 {len(rows)} 组，MISMATCH {len(bad)} 组: {[r['id'] for r in bad]}")


if __name__ == "__main__":
    main()
