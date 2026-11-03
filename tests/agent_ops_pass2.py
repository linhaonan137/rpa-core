"""第二轮：交叉验证（save_as 是否真落变量、flow_patch 是否写出非法流程、
limit_steps/limit 的实际行为、run_id 唯一性、清理）。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_ops_probe import MCP, ROOT  # noqa: E402

OUT = ROOT / "out"
LOG: list[dict] = []


def show(m: MCP, cid: str, tool: str, args: dict, timeout: float = 70) -> dict:
    t0 = time.time()
    r = m.call(tool, args, timeout=timeout)
    ms = round((time.time() - t0) * 1000)
    LOG.append({"id": cid, "tool": tool, "args": args, "ms": ms, "result": r})
    print(f"{cid:<6} {tool:<13} {ms:>6}ms {json.dumps(r, ensure_ascii=False)[:300]}", flush=True)
    return r


def main() -> None:
    m = MCP()
    try:
        print("--- 1) save_as 是否真的写进 ctx 变量 ---")
        show(m, "V01", "data_ops", {"action": "csv_gen", "rows": [{"z": 9}], "save_as": "v_csv"})
        show(m, "V02", "file_ops", {"action": "read", "path": "out/tmpb_a.txt", "save_as": "v_txt"})
        show(m, "V03", "net_http", {"url": "https://httpbin.org/get", "save_as": "v_http"})
        show(m, "V04", "script_run", {"save_as": "v_py", "code": "result = 7"})
        show(m, "V05", "script_run",
             {"code": "result = {k: (type(v).__name__) for k, v in ctx.all_vars().items() if k.startswith('v_')}"},
             timeout=40)

        print("--- 2) flow_patch 写出的非法 expect/on_fail 是否留在文件里 ---")
        show(m, "V06", "flow_read", {"flow": "tmpa_ok"})

        print("--- 3) limit_steps / stopped 实际语义 ---")
        r = show(m, "V07", "flow_run", {"flow": "demo", "limit_steps": 1})
        print("    limit_steps=1 →", {k: r.get(k) for k in ("ok", "ms", "failed")},
              "nodes=", len(r.get("nodes") or []))
        r = show(m, "V08", "flow_run", {"flow": "demo", "limit_steps": 2})
        print("    limit_steps=2 →", {k: r.get(k) for k in ("ok",)}, "nodes=", len(r.get("nodes") or []))

        print("--- 4) run_id 唯一性：同秒连跑 4 次 ---")
        ids = []
        for i in range(4):
            r = show(m, f"V1{i}", "flow_run", {"flow": "demo"})
            ids.append(r.get("run_id"))
        print("    run_ids:", ids, "唯一:", len(set(ids)) == 4)

        print("--- 5) run_status 的 limit 截断实测 ---")
        for lim in (0, -1, 5, 999):
            r = show(m, f"V2{lim}", "run_status", {"limit": lim})
            print(f"    limit={lim} → 返回 {len(r.get('runs') or [])} 条")

        print("--- 6) file_ops delete 不存在的文件（是否静默成功）---")
        show(m, "V30", "file_ops", {"action": "delete", "path": "out/tmpb_never_existed.txt"})

        print("--- 7) data_ops data_csv_read 给 path（文件真实存在）---")
        show(m, "V31", "data_ops", {"action": "data_csv_read", "path": "out/tmpb_d.csv"})
        show(m, "V32", "file_ops", {"action": "read", "path": "out/tmpb_d.csv"})

        print("--- 8) data_ops json_query / list_* 传 key 与 text ---")
        show(m, "V33", "data_ops", {"action": "json_query", "text": '{"a":1}', "key": "a"})
        show(m, "V34", "data_ops", {"action": "seq_order", "text": [3, 1, 2]})
        show(m, "V35", "data_ops", {"action": "seq_order", "rows": [3, 1, 2]})

        print("--- 9) 清理自己造的文件 ---")
        for p in ("out/tmpb_t.xlsx", "out/tmpb_t2.xlsx", "out/tmpb_a.txt", "out/tmpb_c.txt",
                  "out/tmpb_d.csv", "out/tmpb_k.txt", "out/tmpb_empty.txt",
                  "out/tmpb_r.txt", "out/tmpb_m.txt", "out/tmpb_dir/inner.txt",
                  "flows/tmpa_ok.json", "flows/tmpa_escape.json", "tmpa_escape.json"):
            show(m, "C:" + p, "file_ops", {"action": "delete", "path": p})
    finally:
        m.kill()
    (OUT / "agent_ops_pass2.json").write_text(json.dumps(LOG, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
