"""临时诊断：51 个 MCP 工具谁被引用过（用完即删）"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.mcp import mcp

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".venv", "__pycache__", ".git", ".chrome-debug-profile", "_tmp", "out"}

def collect():
    d = {}
    for p in ROOT.rglob("*"):
        if any(s in p.parts for s in SKIP) or p.is_dir():
            continue
        if p.suffix not in (".py", ".md", ".json", ".jsonl", ".ps1", ".cmd"):
            continue
        try:
            d[p] = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass
    return d

texts = collect()
tools = sorted(t.name for t in asyncio.run(mcp.list_tools()))

rows = []
for t in tools:
    where = {"def": 0, "matrix": 0, "docs": 0, "other": 0}
    for p, txt in texts.items():
        n = txt.count(t)
        if not n:
            continue
        if p.name == "mcp.py":
            # 排除 async def 定义与装饰器所在行，剩下的才是真引用
            cnt = 0
            for line in txt.splitlines():
                if t not in line:
                    continue
                if line.strip().startswith(f"async def {t}(") or line.strip().startswith(f"def {t}("):
                    continue
                cnt += line.count(t)
            where["def"] += cnt
        elif p.name == "mcp_matrix.py":
            where["matrix"] += n
        elif p.suffix == ".md":
            where["docs"] += n
        else:
            where["other"] += n
    total = sum(where.values())
    if where["matrix"]:
        verdict = "已测"
    elif total == 0:
        verdict = "★零引用"
    elif where["docs"] and not where["other"]:
        verdict = "仅文档"
    elif where["other"]:
        verdict = "代码在用"
    else:
        verdict = "仅内部"
    rows.append((t, where["matrix"], where["docs"], where["other"], where["def"], verdict))

rows.sort(key=lambda r: (r[5] != "★零引用", r[1] == 0, r[0]))

print(f"工具总数 {len(tools)}")
print(f"{'工具':<22}{'矩阵':>4}{'文档':>6}{'代码':>6}{'内部':>6}  判定")
print("-" * 62)
for t, m, d, o, i, v in rows:
    print(f"{t:<22}{m:>4}{d:>6}{o:>6}{i:>6}  {v}")

zero = [r[0] for r in rows if r[5] == "★零引用"]
nodoc = [r[0] for r in rows if r[1] == 0 and r[5] != "★零引用"]
print()
print("零引用（连文档都没提）:", zero or "无")
print("矩阵里没有（可能没测过）:", nodoc or "无")
