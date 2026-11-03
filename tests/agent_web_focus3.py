"""第三轮：把右键/中键的场景用全新页面各自跑一次，避免互相污染；顺带测 radio。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_matrix import MCP, OUT  # noqa: E402

QUOTES = "https://quotes.toscrape.com/"
FORM = "https://httpbin.org/forms/post"
rows: list[dict] = []


def run(m: MCP, label: str, tool: str, args: dict) -> dict:
    r = m.call(tool, args)
    rows.append({"label": label, "tool": tool, "args": args, "ok": r["ok"], "ms": round(r["ms"]),
                 "message": str(r["message"])[:400], "payload": r["payload"]})
    print(f"    {label:<32} {'ok' if r['ok'] else 'FAIL':<5} {r['ms']:>7.0f}ms  {str(r['message'])[:80]}")
    return r


def scene(name, fn):
    print(f"\n===== {name} =====")
    m = MCP()
    try:
        fn(m)
    finally:
        m.stop()


def u1(m: MCP) -> None:
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "中键 li.next a（全新页）", "web_click", {"target": "li.next a", "kind": "middle", "limit_ms": 10000})
    run(m, "读 href", "web_read", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000})


def u2(m: MCP) -> None:
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "右键 li.next a（全新页）", "web_click", {"target": "li.next a", "kind": "right", "limit_ms": 10000})
    run(m, "读 href", "web_read", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000})


def u3(m: MCP) -> None:
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "可见性探测（立即）", "web_read", {
        "kind": "eval",
        "code": "() => {const e=document.querySelector('li.next'); const r=e.getBoundingClientRect(); "
                "return [getComputedStyle(e).display, getComputedStyle(e).visibility, Math.round(r.width), "
                "Math.round(r.height), window.scrollY].join('|');}", "limit_ms": 5000})
    run(m, "中键 li.next（全新页）", "web_click", {"target": "li.next", "kind": "middle", "limit_ms": 8000})
    run(m, "可见性探测（之后）", "web_read", {
        "kind": "eval",
        "code": "() => {const e=document.querySelector('li.next'); const r=e.getBoundingClientRect(); "
                "return [getComputedStyle(e).display, getComputedStyle(e).visibility, Math.round(r.width), "
                "Math.round(r.height), window.scrollY].join('|');}", "limit_ms": 5000})


def u4(m: MCP) -> None:
    run(m, "打开 forms/post", "web_open", {"url": FORM, "until": "dom", "limit_ms": 30000})
    run(m, "radio 用 kind=checkbox", "web_fill", {"target": "input[value=cheese]", "value": "1",
                                                 "kind": "checkbox", "limit_ms": 8000})
    run(m, "读 radio checked", "web_read", {
        "kind": "eval", "code": "() => document.querySelector('input[value=cheese]').checked", "limit_ms": 5000})
    run(m, "checkbox 用 kind=checkbox（bacon）", "web_fill",
        {"target": "input[name=topping][value=bacon]", "value": "1", "kind": "checkbox", "limit_ms": 8000})
    run(m, "读 bacon checked", "web_read", {
        "kind": "eval", "code": "() => document.querySelector('input[name=topping][value=bacon]').checked",
        "limit_ms": 5000})


def u5(m: MCP) -> None:
    """dblclick 到底触发几次 click：用 addEventListener 精确计数。"""
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "注入精确计数按钮", "web_read", {
        "kind": "eval",
        "code": "() => {document.body.insertAdjacentHTML('afterbegin',"
                "\"<button id='c2' style='padding:20px'>C2</button>\");"
                "window.__c={click:0,dbl:0};"
                "const b=document.querySelector('#c2');"
                "b.addEventListener('click',()=>window.__c.click++);"
                "b.addEventListener('dblclick',()=>window.__c.dbl++); return 'ok';}", "limit_ms": 5000})
    run(m, "kind=double", "web_click", {"target": "#c2", "kind": "double", "limit_ms": 8000})
    run(m, "读计数", "web_read", {"kind": "eval", "code": "() => JSON.stringify(window.__c)", "limit_ms": 5000},
        "期望 click=2 dbl=1")
    run(m, "kind=left 对照", "web_click", {"target": "#c2", "kind": "left", "limit_ms": 8000})
    run(m, "读计数2", "web_read", {"kind": "eval", "code": "() => JSON.stringify(window.__c)", "limit_ms": 5000})


def main() -> int:
    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    for name, fn in [("U1 中键（全新页）", u1), ("U2 右键（全新页）", u2), ("U3 中键 li 容器", u3),
                     ("U4 radio/checkbox", u4), ("U5 dblclick 计数", u5)]:
        if only and only not in name:
            continue
        scene(name, fn)
    (OUT / "agent_web_focus3.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n→ out/agent_web_focus3.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
