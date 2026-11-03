"""针对主阶段暴露出的可疑点做隔离复测（每个场景独立 MCP 进程，避免状态串扰）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_matrix import MCP, OUT, ROOT  # noqa: E402

ROWS = OUT / "agent_web_rows.jsonl"
QUOTES = "https://quotes.toscrape.com/"
QUOTES3 = "https://quotes.toscrape.com/page/3/"
QUOTES4 = "https://quotes.toscrape.com/page/4/"
SLOW = "https://httpbin.org/delay/5"
DEMOQA_CB = "https://demoqa.com/checkbox"
HTTPBIN_IMG = "https://httpbin.org/image"

results: list[dict] = []


def rec(scene: str, label: str, r: dict, note: str = "") -> None:
    row = {
        "scene": scene, "tool": "", "label": label, "args": {},
        "expect": "ok", "verdict": "INFO" if r["ok"] else "FAIL",
        "ms": round(r["ms"]), "ok": r["ok"], "status": r["status"],
        "message": str(r["message"])[:300], "flags": [], "note": note,
        "payload": r["payload"] if not _big(r["payload"]) else {"_truncated": _big(r["payload"])},
    }
    results.append(row)
    print(f"    {label:<34} {'ok' if r['ok'] else 'FAIL':<5} {r['ms']:>7.0f}ms  {str(r['message'])[:60]}")
    print(f"      data={json.dumps(r['payload'], ensure_ascii=False)[:260]}")


def _big(payload) -> str:
    s = json.dumps(payload, ensure_ascii=False)
    return s[:800] if len(s) > 800 else ""


def scene(name: str, fn) -> None:
    print(f"\n===== {name} =====")
    m = MCP()
    try:
        fn(m)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 场景异常: {type(exc).__name__}: {exc}")
        results.append({"scene": name, "label": "场景异常", "verdict": "CRASH",
                        "message": f"{type(exc).__name__}: {exc}", "ms": 0, "ok": False,
                        "status": "crash", "args": {}, "expect": "ok", "flags": [], "note": "",
                        "payload": None, "tool": ""})
    finally:
        m.stop()


def run(m: MCP, label: str, tool: str, args: dict, note: str = "") -> dict:
    r = m.call(tool, args)
    rec("", label, r, note)
    return r


# ---------------------------------------------------------------- 场景


def s1_network(m: MCP) -> None:
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "network start（页面已存在）", "web_network", {"action": "start"})
    run(m, "点下一页", "web_click", {"target": "li.next a", "limit_ms": 10000})
    run(m, "network wait /page/2", "web_network", {"action": "wait", "pattern": "/page/2", "limit_ms": 8000},
        "健康态下应命中")
    run(m, "network stop 取记录", "web_network", {"action": "stop"})
    run(m, "network wait save_as", "web_network", {"action": "wait", "pattern": "/page", "save_as": "netresp",
                                                   "limit_ms": 4000})


def s1b_network_poison(m: MCP) -> None:
    """复现：页面没开时先调一次 network，之后监听再也挂不上。"""
    run(m, "先调 network（无页面）", "web_network", {"action": "start"})
    run(m, "再打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "network start 第二次", "web_network", {"action": "start"})
    run(m, "点下一页", "web_click", {"target": "li.next a", "limit_ms": 10000})
    run(m, "network wait /page/2", "web_network", {"action": "wait", "pattern": "/page/2", "limit_ms": 4000},
        "命中=监听正常；最近请求 [] = 被废掉")
    run(m, "network stop", "web_network", {"action": "stop"})


def s2_back_blank(m: MCP) -> None:
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "读 href（前）", "web_read", {"kind": "eval", "code": "() => location.href", "limit_ms": 5000})
    r = run(m, "kind=back 无历史", "web_open", {"kind": "back", "limit_ms": 10000})
    run(m, "读 href（后）", "web_read", {"kind": "eval", "code": "() => location.href", "limit_ms": 5000},
        "about:blank 说明后退把页面导到空白页却报成功" if r["ok"] else "")


def s3_mouse(m: MCP) -> None:
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "li.next 可见性探测", "web_read", {
        "kind": "eval",
        "code": "() => {const e=document.querySelector('li.next'); "
                "if(!e) return 'no-el'; const s=getComputedStyle(e); "
                "return [s.display, s.visibility, e.getBoundingClientRect().height, e.offsetParent!==null].join('|');}",
        "limit_ms": 5000}, "display|visibility|height|hasOffsetParent")
    run(m, "kind=double 隔离", "web_click", {"target": "li.next", "kind": "double", "limit_ms": 8000})
    run(m, "kind=right 隔离", "web_click", {"target": "li.next", "kind": "right", "limit_ms": 8000})
    run(m, "kind=middle 隔离", "web_click", {"target": "li.next", "kind": "middle", "limit_ms": 8000})
    run(m, "读 href", "web_read", {"kind": "eval", "code": "() => location.href", "limit_ms": 5000})


def s4_newtab(m: MCP) -> None:
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "注入 _blank 链接", "web_read", {
        "kind": "eval",
        "code": "() => {const a=document.createElement('a'); a.id='nt'; a.href='"
                + QUOTES4 + "'; a.target='_blank'; a.textContent='nt'; "
                "document.body.prepend(a); return 'ok';}", "limit_ms": 5000})
    run(m, "follow_new_tab=true", "web_click", {"target": "#nt", "follow_new_tab": True, "limit_ms": 15000},
        "应返回 data.tab 并切页")
    run(m, "读 href（点击后）", "web_read", {"kind": "eval", "code": "() => location.href", "limit_ms": 5000})
    run(m, "关掉当前页", "web_open", {"kind": "close"})
    run(m, "读 href（关页后，若存在弹窗页会露出来）", "web_read",
        {"kind": "eval", "code": "() => location.href", "limit_ms": 5000},
        "能读到=确实有第二个标签页；报页面未打开=根本没开新标签")


def s5_slow(m: MCP) -> None:
    run(m, "打开 quotes 打底", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "delay/5 limit_ms=30000", "web_open", {"url": SLOW, "until": "load", "limit_ms": 30000},
        "应 5s 左右成功")
    run(m, "delay/5 再来一次 load", "web_open", {"url": SLOW, "until": "load", "limit_ms": 30000})
    run(m, "delay/5 until=none", "web_open", {"url": SLOW, "until": "none", "limit_ms": 30000})


def s6_demoqa(m: MCP) -> None:
    run(m, "打开 demoqa/checkbox", "web_open", {"url": DEMOQA_CB, "until": "load", "limit_ms": 40000})
    run(m, "body 文本长度", "web_read",
        {"kind": "eval", "code": "() => (document.body.innerText||'').length", "limit_ms": 5000})
    run(m, "rct-checkbox 数量", "web_read",
        {"kind": "eval", "code": "() => document.querySelectorAll('.rct-checkbox').length", "limit_ms": 5000})
    run(m, "iframe 数量", "web_read",
        {"kind": "eval", "code": "() => document.querySelectorAll('iframe').length", "limit_ms": 5000})
    run(m, "等 #tree-node 10s", "web_wait", {"target": "#tree-node", "state": "attached", "limit_ms": 10000})
    run(m, "checkbox 勾选", "web_fill", {"target": "#tree-node-home", "value": "1", "kind": "checkbox",
                                         "limit_ms": 8000})
    run(m, "读 checked", "web_read",
        {"kind": "eval", "code": "() => document.querySelector('#tree-node-home')?.checked ?? 'no-el'",
         "limit_ms": 5000})


def s7_img(m: MCP) -> None:
    run(m, "打开 httpbin/image", "web_open", {"url": HTTPBIN_IMG, "until": "load", "limit_ms": 30000})
    run(m, "img 数量与首个 src", "web_read", {
        "kind": "eval", "code": "() => {const a=[...document.querySelectorAll('img')]; "
                                "return a.length + '|' + (a[0]? a[0].getAttribute('src'):'none');}",
        "limit_ms": 5000})
    run(m, "capture image（img 标签）", "web_capture", {"kind": "image", "target": "img", "path": "out",
                                                       "name": "agent_web_img2.png"})
    run(m, "capture image（带 src 属性选择器）", "web_capture",
        {"kind": "image", "target": "[src]", "path": "out", "name": "agent_web_img3.png"})


def s8_inject(m: MCP) -> None:
    """兜底：在真实页面里造真控件，验证 checkbox/dropdown/upload 的参数通路。"""
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "注入控件", "web_read", {
        "kind": "eval",
        "code": "() => {document.body.insertAdjacentHTML('afterbegin',"
                "\"<select id='agent_sel'><option value='a'>A</option><option value='b'>B</option></select>"
                "<input type='checkbox' id='agent_cb'><input type='file' id='agent_file'>\"); return 'ok';}",
        "limit_ms": 5000})
    run(m, "dropdown 选 b", "web_fill", {"target": "#agent_sel", "value": "b", "kind": "dropdown",
                                        "limit_ms": 8000})
    run(m, "读 select value", "web_read", {"target": "#agent_sel", "kind": "value", "limit_ms": 5000})
    run(m, "checkbox 勾选", "web_fill", {"target": "#agent_cb", "value": "1", "kind": "checkbox", "limit_ms": 8000})
    run(m, "读 checked", "web_read", {"kind": "eval", "code": "() => document.querySelector('#agent_cb').checked",
                                      "limit_ms": 5000}, "应为 true")
    run(m, "checkbox 取消（value=0）", "web_fill", {"target": "#agent_cb", "value": "0", "kind": "checkbox",
                                                    "limit_ms": 8000})
    run(m, "读 checked", "web_read", {"kind": "eval", "code": "() => document.querySelector('#agent_cb').checked",
                                     "limit_ms": 5000}, "应为 false")
    run(m, "upload 真控件", "web_fill", {"target": "#agent_file", "value": "out/agent_web_upload.txt",
                                        "kind": "upload", "limit_ms": 8000})
    run(m, "读 files 数", "web_read",
        {"kind": "eval", "code": "() => document.querySelector('#agent_file').files.length", "limit_ms": 5000},
        "应为 1")


def s9_dl(m: MCP) -> None:
    run(m, "打开 httpbin/html", "web_open", {"url": "https://httpbin.org/html", "until": "dom", "limit_ms": 30000})
    run(m, "注入下载链接", "web_read", {
        "kind": "eval",
        "code": "() => {const a=document.createElement('a'); a.id='dl'; a.href='/bytes/1024'; "
                "a.download='agent_web_dl2.bin'; document.body.prepend(a); return 'ok';}", "limit_ms": 5000})
    run(m, "capture download", "web_capture", {"kind": "download", "target": "#dl", "path": "out",
                                              "name": "agent_web_dl2.bin"})
    p = OUT / "agent_web_dl2.bin"
    print(f"      文件大小: {p.stat().st_size if p.is_file() else 'MISSING'}B（应为 1024）")


def s10_drag(m: MCP) -> None:
    """真实站点上没有拖拽源的替代：注入 draggable 元素 + 目标。"""
    run(m, "打开 quotes", "web_open", {"url": QUOTES, "until": "dom", "limit_ms": 30000})
    run(m, "注入拖拽源/目标", "web_read", {
        "kind": "eval",
        "code": "() => {document.body.insertAdjacentHTML('afterbegin',"
                "\"<div id='src' draggable='true' style='width:60px;height:60px;background:#ccc'>S</div>"
                "<div id='dst' style='width:120px;height:120px;background:#eee'>D</div>\");"
                "return 'ok';}", "limit_ms": 5000})
    run(m, "drag 到目标", "web_click", {"target": "#src", "kind": "drag", "to": "#dst", "limit_ms": 8000})


SCENES = [
    ("S1 网络监听 · 健康态", s1_network),
    ("S1b 网络监听 · 先无页面再监听", s1b_network_poison),
    ("S2 back 无历史", s2_back_blank),
    ("S3 双击/右键/中键 隔离", s3_mouse),
    ("S4 follow_new_tab", s4_newtab),
    ("S5 慢站 limit_ms", s5_slow),
    ("S6 demoqa checkbox", s6_demoqa),
    ("S7 httpbin image", s7_img),
    ("S8 注入控件兜底", s8_inject),
    ("S9 下载字节校验", s9_dl),
    ("S10 拖拽（注入真源）", s10_drag),
]


def main() -> int:
    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    for name, fn in SCENES:
        if only and only not in name:
            continue
        scene(name, fn)
    path = OUT / "agent_web_focus.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n复测明细 → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
