"""7 个网页工具 · 逐参数真实站点矩阵（诊断用，不是回归测试）。

设计：
  · 单进程长活 MCP（浏览器状态跨用例保持），超时自动重启
  · 每个参数一个用例；负例一律显式给 limit_ms，保证总时长可控
  · 判定不止看 ok/not ok，还看「参数是否真生效」：耗时区间、产物落盘、返回体内容
  · 每跑完一条立刻追加到 out/agent_web_rows.jsonl（中断不丢数据）

用法：python tests/agent_web_matrix.py [--only web_read]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_matrix as MX  # noqa: E402
from mcp_matrix import MCP, OUT, ROOT, png_size  # noqa: E402

ROWS_FILE = OUT / "agent_web_rows.jsonl"
ELEMENTS_FILE = OUT / "agent_web_elements.json"
UPLOAD_FILE = OUT / "agent_web_upload.txt"

QUOTES = "https://quotes.toscrape.com/"
QUOTES2 = "https://quotes.toscrape.com/page/2/"
QUOTES3 = "https://quotes.toscrape.com/page/3/"
FORM = "https://httpbin.org/forms/post"
SAUCE = "https://www.saucedemo.com/"
DEMOQA_CB = "https://demoqa.com/checkbox"
DEMOQA_UP = "https://demoqa.com/upload-download"
HTTPBIN_HTML = "https://httpbin.org/html"
HTTPBIN_IMG = "https://httpbin.org/image"
SLOW = "https://httpbin.org/delay/5"


def C(tool, label, args, expect="ok", msg_has="", ms_min=0, ms_max=0,
      art=(), note="") -> dict:
    """expect: ok/fail。ms_min/ms_max：耗时区间，用于判断 limit_ms 是否生效。"""
    return {"tool": tool, "label": label, "args": args, "expect": expect,
            "msg_has": msg_has, "ms_min": ms_min, "ms_max": ms_max,
            "art": list(art), "note": note}


# ================================================================ 用例表

def build_cases() -> list[dict]:
    out: list[dict] = []
    A = out.append

    # ---- A. 没开页面就动手（诊断性） -----------------------------
    A(C("web_read", "A1 未开页面读文本", {"target": "div.quote", "kind": "text", "limit_ms": 1000},
        expect="fail", msg_has="页面未打开"))
    A(C("web_wait", "A2 未开页面等待", {"target": "body", "limit_ms": 1000},
        expect="fail", msg_has="页面未打开"))
    A(C("web_click", "A3 未开页面点击", {"target": "body", "limit_ms": 1000},
        expect="fail", msg_has="页面未打开"))
    A(C("web_fill", "A4 未开页面填写", {"target": "#x", "value": "v", "limit_ms": 1000},
        expect="fail", msg_has="页面未打开"))
    A(C("web_capture", "A5 未开页面截图", {"kind": "web_shot", "path": "out/agent_web_nopage.png"},
        expect="fail", msg_has="页面未打开"))
    A(C("web_network", "A6 未开页面监听", {"action": "start"}, expect="fail", msg_has="没有打开的页面"))

    # ---- B. web_open ---------------------------------------------
    A(C("web_open", "B1 kind=new 打开 quotes", {"url": QUOTES, "kind": "new", "until": "dom", "limit_ms": 30000},
        note="基线"))
    A(C("web_read", "B1b 确认落地页", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_open", "B2 kind=back 无历史", {"kind": "back", "limit_ms": 10000},
        expect="fail", note="新开的页没有历史，后退应当失败"))
    A(C("web_read", "B2b 后退后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_open", "B3 kind=new 再开一页", {"url": QUOTES2, "kind": "new", "until": "dom", "limit_ms": 30000},
        note="验证 new 是否真开新标签"))
    A(C("web_open", "B3b kind=back 验证历史", {"kind": "back", "limit_ms": 10000},
        note="若 new 是复用当前标签，这里能回退到首页"))
    A(C("web_read", "B3c 后退后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000},
        note="/page/2/ = 新标签没历史(new 真的新开了)；/ = new 落成了复用"))
    A(C("web_open", "B4 kind=forward", {"kind": "forward", "limit_ms": 10000}))
    A(C("web_read", "B4b 前进后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_open", "B5 kind=refresh", {"kind": "refresh", "limit_ms": 15000}))
    A(C("web_read", "B5b 刷新后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_open", "B6 until=load", {"url": QUOTES, "until": "load", "limit_ms": 30000},
        note="对照 until 三种取值耗时"))
    A(C("web_open", "B7 until=none", {"url": QUOTES2, "until": "none", "limit_ms": 30000},
        note="commit 语义，应最快"))
    A(C("web_open", "B8 until=dom", {"url": QUOTES, "until": "dom", "limit_ms": 30000}))
    A(C("web_open", "B9 until=非法值", {"url": QUOTES, "until": "bogus", "limit_ms": 10000},
        expect="fail", msg_has="bogus"))
    A(C("web_open", "B10 limit_ms=1000 慢站", {"url": SLOW, "until": "load", "limit_ms": 1000},
        expect="fail", ms_max=8000, note="delay/5，1s 必须超时"))
    A(C("web_open", "B11 limit_ms=30000 慢站", {"url": SLOW, "until": "load", "limit_ms": 30000},
        ms_min=4000, note="对照：参数真生效则耗时 >=4s"))
    A(C("web_open", "B12 kind=reuse 带 url", {"kind": "reuse", "url": FORM, "until": "dom", "limit_ms": 30000}))
    A(C("web_read", "B12b 复用后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_open", "B13 kind=reuse 不带 url", {"kind": "reuse"}))
    A(C("web_open", "B14 kind=attach 不带 cdp", {"kind": "attach"}, note="接管当前已开浏览器里的页面"))
    A(C("web_open", "B15 url 为空 kind=new", {"url": "", "kind": "new"}, expect="fail", msg_has="缺 url"))
    A(C("web_open", "B16 url 非法", {"url": "not-a-url", "kind": "new", "limit_ms": 8000},
        expect="fail", note="错误信息是否可诊断"))
    A(C("web_open", "B17 action=back 别名", {"action": "back", "limit_ms": 10000}))
    A(C("web_read", "B17b 别名后退后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_open", "B18 action=refresh 别名", {"action": "refresh", "limit_ms": 15000}))
    A(C("web_open", "B19 action 与 kind 冲突", {"action": "refresh", "kind": "new", "url": QUOTES3, "limit_ms": 15000},
        note="action 优先则停留在当前页；kind 优先则跳 page/3"))
    A(C("web_read", "B19b 冲突后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_open", "B20 kind=close", {"kind": "close"}))
    A(C("web_read", "B20b 关闭后读", {"kind": "eval", "code": "() => 1", "limit_ms": 3000},
        expect="fail", msg_has="页面未打开"))
    A(C("web_open", "B21 无页面时 kind=all", {"kind": "all"}, expect="fail",
        note="收尾应当可用；报页面未打开则不合理"))
    A(C("web_open", "B22 重开 quotes", {"url": QUOTES, "kind": "new", "until": "dom", "limit_ms": 30000}))

    # ---- C. web_read ---------------------------------------------
    A(C("web_read", "C1 text div.quote", {"target": "div.quote", "kind": "text", "limit_ms": 10000}))
    A(C("web_read", "C2 text= 写法", {"target": "text=Next", "kind": "text", "limit_ms": 10000}))
    A(C("web_read", "C3 value 用在 div", {"target": "div.quote", "kind": "value", "limit_ms": 8000},
        expect="fail", note="div 没有 value，应可诊断"))
    A(C("web_read", "C4 attr href", {"target": "li.next a", "kind": "attr", "attr": "href", "limit_ms": 10000},
        note="应得 /page/2/"))
    A(C("web_read", "C5 attr + save_as", {"target": "li.next a", "kind": "attr", "attr": "href",
                                          "save_as": "next_href", "limit_ms": 10000}))
    A(C("web_read", "C5b 引用 save_as 变量", {"kind": "eval", "code": '() => "{next_href}"', "limit_ms": 5000},
        note="变量替换生效则应返回 /page/2/"))
    A(C("web_read", "C6 kind=attr 不给 attr", {"target": "div.quote", "kind": "attr", "limit_ms": 8000},
        expect="fail", note="attr 为空应报错，退化成 text 就是参数不生效"))
    A(C("web_read", "C7 html", {"target": "div.quote", "kind": "html", "limit_ms": 10000}))
    A(C("web_read", "C8 list div.quote", {"target": "div.quote", "kind": "list", "limit_ms": 10000},
        note="应 10 条"))
    A(C("web_read", "C9 list 用 text= 写法", {"target": "text=Next", "kind": "list", "limit_ms": 8000},
        note="三种 target 写法在 list 下是否都支持"))
    A(C("web_read", "C10 children", {"target": "div.quote", "kind": "children", "limit_ms": 10000}))
    A(C("web_read", "C11 siblings", {"target": "div.quote", "kind": "siblings", "limit_ms": 10000}))
    A(C("web_read", "C12 eval 箭头函数", {"kind": "eval", "code": "() => document.querySelectorAll('div.quote').length",
                                          "limit_ms": 5000}))
    A(C("web_read", "C13 eval 裸表达式", {"kind": "eval", "code": "1+1", "limit_ms": 5000}))
    A(C("web_read", "C14 eval 无 code 无 target", {"kind": "eval", "limit_ms": 5000}, expect="fail", msg_has="code"))
    A(C("web_read", "C15 eval 缺 code 用 target 兜底", {"kind": "eval", "target": "2+2", "limit_ms": 5000},
        note="target 当 code 用，应返回 4"))
    A(C("web_read", "C16 元素不存在 limit=1000", {"target": "不存在_的元素", "kind": "text", "limit_ms": 1000},
        expect="fail", ms_max=6000))
    A(C("web_read", "C17 元素不存在 limit=8000", {"target": "不存在_的元素", "kind": "text", "limit_ms": 8000},
        expect="fail", ms_min=7000, note="对照：参数真生效则等满 8s"))
    A(C("web_read", "C18 action=html 别名", {"target": "div.quote", "action": "html", "limit_ms": 10000}))
    A(C("web_read", "C19 kind 非法", {"target": "div.quote", "kind": "xml", "limit_ms": 5000},
        expect="fail", msg_has="xml"))
    A(C("web_read", "C20 @el 库里没有", {"target": "@el:不存在", "kind": "text", "limit_ms": 5000},
        expect="fail", msg_has="元素库"))
    A(C("web_read", "C21 @el 有但不在本页", {"target": "@el:baidu_submit", "kind": "text", "limit_ms": 2000},
        expect="fail", note="库里 baidu 的按钮，当前页没有"))

    # ---- D. web_wait ---------------------------------------------
    A(C("web_wait", "D1 selector visible", {"target": "body", "state": "visible", "limit_ms": 5000}))
    A(C("web_wait", "D2 selector attached", {"target": "div.quote", "state": "attached", "limit_ms": 5000}))
    A(C("web_wait", "D3 state=hidden 在可见元素上", {"target": "div.quote", "state": "hidden", "limit_ms": 2000},
        expect="fail", ms_min=1500, ms_max=6000, note="state 生效则等满 2s 超时"))
    A(C("web_wait", "D4 when=gone 元素还在", {"target": "div.quote", "when": "gone", "limit_ms": 2000},
        expect="fail", ms_min=1500))
    A(C("web_wait", "D5 when=gone 元素本就没有", {"target": "div.根本没有", "when": "gone", "limit_ms": 2000},
        note="不存在=已消失，应立即通过"))
    A(C("web_wait", "D6 when=text 命中", {"target": "The world", "when": "text", "limit_ms": 5000}))
    A(C("web_wait", "D7 when=text 不命中 limit=1500", {"target": "绝对不存在的文案XYZ", "when": "text", "limit_ms": 1500},
        expect="fail", ms_min=1200, ms_max=6000))
    A(C("web_wait", "D8 when=url 命中", {"target": "quotes.toscrape.com", "when": "url", "limit_ms": 3000}))
    A(C("web_wait", "D9 when=url 不命中 limit=1500", {"target": "https://example.com/", "when": "url", "limit_ms": 1500},
        expect="fail", ms_min=1200, ms_max=6000))
    A(C("web_wait", "D10 action=text 别名", {"target": "The world", "action": "text", "limit_ms": 5000}))
    A(C("web_wait", "D11 kind=url 别名", {"target": "quotes.toscrape", "kind": "url", "limit_ms": 5000}))
    A(C("web_wait", "D12 when 与 action 冲突", {"target": "quotes.toscrape", "when": "text", "action": "url",
                                                "limit_ms": 3000},
        note="action 优先应走 url 分支；走 text 分支会失败"))
    A(C("web_wait", "D13 元素不存在 limit=1000", {"target": "不存在_的元素", "limit_ms": 1000},
        expect="fail", ms_max=6000))
    A(C("web_wait", "D14 when 非法", {"target": "body", "when": "visible", "limit_ms": 2000},
        expect="fail", msg_has="visible"))

    # ---- E. web_click -------------------------------------------
    A(C("web_click", "E1 kind=left 点下一页", {"target": "li.next a", "kind": "left", "limit_ms": 10000},
        note="应跳到 /page/2/"))
    A(C("web_read", "E1b 点击后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_click", "E2 text= 写法点击", {"target": "text=Next", "limit_ms": 10000}))
    A(C("web_read", "E2b 点击后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}))
    A(C("web_click", "E3 kind=hover", {"target": "li.next a", "kind": "hover", "limit_ms": 10000}))
    A(C("web_click", "E4 kind=double", {"target": "li.next", "kind": "double", "limit_ms": 10000}))
    A(C("web_click", "E5 kind=right", {"target": "li.next", "kind": "right", "limit_ms": 10000}))
    A(C("web_click", "E6 kind=middle", {"target": "li.next", "kind": "middle", "limit_ms": 10000},
        note="中键通常会开新标签"))
    A(C("web_click", "E7 kind=scroll", {"target": "div.quote", "kind": "scroll", "limit_ms": 10000}))
    A(C("web_click", "E8 kind=drag 不给 to", {"target": "div.quote", "kind": "drag", "limit_ms": 5000},
        expect="fail", msg_has="to"))
    A(C("web_click", "E9 kind=drag 给 to", {"target": "div.quote", "kind": "drag", "to": "li.next", "limit_ms": 10000}))
    A(C("web_click", "E10 hints 单独用（target 空）", {"target": "", "hints": {"css": "li.next a"}, "limit_ms": 5000},
        expect="fail", note="target 必填，hints 无法独立使用"))
    A(C("web_click", "E11 hints 与错误 target", {"target": "不存在_的元素", "hints": {"css": "li.next a"},
                                                  "limit_ms": 2000},
        expect="fail", note="target 优先 → hints 被忽略"))
    A(C("web_click", "E12 不存在元素 limit=1000", {"target": "不存在_的元素", "limit_ms": 1000},
        expect="fail", ms_max=6000))
    A(C("web_click", "E13 @el 库里没有", {"target": "@el:不存在", "limit_ms": 5000}, expect="fail", msg_has="元素库"))
    A(C("web_click", "E14 kind 非法", {"target": "li.next", "kind": "triple", "limit_ms": 5000},
        expect="fail", msg_has="triple"))
    A(C("web_click", "E15 action=hover 别名", {"target": "li.next a", "action": "hover", "limit_ms": 10000}))
    A(C("web_click", "E16 action 非法值", {"target": "li.next a", "action": "click", "limit_ms": 5000},
        expect="fail", msg_has="click"))
    # 注入一个 target=_blank 链接验证 follow_new_tab
    A(C("web_read", "E17 注入 _blank 链接", {
        "kind": "eval",
        "code": "() => { const a=document.createElement('a'); a.id='agent_nt'; "
                "a.href='https://quotes.toscrape.com/page/4/'; a.target='_blank'; a.textContent='agent-nt'; "
                "document.body.prepend(a); return 'ok'; }", "limit_ms": 5000}))
    A(C("web_read", "E17b 确认当前页", {"kind": "eval", "code": "() => location.href", "limit_ms": 5000}))
    A(C("web_click", "E18 follow_new_tab=true", {"target": "#agent_nt", "follow_new_tab": True, "limit_ms": 15000},
        note="应切到新标签并返回 tab 下标"))
    A(C("web_read", "E18b 新标签地址", {"kind": "eval", "code": "() => location.href", "limit_ms": 5000},
        note="应为 /page/4/"))
    A(C("web_click", "E19 follow_new_tab=false", {"target": "#agent_nt", "follow_new_tab": False, "limit_ms": 15000}))

    # ---- F. web_fill（httpbin 原生表单） -------------------------
    A(C("web_open", "F0 打开 httpbin 表单", {"url": FORM, "until": "dom", "limit_ms": 30000}))
    A(C("web_fill", "F1 text 单字段", {"target": "input[name=custname]", "value": "张三", "limit_ms": 10000}))
    A(C("web_read", "F1b 读回 value", {"target": "input[name=custname]", "kind": "value", "limit_ms": 5000},
        note="应为 张三"))
    A(C("web_fill", "F2 clear=false 追加", {"target": "input[name=custname]", "value": "李四", "clear": False,
                                            "limit_ms": 10000}))
    A(C("web_read", "F2b 读回 value", {"target": "input[name=custname]", "kind": "value", "limit_ms": 5000},
        note="clear=false 若生效应是 张三李四；是 李四 则参数无效"))
    A(C("web_fill", "F3 clear=true 覆盖", {"target": "input[name=custname]", "value": "王五", "clear": True,
                                           "limit_ms": 10000}))
    A(C("web_read", "F3b 读回 value", {"target": "input[name=custname]", "kind": "value", "limit_ms": 5000}))
    A(C("web_fill", "F4 verify=true", {"target": "input[name=custname]", "value": "赵六", "verify": True,
                                       "limit_ms": 10000}, note="verify 是否真的回读校验"))
    A(C("web_fill", "F5 kind=label", {"target": "Customer name", "value": "钱七", "kind": "label", "limit_ms": 10000}))
    A(C("web_read", "F5b 读回 value", {"target": "input[name=custname]", "kind": "value", "limit_ms": 5000}))
    A(C("web_fill", "F6 kind=label 标签不存在", {"target": "不存在的标签ABC", "value": "x", "kind": "label",
                                                 "limit_ms": 1500}, expect="fail"))
    A(C("web_fill", "F7 kind=form 批量", {"kind": "form", "pairs": [
        {"target": "input[name=custname]", "value": "孙八"},
        {"label": "Telephone", "value": "13800000000"}], "limit_ms": 10000}))
    A(C("web_read", "F7b 读回 value", {"target": "input[name=custname]", "kind": "value", "limit_ms": 5000}))
    A(C("web_fill", "F8 kind=form 含坏项", {"kind": "form", "pairs": [
        {"target": "input[name=custname]", "value": "正常"},
        {"target": "#根本不存在", "value": "坏"}], "limit_ms": 2000},
        expect="fail", note="有一项失败整体应判失败"))
    A(C("web_fill", "F9 kind=form 空 pairs", {"kind": "form", "pairs": [], "limit_ms": 5000}, expect="fail"))
    A(C("web_fill", "F10 text 目标不存在 limit=1000", {"target": "#根本不存在", "value": "x", "limit_ms": 1000},
        expect="fail", ms_max=6000))
    A(C("web_fill", "F11 text 空值", {"target": "input[name=custname]", "value": "", "limit_ms": 5000},
        expect="fail", msg_has="空"))
    A(C("web_fill", "F12 kind 非法", {"target": "input[name=custname]", "value": "x", "kind": "radio",
                                      "limit_ms": 5000}, expect="fail", msg_has="radio"))
    A(C("web_fill", "F13 多余参数 action", {"target": "input[name=custname]", "value": "多余参数",
                                            "action": "form", "limit_ms": 8000},
        note="schema 无 action；被静默忽略则不报错"))
    A(C("web_read", "F13b 读回 value", {"target": "input[name=custname]", "kind": "value", "limit_ms": 5000},
        note="仍是单字段写入说明 action 被忽略"))

    # ---- G. saucedemo（React SPA + select + img） ----------------
    A(C("web_open", "G0 打开 saucedemo", {"url": SAUCE, "until": "dom", "limit_ms": 30000}))
    A(C("web_fill", "G1 填用户名", {"target": "#user-name", "value": "standard_user", "limit_ms": 10000}))
    A(C("web_fill", "G2 填密码", {"target": "#password", "value": "secret_sauce", "limit_ms": 10000}))
    A(C("web_click", "G3 点登录", {"target": "#login-button", "limit_ms": 10000}))
    A(C("web_wait", "G4 等 URL 变 inventory", {"target": "inventory.html", "when": "url", "limit_ms": 15000}))
    A(C("web_read", "G5 list 商品", {"target": "div.inventory_item", "kind": "list", "limit_ms": 10000},
        note="应 6 条"))
    A(C("web_fill", "G6 kind=dropdown", {"target": "select.product_sort_container", "value": "hilo",
                                         "kind": "dropdown", "limit_ms": 10000}))
    A(C("web_read", "G6b 读回 select value", {"target": "select.product_sort_container", "kind": "value",
                                              "limit_ms": 5000}, note="应为 hilo"))
    A(C("web_fill", "G7 dropdown 值不存在", {"target": "select.product_sort_container", "value": "不存在的选项",
                                             "kind": "dropdown", "limit_ms": 5000}, expect="fail",
        note="错误信息是否可诊断"))
    A(C("web_capture", "G8 kind=image 元素", {"kind": "image", "target": "img", "path": "out",
                                              "name": "agent_web_img_sauce.png", "action": ""},
        art=["out/agent_web_img_sauce.png"]))

    # ---- H. demoqa（复杂 SPA + iframe 广告） --------------------
    A(C("web_open", "H0 打开 demoqa checkbox", {"url": DEMOQA_CB, "until": "load", "limit_ms": 40000}))
    A(C("web_wait", "H1 等 SPA 渲染", {"target": "#tree-node", "state": "visible", "limit_ms": 20000}))
    A(C("web_click", "H2 展开 Home", {"target": "button.rct-option-expand-all", "limit_ms": 8000},
        note="demoqa 树控件"))
    A(C("web_fill", "H3 kind=checkbox 勾选", {"target": "#tree-node-home", "value": "1", "kind": "checkbox",
                                              "limit_ms": 8000}))
    A(C("web_read", "H3b 校验勾选结果", {"kind": "eval",
                                         "code": "() => !!document.querySelector('#tree-node-home')?.checked || "
                                                 "document.querySelectorAll('.rct-icon-check').length",
                                         "limit_ms": 5000}, note="应为真/大于 0"))
    A(C("web_open", "H4 打开 demoqa upload", {"url": DEMOQA_UP, "until": "load", "limit_ms": 40000}))
    A(C("web_wait", "H5 等上传控件", {"target": "#uploadFile", "state": "attached", "limit_ms": 20000}))
    A(C("web_fill", "H6 kind=upload", {"target": "#uploadFile", "value": "out/agent_web_upload.txt",
                                       "kind": "upload", "limit_ms": 10000}))
    A(C("web_fill", "H7 upload 文件不存在", {"target": "#uploadFile", "value": "out/根本没有这个文件.txt",
                                             "kind": "upload", "limit_ms": 8000}, expect="fail",
        msg_has="不存在"))

    # ---- I. web_capture -----------------------------------------
    A(C("web_open", "I0 回到 quotes", {"url": QUOTES, "until": "dom", "limit_ms": 30000}))
    A(C("web_capture", "I1 web_shot 不给 path", {"kind": "web_shot"},
        note="应自动落到项目 out/"))
    A(C("web_capture", "I2 web_shot 带后缀路径", {"kind": "web_shot", "path": "out/agent_web_shot.png"},
        art=["out/agent_web_shot.png"]))
    A(C("web_capture", "I3 path=目录 + name", {"kind": "web_shot", "path": "out", "name": "agent_web_named.png"},
        art=["out/agent_web_named.png"]))
    A(C("web_capture", "I4 path=目录 无 name", {"kind": "web_shot", "path": "out"},
        note="自动按秒命名，落在 out/"))
    A(C("web_capture", "I5 多级目录自动建", {"kind": "web_shot", "path": "out/agent_web_sub/deep.png"},
        art=["out/agent_web_sub/deep.png"]))
    A(C("web_capture", "I6 绝对路径", {"kind": "web_shot", "path": "C:/Windows/Temp/agent_web_bad.png"},
        expect="fail", msg_has="路径"))
    A(C("web_capture", "I7 越界路径", {"kind": "web_shot", "path": "../agent_web_out.png"},
        expect="fail", msg_has="路径"))
    A(C("web_capture", "I8 full_page=true", {"kind": "web_shot", "path": "out/agent_web_full.png",
                                             "full_page": True}, art=["out/agent_web_full.png"]))
    A(C("web_capture", "I9 full_page=false 对照", {"kind": "web_shot", "path": "out/agent_web_view.png",
                                                   "full_page": False}, art=["out/agent_web_view.png"]))
    A(C("web_capture", "I10 action=web_shot 别名", {"action": "web_shot", "path": "out/agent_web_alias.png"},
        art=["out/agent_web_alias.png"]))
    A(C("web_capture", "I11 kind 非法", {"kind": "pdf"}, expect="fail", msg_has="pdf"))
    A(C("web_capture", "I12 download 目标不存在", {"kind": "download", "target": "#根本没有这个下载链接",
                                                   "path": "out", "name": "agent_web_dl_bad.bin"},
        expect="fail"))
    A(C("web_open", "I13 打开 httpbin/html", {"url": HTTPBIN_HTML, "until": "dom", "limit_ms": 30000}))
    A(C("web_read", "I14 注入同源下载链接", {
        "kind": "eval",
        "code": "() => { const a=document.createElement('a'); a.id='agent_dl'; a.href='/bytes/1024'; "
                "a.download='agent_web_dl.bin'; a.textContent='dl'; document.body.prepend(a); return 'ok'; }",
        "limit_ms": 5000}))
    A(C("web_capture", "I15 kind=download", {"kind": "download", "target": "#agent_dl", "path": "out",
                                             "name": "agent_web_dl.bin"},
        art=["out/agent_web_dl.bin"], note="应是 1024 字节"))
    A(C("web_open", "I16 打开 httpbin/image", {"url": HTTPBIN_IMG, "until": "dom", "limit_ms": 30000}))
    A(C("web_capture", "I17 kind=image 真实 img", {"kind": "image", "target": "img[src*='png']", "path": "out",
                                                    "name": "agent_web_img.png"},
        art=["out/agent_web_img.png"]))
    A(C("web_capture", "I18 kind=image 目标不存在", {"kind": "image", "target": "#没有这个图", "path": "out",
                                                     "name": "agent_web_img_bad.png", "limit_ms": 2000},
        expect="fail"))

    # ---- J. web_network ----------------------------------------
    A(C("web_open", "J0 回 quotes", {"url": QUOTES, "until": "dom", "limit_ms": 30000}))
    A(C("web_network", "J1 action=start", {"action": "start"}))
    A(C("web_click", "J2 触发请求（点下一页）", {"target": "li.next a", "limit_ms": 10000}))
    A(C("web_network", "J3 action=wait 命中", {"action": "wait", "pattern": "/page/2", "limit_ms": 8000},
        note="应命中并返回 url/status"))
    A(C("web_network", "J4 wait pattern 不命中", {"action": "wait", "pattern": "/根本没这个接口", "limit_ms": 1500},
        expect="fail", ms_min=1200, ms_max=6000, note="超时应给出最近请求列表"))
    A(C("web_network", "J5 action=stop", {"action": "stop"}, note="应返回已记录请求"))
    A(C("web_network", "J6 kind=start 别名", {"kind": "start"}))
    A(C("web_network", "J7 action 与 kind 冲突", {"action": "stop", "kind": "start"},
        note="其他工具 action 优先；这里 kind 参数在 action 前面，看谁赢"))
    A(C("web_network", "J8 save_as 指定变量名", {"action": "wait", "pattern": "/page", "save_as": "netresp",
                                                 "limit_ms": 5000}))
    A(C("web_network", "J9 wait 缺 pattern", {"action": "wait", "limit_ms": 3000}, expect="fail", msg_has="pattern"))
    A(C("web_network", "J10 action 非法", {"action": "listen"}, expect="fail", msg_has="listen"))

    return out


# ================================================================ 元素库阶段（独立进程）

def build_el_cases() -> list[dict]:
    return [
        C("web_open", "K1 打开 quotes", {"url": QUOTES, "until": "dom", "limit_ms": 30000}),
        C("web_read", "K2 @el:quote_card 读文本", {"target": "@el:quote_card", "kind": "text", "limit_ms": 10000}),
        C("web_click", "K3 @el:next_link 点击", {"target": "@el:next_link", "limit_ms": 10000}),
        C("web_read", "K3b 点击后地址", {"kind": "eval", "code": "() => location.pathname", "limit_ms": 5000}),
        C("web_read", "K4 @el:list_card 读 list", {"target": "@el:list_card", "kind": "list", "limit_ms": 10000}),
        C("web_click", "K5 @el:库里没有", {"target": "@el:不存在", "limit_ms": 3000}, expect="fail", msg_has="元素库"),
    ]


# ================================================================ 执行

def prep_files() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    UPLOAD_FILE.write_text("agent web upload probe\n", encoding="utf-8")
    ELEMENTS_FILE.write_text(json.dumps({
        "version": 1,
        "elements": {
            "quote_card": {"key": "quote_card", "role": None, "name": None, "css": "div.quote",
                           "hints": {}, "fingerprint": {"attrs": {}, "path": [], "siblings": []}},
            "list_card": {"key": "list_card", "role": None, "name": None, "css": "div.quote",
                          "hints": {}, "fingerprint": {"attrs": {}, "path": [], "siblings": []}},
            "next_link": {"key": "next_link", "role": "link", "name": "Next", "css": None,
                          "hints": {}, "fingerprint": {"attrs": {}, "path": [], "siblings": []}},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def brief(payload, limit: int = 1500):
    if payload is None:
        return None
    s = json.dumps(payload, ensure_ascii=False)
    return payload if len(s) <= limit else {"_truncated": s[:limit] + "…"}


def judge(case: dict, r: dict) -> tuple[str, list[str]]:
    """返回 (判定, 说明列表)。"""
    flags: list[str] = []
    if r["status"] == "timeout":
        return "TIMEOUT", ["调用超过 60s"]
    if r["status"] == "crash":
        return "CRASH", [f"进程退出: {r.get('stderr','')[:120]}"]
    if r["status"] == "badjson":
        return "BAD-JSON", []

    expect = case["expect"]
    if expect == "ok" and not r["ok"]:
        return "UNEXPECTED-FAIL", [r["message"][:160]]
    if expect == "fail" and r["ok"]:
        flags.append("本该失败却 ok:true")
        return "UNEXPECTED-OK", flags

    # 预期形态对了，再查参数是否真生效
    verdict = "PASS" if (expect == "ok") == r["ok"] else "EXP-FAIL"
    ms = r["ms"]
    if case["ms_min"] and ms < case["ms_min"]:
        flags.append(f"耗时 {ms:.0f}ms < 下界 {case['ms_min']}ms（等太短/参数没生效）")
    if case["ms_max"] and ms > case["ms_max"]:
        flags.append(f"耗时 {ms:.0f}ms > 上界 {case['ms_max']}ms（等太久/limit_ms 没生效）")
    if case["msg_has"] and case["msg_has"] not in str(r["message"]):
        flags.append(f"错误信息里没有 {case['msg_has']!r}（不可诊断）")
    for rel in case["art"]:
        p = ROOT / rel
        if not p.is_file():
            flags.append(f"产物缺失 {rel}")
            continue
        size = p.stat().st_size
        if size <= 0:
            flags.append(f"产物 0 字节 {rel}")
        if p.suffix == ".png":
            size_ = png_size(p)
            if size_ == "-":
                flags.append(f"不是合法 PNG {rel}")
    if flags and verdict == "PASS":
        verdict = "SUSPECT"
    return verdict, flags


def run(cases: list[dict], tag: str, mcp: MCP | None = None) -> list[dict]:
    own = mcp is None
    if own:
        mcp = MCP()
    rows: list[dict] = []
    print(f"\n===== {tag}：{len(cases)} 组 =====")
    try:
        for i, case in enumerate(cases, 1):
            r = mcp.call(case["tool"], case["args"])
            v, flags = judge(case, r)
            payload = r["payload"]
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, str) and data and ("out/" in data or data.endswith(".png")):
                p = Path(data)
                if not p.is_absolute():
                    p = ROOT / data
                if p.is_file():
                    flags = [f for f in flags if "产物缺失" not in f] + [f"返回 data={data} {p.stat().st_size}B"]
            row = {
                "tool": case["tool"], "label": case["label"], "args": case["args"],
                "expect": case["expect"], "verdict": v, "ms": round(r["ms"]),
                "ok": r["ok"], "status": r["status"], "message": str(r["message"])[:300],
                "flags": flags, "note": case["note"], "payload": brief(payload),
            }
            rows.append(row)
            with ROWS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            flag = ("  ⚠ " + " / ".join(flags)) if flags else ""
            print(f"  [{i:>3}/{len(cases)}] {v:<16} {case['tool']:<13} {case['label']:<30} "
                  f"{r['ms']:>7.0f}ms {str(r['message'])[:50]}{flag}")
    finally:
        if own:
            mcp.stop()
    return rows


def main() -> int:
    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    prep_files()
    if ROWS_FILE.exists() and not only:
        ROWS_FILE.unlink()

    cases = build_cases()
    if only:
        cases = [c for c in cases if only in c["tool"]]

    rows = run(cases, "主阶段 · 默认元素库")

    # 元素库阶段：独立进程 + 独立 RPA_ELEMENTS
    el_rows: list[dict] = []
    if not only:
        old = MX.ENV.get("RPA_ELEMENTS")
        MX.ENV["RPA_ELEMENTS"] = str(ELEMENTS_FILE)
        try:
            el_rows = run(build_el_cases(), "元素库阶段 · agent_web_elements.json")
        finally:
            MX.ENV["RPA_ELEMENTS"] = old or str(ROOT / "elements.json")

    all_rows = rows + el_rows
    dump = {
        "total": len(all_rows),
        "verdicts": {},
        "rows": all_rows,
        "engines": {"python": sys.executable},
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    for r in all_rows:
        dump["verdicts"][r["verdict"]] = dump["verdicts"].get(r["verdict"], 0) + 1
    (OUT / "agent_web_matrix.json").write_text(
        json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 100)
    print("统计:", json.dumps(dump["verdicts"], ensure_ascii=False))
    print("可疑/失败:")
    for r in all_rows:
        if r["verdict"] != "PASS":
            print(f"  {r['verdict']:<16} {r['tool']:<13} {r['label']:<30} {'; '.join(r['flags'])[:110]}")
    print(f"\n明细 {OUT / 'agent_web_matrix.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
