"""第二轮隔离复测：修掉上一轮脚本自身的干扰（dblclick 前置污染、零尺寸锚点）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_matrix import MCP, OUT  # noqa: E402

QUOTES = "https://quotes.toscrape.com/"
HTTPBIN_HTML = "https://httpbin.org/html"
FORM = "https://httpbin.org/forms/post"
rows: list[dict] = []


def run(m: MCP, label: str, tool: str, args: dict, note: str = "") -> dict:
    r = m.call(tool, args)
    rows.append({"label": label, "tool": tool, "args": args, "ok": r["ok"], "ms": round(r["ms"]),
                 "message": str(r["message"])[:300], "note": note, "payload": r["payload"]})
    print(f"    {label:<30} {'ok' if r['ok'] else 'FAIL':<5} {r['ms']:>7.0f}ms  {str(r['message'])[:70]}")
    print(f"      {json.dumps(r['payload'], ensure_ascii=False)[:220]}")
    return r


def scene(name: str, fn) -> None:
    print(f"\n===== {name} =====")
    m = MCP()
    try:
        fn(m)
    finally:
        m.stop()


def t1_right_middle(m: MCP) -> None:
    """先右键/中键，再双击，避免上一个动作污染。"""
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "注入点击计数器按钮", "web_read", {
        "kind": "eval",
        "code": "() => {document.body.insertAdjacentHTML('afterbegin',"
                "\"<button id='cnt' onclick='window.__n=(window.__n||0)+1'>C</button>\"); "
                "window.__n=0; return 'ok';}", "limit_ms": 5000})
    run(m, "kind=right on button", "web_click", {"target": "#cnt", "kind": "right", "limit_ms": 8000})
    run(m, "点击计数", "web_read", {"kind": "eval", "code": "() => window.__n", "limit_ms": 5000})
    run(m, "kind=middle on button", "web_click", {"target": "#cnt", "kind": "middle", "limit_ms": 8000})
    run(m, "点击计数", "web_read", {"kind": "eval", "code": "() => window.__n", "limit_ms": 5000})
    run(m, "kind=double on button", "web_click", {"target": "#cnt", "kind": "double", "limit_ms": 8000})
    run(m, "点击计数（应为 2）", "web_read", {"kind": "eval", "code": "() => window.__n", "limit_ms": 5000})
    run(m, "kind=left on button", "web_click", {"target": "#cnt", "kind": "left", "limit_ms": 8000})
    run(m, "点击计数", "web_read", {"kind": "eval", "code": "() => window.__n", "limit_ms": 5000})


def t2_right_middle_link(m: MCP) -> None:
    """真实站点的 li.next：先右键，再中键，最后双击，逐步看地址。"""
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "kind=right on li.next", "web_click", {"target": "li.next", "kind": "right", "limit_ms": 10000})
    run(m, "读 href", "web_read", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000})
    run(m, "kind=middle on li.next", "web_click", {"target": "li.next", "kind": "middle", "limit_ms": 10000})
    run(m, "读 href", "web_read", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000})


def t3_download(m: MCP) -> None:
    run(m, "打开 httpbin/html", "web_open", {"url": HTTPBIN_HTML, "until": "dom", "limit_ms": 30000})
    run(m, "注入带文本的下载锚点", "web_read", {
        "kind": "eval",
        "code": "() => {const a=document.createElement('a'); a.id='dl3'; a.href='/bytes/1024'; "
                "a.download='agent_web_dl3.bin'; a.textContent='download-me'; "
                "document.body.prepend(a); return 'ok';}", "limit_ms": 5000})
    run(m, "capture download", "web_capture", {"kind": "download", "target": "#dl3", "path": "out",
                                               "name": "agent_web_dl3.bin"})
    p = OUT / "agent_web_dl3.bin"
    print(f"      文件: {p.stat().st_size if p.is_file() else 'MISSING'}B（应为 1024）")
    run(m, "capture download 指定目录+name 覆盖", "web_capture",
        {"kind": "download", "target": "#dl3", "path": "out/agent_web_dlsub", "name": "dl4.bin"})
    p2 = OUT / "agent_web_dlsub" / "dl4.bin"
    print(f"      文件: {p2.stat().st_size if p2.is_file() else 'MISSING'}B")


def t4_newtab(m: MCP) -> None:
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "注入 window.open 按钮", "web_read", {
        "kind": "eval",
        "code": "() => {document.body.insertAdjacentHTML('afterbegin',"
                "\"<button id='op' onclick=\\\"window.open('https://quotes.toscrape.com/page/4/','_blank')\\\">o</button>\");"
                " return 'ok';}", "limit_ms": 5000})
    run(m, "follow_new_tab=true（window.open）", "web_click", {"target": "#op", "follow_new_tab": True,
                                                              "limit_ms": 15000})
    run(m, "读 href", "web_read", {"kind": "eval", "code": "() => location.href", "limit_ms": 5000})
    run(m, "关掉当前页", "web_open", {"kind": "close"})
    run(m, "关页后读 href", "web_read", {"kind": "eval", "code": "() => location.href", "limit_ms": 5000},
        "能读到说明确有第二个标签页")


def t5_form_submit(m: MCP) -> None:
    """httpbin 表单：textarea / radio / checkbox 的 fill 通路（真实控件）。"""
    run(m, "打开 forms/post", "web_open", {"url": FORM, "until": "dom", "limit_ms": 30000})
    run(m, "填 textarea", "web_fill", {"target": "textarea[name=comments]", "value": "多行\n文本", "limit_ms": 8000})
    run(m, "读 textarea value", "web_read", {"target": "textarea[name=comments]", "kind": "value", "limit_ms": 5000})
    run(m, "填 radio（value 需为 selected）", "web_fill", {"target": "input[value=cheese]", "value": "1",
                                                          "limit_ms": 8000})
    run(m, "读 checked", "web_read", {"kind": "eval", "code": "() => document.querySelector('input[value=cheese]').checked",
                                      "limit_ms": 5000})
    run(m, "填 checkbox（fill 语义）", "web_fill", {"target": "input[name=topping][value=bacon]", "value": "1",
                                                   "limit_ms": 8000})
    run(m, "提交表单", "web_click", {"target": "button", "limit_ms": 10000})
    run(m, "等 /post", "web_wait", {"target": "/post", "when": "url", "limit_ms": 15000})
    run(m, "读 body 文本", "web_read", {"kind": "eval", "code": "() => document.body.innerText.slice(0,200)",
                                        "limit_ms": 5000})


def t6_positional(m: MCP) -> None:
    """web_read 的 xpath= / css= / id= 写法 + 裸标签名 + 中文文本。"""
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "xpath= 写法", "web_read", {"target": "xpath=//li[@class='next']/a", "kind": "attr", "attr": "href",
                                      "limit_ms": 8000})
    run(m, "css= 写法", "web_read", {"target": "css=div.quote span.text", "kind": "text", "limit_ms": 8000})
    run(m, "裸标签名 span", "web_read", {"target": "span", "kind": "text", "limit_ms": 8000})
    run(m, "中文纯文本 target", "web_read", {"target": "Next", "kind": "text", "limit_ms": 8000})
    run(m, "role= 写法", "web_read", {"target": "role=link[name='Next']", "kind": "text", "limit_ms": 8000})


def main() -> int:
    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    scenes = [
        ("T1 右键/中键/双击（注入按钮）", t1_right_middle),
        ("T2 右键/中键（真实 li.next）", t2_right_middle_link),
        ("T3 下载（带文本锚点）", t3_download),
        ("T4 新标签页（window.open）", t4_newtab),
        ("T5 httpbin 真实控件", t5_form_submit),
        ("T6 target 写法全覆盖", t6_positional),
    ]
    for name, fn in scenes:
        if only and only not in name:
            continue
        scene(name, fn)
    p = OUT / "agent_web_focus2.json"
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
