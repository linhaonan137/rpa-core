import asyncio
import json
import sys
import traceback
from pathlib import Path

# 项目根 = 本文件的上两级（tests/ 的父目录）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nodes

nodes.load_all()

from engine.context import ExecutionContext
from engine.schema import Flow
from engine.runner import Runner

CASES = [
    ("real_1_quotes", "quotes.toscrape.com", ["page1", "page2", "page2_meta"]),
    ("real_2_saucedemo", "www.saucedemo.com", ["inv", "cart"]),
    ("real_3_httpbin", "httpbin.org", ["resp"]),
    ("real_4_demoqa", "demoqa.com", ["output"]),
]


async def run_one(name: str, vars_wanted: list[str]) -> dict:
    raw = json.loads((ROOT / "flows" / f"{name}.json").read_text(encoding="utf-8"))
    flow = Flow(**raw)
    ctx = ExecutionContext()
    out = {"flow": name, "ok": False, "nodes": 0, "passed": 0, "failed": [], "ms": 0, "data": {}, "error": ""}
    try:
        r = await Runner(flow, ctx).execute()
        out["ok"] = r["ok"]
        out["ms"] = r["ms"]
        out["nodes"] = len(r["nodes"])
        out["passed"] = sum(1 for x in r["nodes"] if x["ok"])
        out["failed"] = [f'{x["node_id"]}:{x["type"]} -> {(x["error"] or "")[:90]}' for x in r["nodes"] if not x["ok"]]
        for k in vars_wanted:
            if ctx.has_var(k):
                v = ctx.get_var(k)
                out["data"][k] = v if not isinstance(v, list) else f"[{len(v)} 项] {str(v[:1])[:120]}"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-400:]
    return out


async def main():
    results = []
    for name, site, vars_wanted in CASES:
        print(f"\n{'='*70}\n▶ {site}  ({name})")
        r = await run_one(name, vars_wanted)
        results.append(r)
        print(f"  结果: {'PASS' if r['ok'] else 'FAIL'}  节点 {r['passed']}/{r['nodes']}  {r['ms']}ms")
        for f in r["failed"]:
            print(f"    ✗ {f}")
        if r["error"]:
            print(f"    异常: {r['error']}")
        for k, v in r["data"].items():
            print(f"    {k}: {str(v)[:160]}")

    print(f"\n{'='*70}\n汇总\n{'='*70}")
    print(f"{'站点':<28}{'结果':<8}{'节点':<10}{'耗时':<10}失败")
    for r in results:
        print(f"{r['flow']:<28}{'PASS' if r['ok'] else 'FAIL':<8}{str(r['passed'])+'/'+str(r['nodes']):<10}{str(round(r['ms']))+'ms':<10}{len(r['failed'])}")
    ok = sum(1 for r in results if r["ok"])
    print(f"\n通过 {ok}/{len(results)}")

    result_path = ROOT / "out" / "real_site_results.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("明细已存 out/real_site_results.json")


asyncio.run(main())
