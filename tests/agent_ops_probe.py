"""13 个非网页工具的全参数实测探针。

用法:
    python tests/agent_ops_probe.py list      # 只 dump schema
    python tests/agent_ops_probe.py run       # 跑全部用例，写 out/agent_ops_raw.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
MAIN = str(ROOT / "main.py")

env = dict(os.environ)
env.update({
    "RPA_FLOWS": str(ROOT / "flows"),
    "RPA_DB": str(ROOT / "rpa.db"),
    "RPA_ELEMENTS": str(ROOT / "elements.json"),
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
})


class MCP:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [PY, MAIN, "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env, cwd=r"C:\Windows",
        )
        self.errs: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()
        self._id = 0
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "agent-ops", "version": "1.0"}}})
        self._readline(30)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _drain(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.errs.append(line.rstrip())

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _readline(self, timeout: float) -> str:
        box: dict[str, str] = {}

        def _r() -> None:
            try:
                assert self.proc.stdout is not None
                box["l"] = self.proc.stdout.readline()
            except Exception as exc:  # noqa: BLE001
                box["l"] = ""
                box["e"] = str(exc)

        t = threading.Thread(target=_r, daemon=True)
        t.start()
        t.join(timeout)
        return box.get("l", "")

    def rpc(self, method: str, params: dict | None = None, timeout: float = 70) -> dict:
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        line = self._readline(timeout)
        if not line.strip():
            return {"_timeout": True, "_stderr": self.errs[-6:]}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"_badjson": line[:400]}

    def tools(self) -> list[dict]:
        r = self.rpc("tools/list")
        return (r.get("result") or {}).get("tools") or []

    def call(self, tool: str, args: dict, timeout: float = 70) -> dict:
        r = self.rpc("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
        if "_timeout" in r:
            return {"_timeout": True, "stderr": r.get("_stderr")}
        res = r.get("result") or {}
        if r.get("error"):
            return {"_rpc_error": r["error"]}
        content = res.get("content") or []
        txt = ""
        for c in content:
            if c.get("type") == "text":
                txt += c.get("text") or ""
        try:
            return json.loads(txt) if txt else {"_empty": True, "isError": res.get("isError")}
        except json.JSONDecodeError:
            return {"_text": txt[:800], "isError": res.get("isError")}

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def dump_schema() -> None:
    m = MCP()
    try:
        tools = m.tools()
        print(f"{len(tools)} tools")
        out = {}
        for t in tools:
            props = (t.get("inputSchema") or {}).get("properties") or {}
            req = (t.get("inputSchema") or {}).get("required") or []
            out[t["name"]] = {"props": {k: (v.get("type") or v.get("anyOf") or "?") for k, v in props.items()},
                              "required": req}
        (ROOT / "out" / "agent_ops_schema.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        for k, v in out.items():
            print(f"  {k:<14} req={v['required']} props={list(v['props'])}")
    finally:
        m.kill()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        dump_schema()
