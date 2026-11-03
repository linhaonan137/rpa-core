"""MCP 参数矩阵：把 20 个工具按「正常 / 边界 / 应报错」各跑一遍。

用法：
    python tests/mcp_matrix.py            # 全量，约 1~2 分钟
    python tests/mcp_matrix.py -k web_    # 只跑名字含 web_ 的工具

通信层复用 tests/mcp_probe.py 的做法（subprocess + JSON Lines JSON-RPC），
不重写协议：单进程连续调用（浏览器状态要跨用例保持），超时/进程死掉就重启。

三类判定：
    PASS / EXP-FAIL   与预期一致（EXP-FAIL = 该报错的确实报错了）
    UNEXPECTED-FAIL   该成功的失败了 → 功能不可用 / 参数名对不上
    UNEXPECTED-OK     该报错的却成功了 → 静默成功

产物：out/mx_matrix.json（全量结果，含返回体片段）
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
PY = sys.executable
MAIN = str(ROOT / "main.py")
CALL_TIMEOUT = 60.0          # 单次调用上限（硬性要求）
QUOTES = "https://quotes.toscrape.com/"
HTTPBIN_FORM = "https://httpbin.org/forms/post"

ENV = dict(os.environ)
ENV.update({
    "RPA_FLOWS": str(ROOT / "flows"),
    "RPA_DB": str(ROOT / "rpa.db"),
    "RPA_ELEMENTS": str(ROOT / "elements.json"),
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
})


# ---------------------------------------------------------------- 通信层


class MCP:
    """一个长活的 MCP 子进程；超时就重启，保证后续用例还能跑。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.id = 0
        self.err: list[str] = []
        self.restarts = 0
        self.schemas: dict[str, dict] = {}
        self.start()

    # -- 生命周期 --------------------------------------------------

    def start(self) -> None:
        self.err = []
        self.proc = subprocess.Popen(
            [PY, MAIN, "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=ENV, cwd=r"C:\Windows",
        )
        threading.Thread(target=self._drain, daemon=True).start()
        msg = self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "matrix", "version": "1.0"},
        }, timeout=30)
        if msg is None:
            raise RuntimeError(f"MCP 起不来: {self.tail()}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listing = self._rpc("tools/list", {}, timeout=30) or {}
        for t in (listing.get("result") or {}).get("tools", []):
            self.schemas[t["name"]] = t.get("inputSchema") or {}

    def _drain(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self.err.append(line.rstrip())
            del self.err[:-60]

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def restart(self) -> None:
        self.stop()
        self.restarts += 1
        self.start()

    def tail(self) -> str:
        return " | ".join(self.err[-4:])

    # -- 帧读写 ----------------------------------------------------

    def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _read(self, timeout: float) -> tuple[str, object]:
        """返回 (状态, 值)。状态：ok / timeout / eof / badjson / ioerr。"""
        box: dict[str, object] = {}

        def rd() -> None:
            try:
                box["line"] = self.proc.stdout.readline()  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        t = threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout if timeout > 0 else 0.001)
        if t.is_alive():
            return "timeout", None
        if "err" in box:
            return "ioerr", str(box["err"])
        line = str(box.get("line") or "")
        if not line:
            return "eof", None
        try:
            return "ok", json.loads(line)
        except json.JSONDecodeError:
            return "badjson", line[:200]

    def _rpc(self, method: str, params: dict, timeout: float) -> dict | None:
        self.id += 1
        rid = self.id
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except Exception:  # noqa: BLE001 - 进程已死
            return None
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            kind, msg = self._read(remain)
            if kind != "ok":
                return None
            if isinstance(msg, dict) and msg.get("id") == rid:
                return msg

    # -- 工具调用 --------------------------------------------------

    def call(self, tool: str, args: dict, timeout: float = CALL_TIMEOUT) -> dict:
        """返回 {status, ok, message, payload, ms}。

        status: ok / timeout / crash / badjson
        """
        t0 = time.perf_counter()
        if not self.proc or self.proc.poll() is not None:
            self.restart()
        msg = self._rpc("tools/call", {"name": tool, "arguments": args}, timeout)
        ms = (time.perf_counter() - t0) * 1000.0
        if msg is None:
            dead = not self.proc or self.proc.poll() is not None
            tail = self.tail()
            # 无论超时还是崩溃，进程状态都不可信了 → 重启
            try:
                self.restart()
            except Exception:  # noqa: BLE001
                self.proc = None
            return {
                "status": "crash" if dead else "timeout",
                "ok": False, "message": "进程已退出" if dead else "调用超时",
                "payload": None, "ms": ms, "stderr": tail,
            }
        return self._unpack(msg, ms)

    @staticmethod
    def _unpack(msg: dict, ms: float) -> dict:
        if "error" in msg:
            err = msg.get("error") or {}
            text = str(err.get("message") or err)
            return {"status": "ok", "ok": False, "message": text,
                    "payload": {"rpc_error": err}, "ms": ms, "rpc_error": True}
        res = msg.get("result") or {}
        payload: object = None
        texts = []
        for part in res.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text") or "")
        for tx in texts:
            try:
                payload = json.loads(tx)
                break
            except (json.JSONDecodeError, TypeError):
                continue
        if payload is None and texts:
            payload = {"raw_text": texts[0]}
        if isinstance(payload, dict) and "ok" in payload:
            ok = bool(payload.get("ok"))
            message = str(payload.get("error") or payload.get("message") or "")
        else:
            ok = not res.get("isError")
            message = "" if ok else (texts[0] if texts else "工具报错但无说明")
        return {"status": "ok", "ok": ok, "message": message, "payload": payload, "ms": ms}


# ---------------------------------------------------------------- 用例


def C(tool: str, label: str, args: dict, expect: str = "ok",
      note: str = "", artifact: str = "", detail: bool = False) -> dict:
    return {"tool": tool, "label": label, "args": args, "expect": expect,
            "note": note, "artifact": artifact, "detail": detail}


MX_FLOW = {
    "name": "矩阵测试",
    "nodes": [{"id": "n1", "type": "set_var", "args": {"name": "x", "value": 1}}],
    "edges": [],
}


def cases() -> list[dict]:
    out: list[dict] = []
    A = out.append

    # ---------------- 网页：先打开 quotes
    A(C("web_open", "①新建打开", {"url": QUOTES, "kind": "new"}))
    A(C("web_read", "①文本 div.quote", {"target": "div.quote", "kind": "text"}, detail=True))
    A(C("web_read", "②list div.quote", {"kind": "list", "target": "div.quote"}, detail=True))
    A(C("web_read", "③eval 箭头函数", {"kind": "eval", "code": "() => 1+1"},
        expect="fail", note="target 必填，eval 无法只传 code"))
    A(C("web_read", "③b eval 补 target", {"kind": "eval", "target": "", "code": "() => 1+1"}, detail=True))
    A(C("web_read", "③c eval 裸表达式", {"kind": "eval", "target": "", "code": "1+1"}, detail=True))
    A(C("web_read", "④元素不存在", {"target": "不存在_的元素", "kind": "text", "limit_ms": 3000}, expect="fail"))
    A(C("web_wait", "①等 body 可见", {"target": "body", "state": "visible"}))
    A(C("web_wait", "②固定等待 ms", {"ms": 500}, expect="fail",
        note="target 必填，固定等待走不通"))
    A(C("web_wait", "③超时 1s", {"target": "不存在_的元素", "limit_ms": 1000}, expect="fail"))
    A(C("web_wait", "⑤when=text", {"target": "The world", "when": "text", "limit_ms": 3000}))
    A(C("web_wait", "⑥when=url 错误地址", {"target": "https://example.com/", "when": "url", "limit_ms": 3000},
        expect="fail", note="URL 不匹配应超时；秒回 ok 说明 url 没参与判断"))
    A(C("web_capture", "①截图到文件", {"kind": "web_shot", "path": "out/mx_shot.png"},
        artifact="out/mx_shot.png", detail=True))
    A(C("web_capture", "②目录 + name", {"kind": "web_shot", "path": "out", "name": "mx2.png"},
        expect="fail", note="name 不在 schema"))
    A(C("web_capture", "②b 目录自动命名", {"kind": "web_shot", "path": "out"}, detail=True))
    A(C("web_capture", "③整页截图", {"kind": "web_shot", "full_page": True},
        artifact="@data", detail=True))
    A(C("web_click", "①点下一页", {"target": "li.next a"}, detail=True))
    A(C("web_click", "②元素库不存在", {"target": "@el:不存在"}, expect="fail"))
    A(C("web_click", "③悬停(action 参数)", {"target": "li.next a", "action": "hover"},
        expect="fail", note="实际参数叫 kind"))
    A(C("web_open", "③回退", {"kind": "back"}))
    A(C("web_click", "③b 悬停(kind)", {"target": "li.next a", "kind": "hover"}))
    A(C("web_open", "②复用 reuse", {"kind": "reuse"}, expect="fail",
        note="kind=reuse 未实现，落到 web_open 缺 url"))
    A(C("web_open", "③刷新", {"kind": "refresh"}))
    A(C("web_open", "⑤空 url", {"url": "", "kind": "new"}, expect="fail"))
    A(C("web_open", "④关闭", {"kind": "close"}))

    # ---------------- 网页：httpbin 表单
    A(C("web_open", "打开表单页", {"url": HTTPBIN_FORM}))
    A(C("web_fill", "①输入框", {"target": "input[name=custname]", "value": "测试"}, detail=True))
    A(C("web_fill", "②按标签批量", {"kind": "form", "pairs": [
        {"label": "Customer name", "value": "李四"}]}, detail=True))
    A(C("web_fill", "③目标不存在", {"target": "#不存在_的元素", "value": "x", "limit_ms": 3000},
        expect="fail"))
    A(C("web_wait", "④state 是否生效", {"target": "input[name=custname]", "state": "hidden", "limit_ms": 2000},
        expect="fail", note="元素是可见的，state=hidden 应当超时；秒回 ok 说明 state 被忽略"))

    # ---------------- 网络监听（要有真实请求才能命中）
    A(C("web_network", "①start 开始监听", {"action": "start"}, detail=True))
    A(C("web_network", "①b listen(旧枚举)", {"action": "listen"}, expect="fail",
        note="schema 只有 start/wait/stop"))
    A(C("web_click", "提交表单", {"target": "button", "kind": "left"}))
    A(C("web_network", "②等 /post", {"action": "wait", "pattern": "/post", "limit_ms": 8000}, detail=True))
    A(C("web_network", "③stop 取记录", {"action": "stop"}, detail=True))
    A(C("web_network", "③b log(旧枚举)", {"action": "log"}, expect="fail",
        note="schema 只有 start/wait/stop"))
    A(C("web_capture", "整页截图(表单页)", {"kind": "web_shot", "full_page": True},
        artifact="@data", detail=True))

    # ---------------- 桌面 / 媒体 / AI（预期缺库）
    A(C("desktop_act", "①抓表格", {"kind": "table"}, expect="fail", detail=True))
    A(C("desktop_act", "②读焦点控件", {"kind": "focused"}, expect="fail"))
    A(C("media_dl", "①下载视频", {"url": "https://example.com/v.mp4"}, expect="fail", detail=True))
    A(C("ai_call", "①对话", {"prompt": "你好"}, expect="fail", detail=True))

    # ---------------- 数据 / 文件
    A(C("data_ops", "①excel_write(旧名)", {"kind": "excel_write", "path": "out/mx.xlsx",
        "rows": [{"a": 1, "b": 2}]}, expect="fail", note="实际参数 action=data_sheet_write"))
    A(C("data_ops", "①b data_sheet_write", {"action": "data_sheet_write", "path": "out/mx.xlsx",
        "rows": [{"a": 1, "b": 2}]}, artifact="out/mx.xlsx", detail=True))
    A(C("data_ops", "②sheet_read", {"action": "data_sheet_read", "path": "out/mx.xlsx"}, detail=True))
    A(C("data_ops", "③读不存在的表", {"action": "data_sheet_read", "path": "out/不存在.xlsx"}, expect="fail"))
    A(C("data_ops", "④sheet_write 无 rows", {"action": "data_sheet_write", "path": "out/mx_no_rows.xlsx"},
        expect="fail", detail=True))

    A(C("file_ops", "①write(旧名 kind)", {"kind": "write", "path": "out/mx.txt", "content": "hi"},
        expect="fail", note="实际参数 action"))
    A(C("file_ops", "①b 写入", {"action": "write", "path": "out/mx.txt", "content": "hi"},
        artifact="out/mx.txt"))
    A(C("file_ops", "②读取", {"action": "read", "path": "out/mx.txt"}, detail=True))
    A(C("file_ops", "③存在", {"action": "exists", "path": "out/mx.txt"}, detail=True))
    A(C("file_ops", "④列表", {"action": "list", "path": "out"}, detail=True))
    A(C("file_ops", "⑤读系统 hosts（越界）", {"action": "read",
        "path": "C:/Windows/System32/drivers/etc/hosts"}, detail=True))
    A(C("file_ops", "⑥读不存在", {"action": "read", "path": "out/不存在.txt"}, expect="fail"))

    # ---------------- HTTP / 脚本 / 通知
    A(C("net_http", "①GET", {"url": "https://httpbin.org/get"}, detail=True))
    A(C("net_http", "②json(旧名)", {"url": "https://httpbin.org/post", "method": "POST",
        "json": {"a": 1}}, expect="fail", note="实际参数 json_body"))
    A(C("net_http", "②b json_body", {"url": "https://httpbin.org/post", "method": "POST",
        "json_body": {"a": 1}}, detail=True))
    A(C("net_http", "③404", {"url": "https://httpbin.org/status/404"}, expect="fail"))
    A(C("script_run", "①python(旧参数)", {"code": "print(1+1)", "kind": "python"}, expect="fail",
        note="没有 kind 分流，统一用 code"))
    A(C("script_run", "①b python", {"code": "print(1+1)"}, detail=True))
    A(C("script_run", "②shell(旧参数)", {"code": "echo hi", "kind": "shell"}, expect="fail",
        note="shell 要用 cmd，不是 kind"))
    A(C("script_run", "②b shell", {"cmd": "echo hi"}, detail=True))
    A(C("script_run", "③语法错误", {"code": "this is invalid python"}, expect="fail"))
    A(C("script_run", "④空参数", {}, expect="fail", detail=True))
    A(C("notify", "①text(旧名)", {"text": "测试"}, expect="fail", note="实际参数 message"))
    A(C("notify", "②带 url", {"text": "测试", "url": "https://httpbin.org/post"}, expect="fail"))
    A(C("notify", "③message 正式名", {"message": "测试", "url": "https://httpbin.org/post"},
        detail=True))
    A(C("notify", "④缺 url", {"message": "测试"}, expect="fail", detail=True))

    # ---------------- 流程
    A(C("flow_read", "①列表", {}, detail=True))
    A(C("flow_read", "②读 demo", {"flow": "demo"}, detail=True))
    A(C("flow_read", "③不存在", {"flow": "mx_不存在"}, expect="fail"))
    A(C("flow_save", "①旧参数 name/flow", {"name": "mx_test", "flow": MX_FLOW}, expect="fail",
        note="实际参数 flow/content"))
    A(C("flow_save", "①b 新建 mx_test", {"flow": "mx_test", "content": MX_FLOW}, detail=True))
    A(C("flow_save", "②同名覆盖", {"flow": "mx_test", "content": MX_FLOW}))
    A(C("flow_save", "③空图", {"flow": "mx_bad", "content": {"name": "坏", "nodes": [], "edges": []}},
        expect="fail", note="空图应被校验拒绝"))
    A(C("flow_save", "④缺 name", {"flow": "mx_noname", "content": {"nodes": []}}, expect="fail"))
    A(C("flow_patch", "①dry_run 看 diff", {"flow": "mx_test", "node": "n1",
        "args": {"name": "x", "value": 2}, "reason": "矩阵测试", "dry_run": True}, detail=True))
    A(C("flow_patch", "②真改", {"flow": "mx_test", "node": "n1",
        "args": {"name": "x", "value": 2}, "reason": "矩阵测试", "dry_run": False}, detail=True))
    A(C("flow_patch", "③节点不存在", {"flow": "mx_test", "node": "mx_无此节点",
        "args": {}, "reason": "x"}, expect="fail"))
    A(C("flow_patch", "④缺 reason", {"flow": "mx_test", "node": "n1", "args": {}}, expect="fail"))
    A(C("flow_run", "①demo", {"flow": "demo"}, detail=True))
    A(C("flow_run", "②mx_test", {"flow": "mx_test"}, detail=True))
    A(C("flow_run", "③带 vars", {"flow": "mx_test", "vars": {"y": 2}}, detail=True))
    A(C("flow_run", "④不存在", {"flow": "mx_不存在"}, expect="fail"))
    A(C("flow_run", "⑤空图可跑", {"flow": "mx_bad"}, expect="any", detail=True))
    A(C("run_status", "①默认", {}, detail=True))
    A(C("run_status", "②limit=5", {"limit": 5}, detail=True))
    A(C("run_status", "③limit=0", {"limit": 0}, expect="any", detail=True))
    A(C("run_status", "④limit=-1", {"limit": -1}, expect="any", detail=True))
    return out


# ---------------------------------------------------------------- 判定与输出


def verdict(case: dict, r: dict) -> str:
    if r["status"] in ("crash", "timeout"):
        return "CRASH" if r["status"] == "crash" else "TIMEOUT"
    if r["status"] == "badjson":
        return "BAD-JSON"
    expect = case["expect"]
    if expect == "any":
        return "INFO"
    if expect == "ok":
        return "PASS" if r["ok"] else "UNEXPECTED-FAIL"
    return "EXP-FAIL" if not r["ok"] else "UNEXPECTED-OK"


def png_size(p: Path) -> str:
    try:
        b = p.read_bytes()[:24]
        if b[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", b[16:24])
            return f"{w}x{h}"
    except Exception:  # noqa: BLE001
        pass
    return "-"


def dw(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def pad(s: str, w: int) -> str:
    s = s.replace("\n", " ")
    return s + " " * max(0, w - dw(s))


def clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def schema_notes(tool: str, args: dict, schemas: dict) -> str:
    """参数名对不上 schema 的，先在表里标出来（不阻断调用）。"""
    sch = schemas.get(tool) or {}
    props = set((sch.get("properties") or {}).keys())
    req = set(sch.get("required") or [])
    extra = [k for k in args if k not in props]
    miss = [k for k in req if k not in args]
    bits = []
    if extra:
        bits.append("多余:" + ",".join(extra))
    if miss:
        bits.append("缺必填:" + ",".join(miss))
    return " ".join(bits)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    only = ""
    if "-k" in sys.argv:
        only = sys.argv[sys.argv.index("-k") + 1]
    todo = [c for c in cases() if not only or only in c["tool"]]

    mcp = MCP()
    rows: list[dict] = []
    print(f"MCP 就绪：{len(mcp.schemas)} 个工具；用例 {len(todo)} 组\n")
    try:
        for i, case in enumerate(todo, 1):
            r = mcp.call(case["tool"], case["args"])
            v = verdict(case, r)
            art = ""
            if case["artifact"]:
                raw_path = case["artifact"]
                if raw_path == "@data":
                    data = (r["payload"] or {}).get("data") if isinstance(r["payload"], dict) else None
                    raw_path = str(data or "")
                    art = f"path={clip(raw_path, 40)}" if raw_path else "path=?"
                p = ROOT / raw_path if raw_path and not Path(raw_path).is_absolute() else Path(raw_path or ".")
                if raw_path and p.is_file():
                    art += f" exists {p.stat().st_size}B"
                    if p.suffix == ".png":
                        art += f" {png_size(p)}"
                elif raw_path:
                    art += " MISSING"
            row = {
                "tool": case["tool"], "label": case["label"], "args": case["args"],
                "expect": case["expect"], "verdict": v, "ms": r["ms"],
                "message": r["message"], "status": r["status"], "ok": r["ok"], "artifact": art,
                "schema": schema_notes(case["tool"], case["args"], mcp.schemas),
                "note": case["note"], "detail": case["detail"], "payload": r["payload"],
                "out_path": (r["payload"] or {}).get("data") if isinstance(r["payload"], dict) else None,
            }
            rows.append(row)
            print(f"  [{i:>2}/{len(todo)}] {v:<16} {case['tool']:<14} {case['label']:<22} "
                  f"{r['ms']:>7.0f}ms  {clip(r['message'], 60)}")
    finally:
        mcp.stop()

    # ---- 汇总表
    print("\n" + "=" * 118)
    print("汇总表")
    print("=" * 118)
    cols = ("工具", "参数组", "结果", "耗时", "错误摘要")
    widths = [16, 30, 16, 8, 44]
    print("| " + " | ".join(pad(c, w) for c, w in zip(cols, widths)) + " |")
    print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for r in rows:
        print("| " + " | ".join([
            pad(r["tool"], widths[0]), pad(r["label"], widths[1]),
            pad(r["verdict"], widths[2]), pad(f"{r['ms']:.0f}ms", widths[3]),
            pad(clip(r["message"], 44), widths[4]),
        ]) + " |")

    # ---- 统计
    total = len(rows)
    exp_fail = sum(1 for r in rows if r["verdict"] == "EXP-FAIL")
    passed = sum(1 for r in rows if r["verdict"] in ("PASS", "EXP-FAIL", "INFO"))
    failed = sum(1 for r in rows if r["verdict"] in ("UNEXPECTED-FAIL", "UNEXPECTED-OK", "CRASH", "TIMEOUT", "BAD-JSON"))
    print("\n统计")
    print(f"  用例总数    {total}")
    print(f"  通过        {passed}  （其中预期失败 {exp_fail} 条，失败形态符合预期）")
    print(f"  失败        {failed}  → 意外失败 {sum(1 for r in rows if r['verdict']=='UNEXPECTED-FAIL')} / "
          f"意外成功 {sum(1 for r in rows if r['verdict']=='UNEXPECTED-OK')} / "
          f"崩溃 {sum(1 for r in rows if r['verdict']=='CRASH')} / "
          f"超时 {sum(1 for r in rows if r['verdict']=='TIMEOUT')}")
    print(f"  进程重启    {mcp.restarts} 次")

    # ---- 问题线索
    print("\n问题线索")
    bad_ok = [r for r in rows if r["verdict"] == "UNEXPECTED-OK"]
    bad_fail = [r for r in rows if r["verdict"] == "UNEXPECTED-FAIL"]
    crash = [r for r in rows if r["verdict"] in ("CRASH", "TIMEOUT", "BAD-JSON")]
    useless = [r for r in rows if r["status"] == "ok" and not r["ok"]
               and len(str(r["message"]).strip()) <= 8]
    scheme = [r for r in rows if r["schema"]]
    missart = [r for r in rows if "MISSING" in r["artifact"]]

    if crash:
        print("  🔴 崩溃/超时：")
        for r in crash:
            print(f"     {r['tool']} {r['label']} → {r['verdict']} {clip(r['message'], 70)}")
    if bad_ok:
        print("  🔴 静默成功（本该报错却 ok:true）：")
        for r in bad_ok:
            print(f"     {r['tool']} {r['label']}  args={clip(json.dumps(r['args'], ensure_ascii=False), 60)}"
                  f"  msg={clip(r['message'], 50)}")
    if bad_fail:
        print("  🟠 意外失败（该成功却失败）：")
        for r in bad_fail:
            print(f"     {r['tool']} {r['label']} → {clip(r['message'], 80)}")
    if useless:
        print("  🟠 错误信息不可用：")
        for r in useless:
            print(f"     {r['tool']} {r['label']} → {clip(r['message'], 60)!r}")
    if scheme:
        print("  🟠 参数名与 schema 不一致（表里已标注）：")
        for r in scheme:
            print(f"     {r['tool']} {r['label']} → {r['schema']}")
    if missart:
        print("  🟡 产物缺失：")
        for r in missart:
            print(f"     {r['tool']} {r['label']} → {r['artifact']}")

    # ---- 参数是否真的生效（同一进程内可对比的证据）
    print("\n参数生效性")
    shots = [r for r in rows if r["tool"] == "web_capture" and isinstance(r["out_path"], str)
             and str(r["out_path"]).endswith(".png")]
    base = next((r for r in shots if r["label"].startswith("①")), None)
    full = next((r for r in shots if r["label"].startswith("③")), None)
    if base and full:
        h1 = png_size(Path(base["out_path"]))
        h2 = png_size(Path(full["out_path"]))
        same = h1 == h2
        print(f"  截图 full_page：普通 {h1} / 整页 {h2} "
              f"→ {'⚠ 同尺寸，full_page 参数没生效' if same else '整页确实变长'}")

    runs = [r for r in rows if r["tool"] == "flow_run" and r["ok"]]
    ids = [(r["payload"] or {}).get("run_id") for r in runs if isinstance(r["payload"], dict)]
    uniq = len({i for i in ids if i})
    print(f"  flow_run run_id：{len(ids)} 次运行 / {uniq} 个不同 run_id"
          f"{'  ⚠ 秒级时间戳撞号，执行记录被覆盖' if uniq < len(ids) else ''}")

    # ---- 细节（判断生效/不生效用）
    print("\n关键返回细节")
    for r in rows:
        if not (r["detail"] or r["artifact"]):
            continue
        body = json.dumps(r["payload"], ensure_ascii=False)
        print(f"  {r['tool']:<14} {r['label']:<20} {r['artifact']:<34} {clip(body, 130)}")

    # ---- 落盘
    dump = [{k: v for k, v in r.items() if k != "payload"} | {"payload": _brief(r["payload"])}
            for r in rows]
    (OUT / "mx_matrix.json").write_text(
        json.dumps({"total": total, "passed": passed, "failed": failed,
                    "restarts": mcp.restarts, "rows": dump},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {OUT / 'mx_matrix.json'}")
    return 0


def _brief(payload: object, limit: int = 1200) -> object:
    """返回体太长就截断，避免 JSON 变成几 MB。"""
    if payload is None:
        return None
    s = json.dumps(payload, ensure_ascii=False)
    if len(s) <= limit:
        return payload
    return {"_truncated": s[:limit] + "…"}


if __name__ == "__main__":
    raise SystemExit(main())
