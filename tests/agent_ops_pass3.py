"""第三轮：desktop_act 真机验证（已装 requirements 声明的 uiautomation）+ 其余补测。"""
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
    ms = round((time.time() - t0) * 1000)
    LOG.append({"id": cid, "ms": ms, "tool": tool, "args": args, "result": r})
    print(f"{cid:<6} {tool:<12} {ms:>6}ms {json.dumps(r, ensure_ascii=False)[:260]}", flush=True)
    return r


def main() -> None:
    m = MCP()
    try:
        print("--- desktop_act（uiautomation 已就绪）---")
        show(m, "K11", "desktop_act", {"kind": "table", "control": "NoSuchControlXYZ"}, timeout=90)
        show(m, "K12", "desktop_act", {"kind": "table"}, timeout=90)
        show(m, "K13", "desktop_act", {"kind": "select_text", "control": "NoSuchControlXYZ"}, timeout=90)
        show(m, "K14", "desktop_act", {"kind": "focused"}, timeout=90)
        show(m, "K15", "desktop_act", {"kind": "table", "control": "NoSuchControlXYZ", "limit": 3}, timeout=90)

        print("--- net_http 参数是否真的送到对端 ---")
        r = show(m, "H21", "net_http", {"url": "https://httpbin.org/post", "method": "POST",
                                            "json_body": {"probe": "v1"}, "headers": {"X-Probe": "yes"}})
        body = (r.get("data") or {}).get("body") or {}
        print("    echo json:", body.get("json"), " echo header X-Probe:",
              (body.get("headers") or {}).get("X-Probe"))
        r = show(m, "H22", "net_http", {"url": "https://httpbin.org/get?x=1"})
        print("    echo args:", ((r.get("data") or {}).get("body") or {}).get("args"))

        print("--- notify 各通道 body 格式是否真的不同 ---")
        for ch in ("webhook", "dingtalk", "wecom", "feishu"):
            show(m, f"N1{ch[:2]}", "notify", {"message": "probe", "channel": ch,
                                              "url": "https://httpbin.org/post"})

        print("--- flow_patch dry_run 是否真的不落盘 ---")
        show(m, "P21", "flow_save", {"flow": "tmpa_dry",
                                     "content": {"name": "dry", "nodes": [{"id": "n1", "type": "noop", "args": {"x": 0}}]}})
        show(m, "P22", "flow_patch", {"flow": "tmpa_dry", "node": "n1", "args": {"x": 99},
                                      "reason": "试着看看", "dry_run": True})
        show(m, "P23", "flow_read", {"flow": "tmpa_dry"})
        show(m, "P24", "flow_patch", {"flow": "tmpa_dry", "node": "n1", "args": {"x": 99},
                                      "reason": "真的改", "dry_run": False})
        show(m, "P25", "flow_read", {"flow": "tmpa_dry"})

        print("--- flow_patch 合法 expect ---")
        show(m, "P26", "flow_patch", {"flow": "tmpa_dry", "node": "n1",
                                      "expect": {"text": "x"}, "reason": "合法 expect", "dry_run": False})
        show(m, "P27", "flow_run", {"flow": "tmpa_dry"})

        print("--- flow_save 覆盖已存在流程 ---")
        show(m, "P28", "flow_save", {"flow": "tmpa_dry",
                                     "content": {"name": "dry2", "nodes": [{"id": "m1", "type": "noop"}]}})
        show(m, "P29", "flow_read", {"flow": "tmpa_dry"})

        print("--- 清理 ---")
        show(m, "C1", "file_ops", {"action": "delete", "path": "flows/tmpa_dry.json"})
        show(m, "C2", "file_ops", {"action": "delete", "path": "flows/_meta/tmpa_dry.changes.jsonl"})
        show(m, "C3", "file_ops", {"action": "delete", "path": "flows/.versions/tmpa_dry.snap-0001.json"})
    finally:
        m.kill()
    (ROOT / "out" / "agent_ops_pass3.json").write_text(
        json.dumps(LOG, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
