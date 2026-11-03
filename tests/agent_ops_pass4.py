"""第四轮：vars 注入真伪、limit_steps 嵌套计数、失败传播。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_ops_probe import MCP, ROOT  # noqa: E402

LOG: list[dict] = []


def show(m: MCP, cid: str, tool: str, args: dict, timeout: float = 70) -> dict:
    t0 = time.time()
    r = m.call(tool, args, timeout=timeout)
    LOG.append({"id": cid, "ms": round((time.time() - t0) * 1000), "tool": tool,
                "args": args, "result": r})
    print(f"{cid:<5} {tool:<11} {json.dumps(r, ensure_ascii=False)[:280]}", flush=True)
    return r


VARS_FLOW = {
    "name": "vars 探针",
    "vars": [{"name": "v1", "value": "流程默认"}],
    "nodes": [
        {"id": "n1", "type": "sys_cmd",
         "args": {"cmd": "echo {v1}>{v1}-{v2} > out/tmpb_var.txt"}},
        {"id": "n2", "type": "fail", "args": {"error": "故意失败"}},
    ],
    "edges": [{"id": "e1", "from": "n1", "to": "n2"}],
}

LOOP_FLOW = {
    "name": "步数探针",
    "nodes": [
        {"id": "l1", "type": "loop", "args": {"kind": "count", "count": 5}},
        {"id": "b1", "type": "noop", "args": {}},
    ],
    "edges": [
        {"id": "e1", "from": "l1", "to": "b1", "exit": "loop"},
        {"id": "e2", "from": "b1", "to": "l1"},
    ],
}


def main() -> None:
    m = MCP()
    try:
        print("--- vars 注入 + 失败传播 ---")
        show(m, "Y01", "flow_save", {"flow": "tmpa_vars", "content": VARS_FLOW})
        r = show(m, "Y02", "flow_run", {"flow": "tmpa_vars", "vars": {"v1": "外部A", "v2": "外部B"}})
        print("    ok=", r.get("ok"), "failed=", r.get("failed"))
        show(m, "Y03", "file_ops", {"action": "read", "path": "out/tmpb_var.txt"})
        r = show(m, "Y04", "flow_run", {"flow": "tmpa_vars"})
        print("    不给 vars（应回落到流程默认 流程默认-{v2}）ok=", r.get("ok"), "failed=", r.get("failed"))
        show(m, "Y05", "file_ops", {"action": "read", "path": "out/tmpb_var.txt"})

        print("--- limit_steps 在循环里的实际步数 ---")
        show(m, "Y06", "flow_save", {"flow": "tmpa_loop", "content": LOOP_FLOW})
        for lim in (1, 2, 3, 6, 100):
            r = show(m, "Y07", "flow_run", {"flow": "tmpa_loop", "limit_steps": lim})
            print(f"    limit_steps={lim} → ok={r.get('ok')} 节点记录数={len(r.get('nodes') or [])}")

        print("--- 清理 ---")
        show(m, "C1", "file_ops", {"action": "delete", "path": "flows/tmpa_vars.json"})
        show(m, "C2", "file_ops", {"action": "delete", "path": "flows/tmpa_loop.json"})
        show(m, "C3", "file_ops", {"action": "delete", "path": "out/tmpb_var.txt"})
    finally:
        m.kill()
    (ROOT / "out" / "agent_ops_pass4.json").write_text(
        json.dumps(LOG, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
