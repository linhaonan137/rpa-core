import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# 项目根 = 本文件的上两级（tests/ 的父目录）
ROOT = Path(__file__).resolve().parents[1]

PY = sys.executable                      # 用当前解释器，别硬编码 venv 路径
MAIN = str(ROOT / "main.py")

env = dict(os.environ)
env.update({
    "RPA_FLOWS": str(ROOT / "flows"),
    "RPA_DB": str(ROOT / "rpa.db"),
    "RPA_ELEMENTS": str(ROOT / "elements.json"),
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
})

proc = subprocess.Popen(
    [PY, MAIN, "mcp"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1, env=env, cwd=r"C:\Windows",
)

# stderr 单独读，避免管道阻塞
err_lines = []
def _drain():
    for line in proc.stderr:
        err_lines.append(line.rstrip())

threading.Thread(target=_drain, daemon=True).start()


def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv(timeout=15):
    """读一行 JSON（MCP stdio 用 JSON Lines）。"""
    result = {}

    def _r():
        try:
            result["line"] = proc.stdout.readline()
        except Exception as exc:  # noqa: BLE001
            result["err"] = str(exc)

    t = threading.Thread(target=_r, daemon=True)
    t.start()
    t.join(timeout)
    return result.get("line", "")


print("1) initialize")
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "probe", "version": "1.0"}}})
line = recv()
if not line.strip():
    print("   ✗ 没响应")
    print("   stderr:", "\n".join(err_lines[-8:]))
    proc.kill()
    sys.exit(1)
init = json.loads(line)
info = init.get("result", {}).get("serverInfo", {})
print("   ✅ serverInfo:", info)

print("2) notifications/initialized")
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

print("3) tools/list")
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
line = recv()
if not line.strip():
    print("   ✗ 没响应", "\n".join(err_lines[-8:]))
    proc.kill()
    sys.exit(1)
res = json.loads(line)
tools = res.get("result", {}).get("tools", [])
print(f"   ✅ {len(tools)} 个工具")
for t in tools:
    print(f"      - {t['name']}")

# 抽一个工具看参数 schema 是否正常
if tools:
    sample = next((t for t in tools if t["name"] == "flow_read"), tools[0])
    props = list((sample.get("inputSchema") or {}).get("properties", {}).keys())
    print(f"   示例 {sample['name']} 参数: {props}")

proc.kill()
print("4) 关闭")
