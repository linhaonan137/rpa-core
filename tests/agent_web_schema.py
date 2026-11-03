"""只干一件事：起 MCP 子进程，把 7 个 web_* 工具的 inputSchema 原样打出来。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_matrix import MCP  # noqa: E402

TOOLS = ["web_open", "web_click", "web_fill", "web_read",
         "web_wait", "web_capture", "web_network"]


def main() -> int:
    m = MCP()
    try:
        print(f"共 {len(m.schemas)} 个工具\n")
        print("工具清单:", ", ".join(sorted(m.schemas)))
        out = {}
        for t in TOOLS:
            sch = m.schemas.get(t)
            if sch is None:
                print(f"\n!! 缺工具 {t}")
                continue
            out[t] = sch
            print("\n" + "=" * 70)
            print(t, "| required:", sch.get("required"))
            for k, v in (sch.get("properties") or {}).items():
                bits = [f"type={v.get('type')}"]
                for key in ("enum", "default", "description", "minimum", "maximum"):
                    if key in v:
                        bits.append(f"{key}={v[key]}")
                print(f"  - {k:<14} " + "  ".join(str(b) for b in bits))
        p = Path(__file__).resolve().parents[1] / "out" / "agent_web_schema.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {p}")
    finally:
        m.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
