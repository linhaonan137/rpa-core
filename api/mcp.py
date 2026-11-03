"""MCP 服务：20 个工具，全是薄壳，内部转调节点与引擎。

设计原则（见 docs/AGENT-PLAN.md）：
  · 一个工具 = 一个意图，内部用 kind/action 分流到多个节点
  · 参数只暴露「语义必需」的，调优类（limit_ms/attempts）有默认值
  · 工具返回里带 suggestion（超时建议），供第 2 轮调优用
  · 不按节点镜像：93 个节点 → 20 个工具

单实例：全局一个 ExecutionContext，串行执行（浏览器只开一只）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

import nodes  # noqa: F401  触发节点注册
from engine.context import ExecutionContext
from engine.paths import DB_PATH, ELEMENTS_PATH, FLOWS_DIR, META_DIR_NAME, VERSIONS_DIR_NAME
from engine.registry import Result, registry
from engine.runner import RunOptions, Runner
from engine.schema import Flow

mcp = FastMCP("rpa")

FLOW_DIR = FLOWS_DIR
FLOW_DIR.mkdir(parents=True, exist_ok=True)

#: 单实例上下文（浏览器跨调用复用）
_ctx = ExecutionContext(run_id="mcp")


# ---------------------------------------------------------------- 内部工具


def _call_ghost_refs() -> list[str]:
    """启动自检：扫描本文件里 `_call("node_type")` 的字面量，找出没注册的节点。

    为什么需要：`script_run` 曾指向 `sys_py`/`sys_cmd` 两个**根本不存在**
    的节点，一调就报"未注册的节点类型"——工具在、能力不在，是静默的功能缺失。
    """
    import re
    from pathlib import Path

    from engine.registry import registry

    try:
        src = Path(__file__).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return []
    refs = {m for m in re.findall(r'_call\(\s*"([a-zA-Z_][\w]*)"', src) if m != "node_type"}
    known = set(registry.types())
    return sorted(r for r in refs if r not in known)


def _merge_action(*values: str, allowed: tuple[str, ...], default: str) -> tuple[str, dict[str, Any] | None]:
    """合并多种叫法（kind / action / when / task），取第一个非空的，并校验取值。

    同一批工具里"要做什么"有的叫 kind、有的叫 action、有的叫 when ——
    Agent 猜错就报 validation error。这里让几种叫法互相兼容。
    """
    picked = next((v for v in values if v is not None and str(v).strip()), default)
    v = str(picked).strip()
    if v not in allowed:
        return "", {"ok": False, "error": f"取值不支持: {v!r}；可选: {', '.join(allowed)}"}
    return v, None


def _safe_flow(name: str) -> tuple[str, dict[str, Any] | None]:
    """流程名校验：只能是文件名，不许带路径分隔符或 ..。

    实测漏洞：flow_save(flow="../tmpa_escape") 会把文件写到 flows/ 外面，
    flow_read(flow="../elements") 能读 elements.json。必须是白名单字符。
    """
    n = (name or "").strip()
    if not n:
        return "", {"ok": False, "error": "flow 名不能为空"}
    bad = [c for c in ("/", "\\", "..", ":", "*", "?", '"', "<", ">", "|") if c in n]
    if bad:
        return "", {"ok": False, "error": f"flow 名不能包含 {bad}: {n!r}（只允许字母数字下划线横线）"}
    return n, None


async def _call(node_type: str, args: dict[str, Any]) -> dict[str, Any]:
    """调一个节点，统一成 MCP 可返回的 dict。

    录制模式下：args 里的 `{{变量}}` 会先被换成实际值再执行，
    但**记录的是原始模板** —— 这样录出来的流程是参数化的。
    """
    record_args = dict(args or {})
    run_args = record_args
    try:
        from engine.recorder import recorder

        if recorder.active and recorder.vars:
            run_args = recorder.resolve(record_args)
    except Exception:  # noqa: BLE001 - 录制器不可用就按原参数跑
        run_args = record_args

    try:
        fn = registry.get(node_type)
    except KeyError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        r: Result = await fn(run_args, _ctx)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {"ok": r.ok, "message": r.message}
    if r.error:
        out["error"] = r.error
    if r.data is not None:
        out["data"] = _jsonable(r.data)
    # 录制：只记**成功**的写操作（失败的不该进流程）
    # 探索类节点（scan/discover/web_elem_info…）不在 RECORDABLE 里，自动跳过
    if r.ok:
        try:
            from engine.recorder import recorder

            recorder.add(node_type, record_args, r.message)
        except Exception:  # noqa: BLE001 - 录制失败不影响主流程
            pass
    if r.skipped:
        out["skipped"] = True
    return out


def _safe_path(p: str, *, allow_empty: bool = True) -> tuple[str, dict[str, Any] | None]:
    r"""对外接口的路径校验：**只接受相对路径**，绝对路径和 .. 越界一律拒绝。

    节点内部仍用 resolve_path（流程之间可以传绝对路径），但 Agent 通过 MCP
    调过来的一律限制在项目根内 —— 否则它能读 C:/Windows/System32/...。
    """
    from engine.paths import PathNotAllowed, relative_path

    raw = (p or "").strip()
    if not raw:
        if allow_empty:
            return "", None
        return "", {"ok": False, "error": "path 不能为空"}
    try:
        return str(relative_path(raw)), None
    except PathNotAllowed as exc:
        return "", {"ok": False, "error": str(exc)}


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)


def _suggest(node_type: str) -> dict[str, Any] | None:
    """读执行趋势给超时建议（D07 的数据）。没有 db 时返回 None。"""
    try:
        from store.db import DB

        db = DB(DB_PATH)
        v = db.suggest_timeout(node_type)
        db.close()
        return {"timeout_ms": v} if v else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 网页 7 个


@mcp.tool
async def web_open(
    url: str = "",
    kind: Literal["new", "reuse", "attach", "back", "forward", "refresh", "close", "all"] = "new",
    until: Literal["load", "dom", "none"] = "dom",
    limit_ms: int = 60000,
    action: str = "",
    cdp: str = "",
    match: Literal["contains", "exact"] = "contains",
) -> dict[str, Any]:
    """打开/接管/关闭页面，或前进后退刷新。

    kind=attach 表示接管一个已在运行的浏览器（不新开）。
      - `cdp="http://127.0.0.1:57032"` 连到那个已登录的浏览器（**不需要重新登录**）
      - `url="myseller.taobao.com"` 只用来**匹配**已有标签页，不会导航
    动作参数可写 kind 或 action（二选一，等价）。
    """
    kind, err = _merge_action(
        action, kind,
        allowed=("new", "reuse", "attach", "back", "forward", "refresh", "close", "all"),
        default="new",
    )
    if err:
        return err
    if kind in ("back", "forward", "refresh", "close", "all"):
        t = {"back": "web_back", "forward": "web_forward", "refresh": "web_reload",
             "close": "web_close", "all": "web_close"}[kind]
        args = {"kind": "all"} if kind == "all" else {}
        return await _call(t, args)
    if kind == "attach":
        a: dict[str, Any] = {"kind": "url", "match": match, "limit_ms": limit_ms}
        if url:
            a["url"] = url
        if cdp:
            a["cdp"] = cdp
        return await _call("web_attach", a)
    if kind == "reuse":
        # 复用当前页：给 url 就在当前标签导航，不给就纯确认"有页面可用"
        # （之前没这个分支，url 空时落到 web_open 报"缺 url"，语义完全丢失）
        if url:
            return await _call(
                "web_open",
                {"url": url, "kind": "current_tab", "until": until, "limit_ms": limit_ms},
            )
        page = getattr(_ctx, "page", None)
        if page is None:
            return {"ok": False, "error": "reuse 需要有已打开的页面；没有页面请用 kind=new + url"}
        return {"ok": True, "message": "复用当前页面", "data": {"url": str(getattr(page, "url", ""))}}
    return await _call("web_open", {"url": url, "until": until, "limit_ms": limit_ms})


@mcp.tool
async def web_click(
    target: str,
    kind: Literal["left", "right", "middle", "double", "hover", "scroll", "drag"] = "left",
    hints: dict | None = None,
    to: str = "",
    follow_new_tab: bool = False,
    limit_ms: int = 30000,
    action: str = "",
    mode: Literal["cdp", "human", "real"] = "cdp",
    verify_text: str = "",
    hold_ms: int = 60,
) -> dict[str, Any]:
    """对一个元素做动作：点/双击/右键/悬停/滚动/拖拽。

    target 可写 `@el:name`（元素库）或 css / text= / xpath=。

    mode 决定怎么点：
      cdp   —— 原生 CDP 点击（默认，快）
      human —— CDP 层拟人轨迹（贝塞尔 + 按住）
      real  —— **操作系统级真实鼠标**（Windows SendInput）。淘宝/千牛这类后台的
               受控组件对 CDP 合成事件响应不稳，关键操作用这个。

    verify_text：点之前校验元素文本，不符就拒绝 —— 防止定位退化后点错地方。
    """
    kind, err = _merge_action(action, kind, allowed=('left', 'right', 'middle', 'double', 'hover', 'scroll', 'drag'), default='left')
    if err:
        return err
    if kind == "hover":
        return await _call("web_hover", {"target": target, "hints": hints, "limit_ms": limit_ms})
    if kind == "scroll":
        return await _call("web_scroll", {"target": target, "kind": "element", "limit_ms": limit_ms})
    if kind == "drag":
        if not to:
            return {"ok": False, "error": "kind=drag 需要给 to"}
        return await _call("web_drag", {"target": target, "hints": hints, "to": to, "limit_ms": limit_ms})
    return await _call(
        "web_click",
        {
            "target": target,
            "hints": hints,
            "kind": kind,
            "follow_new_tab": follow_new_tab,
            "limit_ms": limit_ms,
            "mode": mode,
            "verify_text": verify_text,
            "hold_ms": hold_ms,
        },
    )


@mcp.tool
async def web_fill(
    target: str = "",
    value: str = "",
    kind: Literal["text", "label", "form", "dropdown", "checkbox", "upload"] = "text",
    pairs: list[dict] | None = None,
    clear: bool = True,
    verify: bool = False,
    limit_ms: int = 30000,
    action: str = "",
) -> dict[str, Any]:
    """填表单：输入框 / 按标签填 / 批量填 / 下拉 / 勾选 / 上传文件。

    kind=form 时用 pairs=[{label,target,value}]；kind=upload 时 value 是文件路径。
    动作参数可写 kind 或 action。
    """
    kind, err = _merge_action(action, kind, allowed=("text","label","form","dropdown","checkbox","upload"), default="text")
    if err:
        return err
    if kind == "form":
        r = await _call("web_fill_many", {"pairs": pairs or [], "limit_ms": limit_ms})
    elif kind == "label":
        r = await _call("web_type_label", {"label": target, "value": value, "limit_ms": limit_ms})
    elif kind == "dropdown":
        r = await _call("web_pick_option", {"target": target, "value": value, "limit_ms": limit_ms})
    elif kind == "checkbox":
        r = await _call("web_toggle", {"target": target, "checked": value in ("1", "true", "True", "yes")})
    elif kind == "upload":
        r = await _call("web_upload", {"target": target, "path": value, "limit_ms": limit_ms})
    else:
        r = await _call("web_type", {"target": target, "value": value, "clear": clear, "verify": verify, "limit_ms": limit_ms})
    if r.get("ok") and verify and kind == "text":
        r["verified"] = True
    return r


@mcp.tool
async def web_read(
    target: str = "",
    kind: Literal["text", "value", "attr", "html", "list", "children", "siblings", "eval"] = "text",
    attr: str = "",
    code: str = "",
    save_as: str = "",
    limit_ms: int = 15000,
    action: str = "",
) -> dict[str, Any]:
    """从页面取内容：文本/值/属性/HTML/元素列表/子元素/兄弟/执行 JS。

    kind=eval 时不需要 target（代码里自带选择器）；其他 kind 必须给 target。
    """
    kind, err = _merge_action(action, kind, allowed=('text', 'value', 'attr', 'html', 'list', 'children', 'siblings', 'eval'), default='text')
    if err:
        return err
    if kind != "eval" and not (target or "").strip():
        return {"ok": False, "error": f"kind={kind} 需要 target（只有 kind=eval 可以不给）"}
    if kind == "eval":
        return await _call("web_js", {"code": code or target, "save_as": save_as})
    if kind == "children":
        return await _call("web_children", {"target": target, "save_as": save_as})
    if kind == "siblings":
        return await _call("web_siblings", {"target": target, "save_as": save_as})
    if kind == "list":
        # 用 locator 实现（支持 css / text= / xpath=），不再用 JS querySelectorAll
        # —— 那样 `text=Next` 会直接 SyntaxError
        return await _call(
            "web_matches",
            {"target": target, "limit": 50, "save_as": save_as, "limit_ms": limit_ms},
        )

    # text / value / html / attr：节点读的是 attr，不是 kind
    # （曾经把 kind 传给了 kind 字段、attr 传了空串 → 走 get_attribute("") → 读不到东西还报成功）
    node_attr = attr if (kind == "attr" and attr) else ("text" if kind == "attr" else kind)
    return await _call(
        "web_elem_info",
        {"target": target, "attr": node_attr, "save_as": save_as, "limit_ms": limit_ms},
    )


@mcp.tool
async def web_switch(
    kind: Literal["iframe", "main", "tab"] = "iframe",
    target: str = "",
    index: int = 0,
    title: str = "",
    action: str = "",
) -> dict[str, Any]:
    """切换上下文：进 iframe / 回主文档 / 切标签页。

    - `kind="iframe"`：进 iframe（`target` 给 iframe 的 css，或 `index` 给序号）
    - `kind="main"`：回到主文档（清空 iframe 链）
    - `kind="tab"`：切浏览器标签（`title` 给标题/域名片段，或 `index` 给序号）

    切完之后所有定位都在新的上下文里生效。
    """
    kind, err = _merge_action(action, kind, allowed=("iframe", "main", "tab"), default="iframe")
    if err:
        return err
    if kind == "main":
        return await _call("web_frame_out", {})
    if kind == "tab":
        a: dict[str, Any] = {"kind": "title", "title": title, "match": "contains"} if title else {"kind": "index", "index": index}
        return await _call("web_tab", a)
    a = {"kind": "target", "target": target} if target else {"kind": "index", "index": index}
    return await _call("web_frame_in", a)


@mcp.tool
async def web_dialog(
    kind: Literal["accept", "dismiss"] = "accept",
    prompt_text: str = "",
    wait_ms: int = 0,
    action: str = "",
) -> dict[str, Any]:
    """处理浏览器弹窗（alert / confirm / prompt）。

    `kind="accept"` 点确定，`kind="dismiss"` 点取消。
    `prompt_text`：prompt 弹窗要输入的文字。
    `wait_ms`：等弹窗出现的毫秒数（弹窗经常延迟出现，给 1000~3000 更稳）。
    """
    kind, err = _merge_action(action, kind, allowed=("accept", "dismiss"), default="accept")
    if err:
        return err
    return await _call(
        "web_dialog",
        {"kind": kind, "prompt_text": prompt_text, "wait_ms": max(int(wait_ms or 0), 0)},
    )


@mcp.tool
async def web_check(
    kind: Literal["exists", "visible", "page_ready"] = "exists",
    target: str = "",
    action: str = "",
) -> dict[str, Any]:
    """判断元素/页面状态（**不是等待**，是"现在是什么状态"）。

    - `kind="exists"`：元素在 DOM 里吗
    - `kind="visible"`：元素可见吗
    - `kind="page_ready"`：页面加载完成了吗

    要"等它出现"请用 `web_wait`；要"判断现在有没有"用这个。
    """
    kind, err = _merge_action(action, kind, allowed=("exists", "visible", "page_ready"), default="exists")
    if err:
        return err
    if kind == "page_ready":
        return await _call("web_load_done", {"kind": "ready"})
    if not target:
        return {"ok": False, "error": f"kind={kind} 需要 target"}
    node = "web_has" if kind == "exists" else "web_visible"
    return await _call(node, {"target": target})


@mcp.tool
async def web_wait(
    target: str,
    when: Literal["selector", "text", "url", "gone"] = "selector",
    state: Literal["visible", "attached", "hidden"] = "visible",
    limit_ms: int = 30000,
    action: str = "",
    kind: str = "",
) -> dict[str, Any]:
    """等一个条件成立：元素出现/文本出现/URL 变化/元素消失。

    不要用固定 sleep——用这个等条件。
    """
    when, err = _merge_action(action, kind, when, allowed=("selector", "text", "url", "gone"), default="selector")
    if err:
        return err
    if when == "text":
        return await _call("web_wait", {"text": target, "limit_ms": limit_ms})
    if when == "url":
        return await _call("web_wait", {"url": target, "limit_ms": limit_ms})
    if when == "gone":
        return await _call("web_wait", {"target": target, "kind": "hidden", "limit_ms": limit_ms})
    # 注意：这里必须传 kind（节点读 kind），传 state 会被当初次值忽略
    return await _call("web_wait", {"target": target, "kind": state, "limit_ms": limit_ms})


@mcp.tool
async def inspect_node(node_type: str) -> dict[str, Any]:
    """**查节点的参数名、默认值和说明** —— 拼流程 / 写脚本前先查，别猜字段名。

    参数名猜错的代价很高（今天踩过：text/items、control/target、limit_ms/timeout_ms
    全是猜错的），这个工具直接把 docstring 里的 `args: {...}` 解析成结构化列表。
    """
    import re

    from engine.registry import registry

    try:
        fn = registry.get(node_type)
    except KeyError:
        return {"ok": False, "error": f"节点不存在: {node_type}", "hint": "用 list_node_types 找"}

    doc = (fn.__doc__ or "").strip()
    params: list[dict[str, Any]] = []
    m = re.search(r"args:\s*\{(.+?)\}", doc, re.S)
    if m:
        raw = m.group(1).replace("\n", " ")
        for part in raw.split(","):
            p = part.strip()
            if not p:
                continue
            if "=" in p:
                n, d = p.split("=", 1)
                params.append({"name": n.strip(), "default": d.strip()})
            else:
                params.append({"name": p, "default": None})
    return {
        "ok": True,
        "node": node_type,
        "module": getattr(fn, "__module__", ""),
        "doc": doc[:1000],
        "params": params,
        "param_count": len(params),
    }


@mcp.tool
async def web_extract(
    item: str,
    fields: list[dict] | None = None,
    limit: int = 50,
    save_as: str = "",
) -> dict[str, Any]:
    """**批量抓列表**：一次调用返回整页结构化数组（比逐个读快得多）。

    item：每一项的容器选择器，如 `div.quote`
    fields：字段映射，如 `[{"name":"内容","selector":"span.text"},{"name":"作者","selector":"small.author"}]`
            不给 selector 就取该项的完整文本
    limit：最多抓多少项（默认 50）

    例：web_extract(item="div.quote",
                     fields=[{"name":"quote","selector":"span.text"},
                             {"name":"author","selector":"small.author"}])
    """
    return await _call(
        "web_extract",
        {
            "item": item,
            "fields": fields or [],
            "limit": min(int(limit or 50), 500),
            "save_as": save_as,
        },
    )


@mcp.tool
async def read_failure(run_id: str = "", level: str = "summary") -> dict[str, Any]:
    """**读失败现场**：那次执行失败时页面长什么样、哪个节点挂的、报错原文。

    run_id 为空则读最近一次失败。
    level=summary 只返回概要（失败节点 + 页面 URL + 元素数量）；
    level=dom 额外返回当时的可交互元素和表单字段清单。
    """
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(FAILURES_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not files:
        return {"ok": False, "error": "还没有失败现场记录（只有流程失败时才会存）"}
    target = None
    if run_id:
        p = FAILURES_DIR / f"{run_id}.json"
        if p.exists():
            target = p
        else:
            return {"ok": False, "error": f"没有这个 run_id 的现场: {run_id}", "available": [f.stem for f in files[:5]]}
    else:
        target = files[0]
    try:
        snap = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"现场文件损坏: {exc}"}
    if level != "dom":
        snap.pop("interactive", None)
        snap.pop("fields", None)
    else:
        snap["interactive"] = (snap.get("interactive") or [])[:40]
        snap["fields"] = (snap.get("fields") or [])[:40]
    return {"ok": True, **snap}


@mcp.tool
async def record_start(variables: dict | None = None) -> dict[str, Any]:
    """**开始录制**：之后每个成功的操作（打开页/点击/输入/选择/等待…）自动记成流程节点。

    `variables` 声明参数（如 `{"title": "测试商品A", "price": "99"}`），
    之后操作参数里写 `{{title}}` 就会在**执行时**换成实际值，
    但**录进流程的是 `{title}` 模板** —— 生成的流程是参数化的，能反复跑不同数据。

    典型用法：
        record_start()
        web_open(kind="attach", cdp=...)   → 记录
        web_scan()                          → 不记录（探索辅助）
        element_pick(...)                   → 不记录（元素进库即可）
        web_click("@el:xxx")                → 记录
        record_stop(flow="我的流程")         → 生成 flows/我的流程.json

    **不录**的：scan / web_fields / web_elem_info / element_check / web_verify
    —— 它们是探索工具，跑流程时不需要。
    """
    from engine.recorder import recorder

    # 记下当前页面现场：attach 通常在 record_start 之前发生，
    # 但生成的流程必须从"接管页面"起步，否则第一条操作就报"页面未打开"
    ctx_node: dict[str, Any] | None = None
    page = getattr(_ctx, "page", None)
    if page is not None:
        cdp = getattr(_ctx, "cdp_url", "") or ""
        u = str(getattr(page, "url", "") or "")
        host = ""
        try:
            from urllib.parse import urlparse

            host = urlparse(u).netloc
        except Exception:  # noqa: BLE001
            host = ""
        args: dict[str, Any] = {"kind": "url", "match": "contains", "limit_ms": 15000}
        if host:
            args["url"] = host
        if cdp:
            args["cdp"] = cdp
        ctx_node = {"type": "web_attach", "args": args}
    return recorder.start(variables, ctx_node)


@mcp.tool
async def record_stop(flow: str, title: str = "", save: bool = True) -> dict[str, Any]:
    """停止录制并生成流程。flow 是文件名（只用字母数字下划线中划线）。

    save=True 直接写到 flows/<flow>.json，返回 steps（录到几步）。
    save=False 只把流程 JSON 回传，自己决定存哪。
    """
    from engine.recorder import recorder

    name, err = _safe_flow(flow)
    if err:
        return err
    target = FLOWS_DIR / f"{name}.json" if save else None
    out = recorder.stop(save_to=target, name=name, title=title or name)
    if save and not out.get("steps"):
        out["error"] = "没有录到任何步骤（可能操作都失败了，或者都是探索类工具）"
    return out


@mcp.tool
async def record_status() -> dict[str, Any]:
    """看录制状态：是否在录、已经录了几步（最近 8 步的摘要）。"""
    from engine.recorder import recorder

    return {
        "ok": True,
        "recording": recorder.active,
        "steps": len(recorder.entries),
        "skipped": recorder.skipped,
        "recent": [
            {"type": e["type"], "msg": e.get("msg", "")} for e in recorder.entries[-8:]
        ],
    }


@mcp.tool
async def call_node(name: str, args: dict | None = None) -> dict[str, Any]:
    """直接调用任意引擎节点（实验 / 调试用）。

    **主要用途**：加或改了一个节点，还没封装成正式 MCP 工具时，
    用这个立刻能试 —— 不用改 api/mcp.py、不用重连 MCP。

    例：call_node("web_fields", {"max_label": 20})
        call_node("web_scan", {"kind": "interactive", "limit": 50})

    args 就是节点的原生参数（和流程 JSON 里 args 字段一致）。
    """
    from engine.registry import registry

    types = registry.types()
    if name not in types:
        return {
            "ok": False,
            "error": f"节点不存在: {name}",
            "hint": "用 call_node('list_node_types') 看全部" if "list_node_types" in types else "",
        }
    return await _call(name, dict(args or {}))


@mcp.tool
async def list_node_types(keyword: str = "") -> dict[str, Any]:
    """列出引擎里全部节点类型（可按关键词过滤）。

    加节点后先用这个确认注册成功，再 call_node 试。
    """
    from engine.registry import registry

    names = sorted(registry.types())
    if keyword:
        names = [n for n in names if keyword.lower() in n.lower()]
    return {"ok": True, "count": len(names), "nodes": names}


@mcp.tool
async def reload_nodes() -> dict[str, Any]:
    """重新加载所有节点模块（改了 nodes/*.py 后不用重启 MCP）。

    只对**节点层**改动有效：新增节点、改节点实现、改 JS 注入脚本。
    如果改的是 api/mcp.py 里的工具签名（参数增删），仍然需要重连。
    """
    import sys as _sys

    import nodes as _n

    cleared = []
    for mod in list(_sys.modules):
        if mod.startswith("nodes."):
            del _sys.modules[mod]
            cleared.append(mod)
    failed = _n.load_all()
    from engine.registry import registry

    return {
        "ok": not failed,
        "reloaded_modules": len(cleared),
        "failed": failed,
        "nodes": len(registry.types()),
    }


@mcp.tool
async def web_fields(max_label: int = 16, include_empty: bool = True) -> dict[str, Any]:
    """**一次**发现页面上所有表单字段：label + 控件类型 + 当前值 + 是否必填。

    解决什么问题：淘宝发品页 25 个下拉框，placeholder 全是"请选择/请输入"，
    没有 id 没有 name —— 这个工具用「上方 label」把真实字段名（袖长类型/面料/版型分类）
    全部找出来，不用一个个猜 CSS、也不用写 JS。

    返回 [{label, tag, type, value, ph, required, xy, w}]。
    """
    return await _call(
        "web_fields",
        {"max_label": min(int(max_label or 16), 40), "include_empty": bool(include_empty)},
    )


@mcp.tool
async def web_snapshot(kind: Literal["structure", "text"] = "structure", limit: int = 200) -> dict[str, Any]:
    """页面快照：按视觉顺序列出页面上的区块和文本，用来「先看懂页面再动手」。

    kind=structure 返回可交互元素 + 表单字段的混合视图；
    kind=text 只返回文本块（适合读内容型页面）。
    """
    return await _call("web_snapshot", {"kind": kind, "limit": min(int(limit or 200), 500)})


@mcp.tool
async def web_verify(target: str, limit_ms: int = 5000) -> dict[str, Any]:
    """验证一个选择器 / `@el:名字` 现在还能不能定位，返回命中的层和元素信息。

    写流程前先验一下，比跑完整流程失败再回头查快得多。
    """
    return await _call("web_verify", {"target": target, "limit_ms": limit_ms})


@mcp.tool
async def web_scan(
    kind: Literal["interactive", "all"] = "interactive",
    limit: int = 100,
) -> dict[str, Any]:
    """扫出页面上所有可交互元素（按钮/链接/输入框/可选中的），带 role + name + 坐标。

    典型用法：进了一个陌生页面，先 web_scan 看一眼有哪些可点的，再决定 element_pick 哪个。
    比猜 CSS 选择器靠谱得多。
    """
    return await _call("web_scan", {"kind": kind, "limit": min(int(limit or 100), 300)})


@mcp.tool
async def element_pick(
    selector: str = "",
    key: str = "",
    text: str = "",
    store: str = "",
    role: str = "",
) -> dict[str, Any]:
    """把一个元素拾取进元素库，之后用 `@el:key` 引用它。

    三种给法（任选其一）：
      - `role="link", text="支付金额"`：**按 ARIA 语义拾取，最准**（web_scan 的 role+name 可直接用）
      - `text="提交订单"`：按可见文本（会自动兜到 placeholder/aria-label）
      - `selector="#submit"`：任意 Playwright 写法（css / xpath= / text=）

    拾取时抓六维指纹（role/name/attrs/path/siblings/geometry），
    以后定位会逐层降级，不只依赖一个选择器。需要先 web_open 打开页面。
    """
    if not key:
        return {"ok": False, "error": "element_pick 需要 key（元素在库里的名字）"}
    base: dict[str, Any] = {"key": key}
    if store:
        s, err = _safe_path(store)
        if err:
            return err
        base["store"] = s

    # ① role + 文本：走 get_by_role（exact 优先），最贴近可访问性树
    #    注意不能拼成 `role=xxx[name=..]` —— Playwright 那个语法只做子串匹配，
    #    「发布商品」会撞到「发布商品时没有所需类目怎么办？」这种隐藏长文本
    if role and text:
        r = await _call("web_pick", {**base, "role": role, "name": text, "exact": True})
        if r.get("ok"):
            return r
        selector = f"text={text}"

    # ② 纯文本：先试属性组（输入框只有 placeholder），再退 text=
    if not selector and text:
        esc = str(text).replace('"', '\\"')
        attr_sel = (
            f'[placeholder="{esc}"], [aria-label="{esc}"], '
            f'[title="{esc}"], [name="{esc}"]'
        )
        r = await _call("web_pick", {**base, "selector": attr_sel})
        if r.get("ok"):
            return r
        selector = f"text={text}"

    if not selector:
        return {"ok": False, "error": "element_pick 需要 selector、text 或 role+text"}
    return await _call("web_pick", {**base, "selector": selector})


@mcp.tool
async def element_list() -> dict[str, Any]:
    """列出元素库：key / role / name / hits / misses / 是否疑似失效。

    `stale=True` 表示连续 3 次定位失败，建议重新拾取。
    """
    return await _call("web_pick_list", {})


@mcp.tool
async def element_check(keys: list[str] | None = None) -> dict[str, Any]:
    """体检：在当前页面上逐个验证元素库条目还能不能定位，以及靠哪一层命中。

    这是判断"元素库还准不准"的最直接方法 —— 页面改版后跑一次就知道哪些坏了。
    keys 为空则检查全部。

    返回每条的 {key, ok, layer, tried}：
      layer=role+name 最好（语义没变）；path / sibling / geometry 说明已经在退化。
    """
    page = getattr(_ctx, "page", None)
    if page is None:
        return {"ok": False, "error": "element_check 需要先 web_open 打开页面"}

    from elements.resolve import AmbiguousElement, resolve_fingerprint
    from elements.store import ElementStore

    store = ElementStore(str(ELEMENTS_PATH))
    targets = keys or store.keys()
    results: list[dict[str, Any]] = []
    for k in targets:
        if not store.has(k):
            results.append({"key": k, "ok": False, "layer": None, "error": "元素库里没有"})
            continue
        e = store.get(k)
        entry = {
            "role": e.role,
            "name": e.name,
            "css": e.css,
            "attrs": e.fingerprint.attrs,
            "path": e.fingerprint.path,
            "siblings": e.fingerprint.siblings,
            "geometry": {"x": e.fingerprint.geometry.x, "y": e.fingerprint.geometry.y},
            "tag": e.fingerprint.tag,
            # 表单 label（"袖长类型"这类）——不带的话只能退化到 path 层
            "label": (e.hints or {}).get("label") or "",
        }
        try:
            # 元素在 iframe 里时，先从主 frame 切进去（和 _locate 的处理保持一致）
            el_root: Any = page
            for fsel in (e.fingerprint.frame or []):
                f_sel = fsel.get("selector") if isinstance(fsel, dict) else str(fsel)
                if f_sel:
                    el_root = el_root.frame_locator(f_sel)
            loc, diag = await resolve_fingerprint(el_root, entry, key=k)
            if loc is None:
                e.miss()
                results.append({
                    "key": k, "ok": False, "layer": None,
                    "error": "六维定位全部失败", "tried": diag.get("tried"),
                })
            else:
                e.hit()
                results.append({"key": k, "ok": True, "layer": diag.get("layer"), "tried": diag.get("tried")})
        except AmbiguousElement as exc:
            results.append({"key": k, "ok": False, "layer": None, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            results.append({"key": k, "ok": False, "layer": None, "error": f"{type(exc).__name__}: {exc}"})
    store.save()

    ok_n = sum(1 for r in results if r.get("ok"))
    degraded = [r["key"] for r in results if r.get("ok") and r.get("layer") not in ("role+name", "role+name(scored)", None)]
    return {
        "ok": True,
        "checked": len(results),
        "passed": ok_n,
        "failed": [r["key"] for r in results if not r.get("ok")],
        "degraded": degraded,  # 靠降级层才命中的（页面已经变了，建议重拾）
        "detail": results,
    }


@mcp.tool
async def web_capture(
    kind: Literal["web_shot", "download", "image"] = "web_shot",
    target: str = "",
    path: str = "",
    name: str = "",
    full_page: bool = False,
    action: str = "",
) -> dict[str, Any]:
    """截图 / 下载文件 / 保存图片。

    name：文件名（不给就按秒自动命名 —— 同一秒两次会互相覆盖，建议给）
    """
    kind, err = _merge_action(action, kind, allowed=('web_shot', 'download', 'image'), default='web_shot')
    if err:
        return err
    if path:
        path, err = _safe_path(path)
        if err:
            return err
    if kind == "download":
        return await _call("web_download", {"target": target, "path": path, "name": name})
    if kind == "image":
        return await _call("web_save_img", {"target": target, "path": path, "name": name})
    # 节点读的是 kind=page|full，不是 full_page 布尔
    return await _call(
        "web_shot",
        {"path": path, "name": name, "kind": "full" if full_page else "page"},
    )


@mcp.tool
async def web_network(
    action: Literal["start", "wait", "stop"] = "wait",
    pattern: str = "",
    save_as: str = "resp",
    limit_ms: int = 30000,
    kind: str = "",
) -> dict[str, Any]:
    """监听/等待接口返回。比解析 DOM 稳一个数量级。

    典型用法：点查询 → web_network(action="wait", pattern="/api/list") → 直接拿 JSON。
    """
    action, err = _merge_action(kind, action, allowed=('start', 'wait', 'stop'), default='wait')
    if err:
        return err
    if action == "start":
        return await _call("web_net", {"action": "listen", "pattern": pattern})
    if action == "stop":
        # 监听是观察型（挂 page 事件），没有"停止"的必要；给最近记录更有用
        return await _call("web_net", {"action": "log", "limit": 20})
    return await _call(
        "web_net",
        {"action": "wait", "pattern": pattern, "save_as": save_as, "limit_ms": limit_ms},
    )


# ---------------------------------------------------------------- 桌面 / 下载


@mcp.tool
async def desktop_act(
    kind: Literal["table", "select_text", "focused"] = "table",
    control: str = "",
    save_as: str = "",
    limit: int = 1000,
    action: str = "",
) -> dict[str, Any]:
    """桌面三件事：批量抓表格 / 抠控件里读不到的文字 / 读当前焦点控件。

    其余桌面操作请用 desktop-touch（另一个 MCP，32 工具）。
    """
    kind, err = _merge_action(action, kind, allowed=('table', 'select_text', 'focused'), default='table')
    if err:
        return err
    if kind == "select_text":
        return await _call("ui_text", {"control": control, "save_as": save_as})
    if kind == "focused":
        return await _call("ui_focus", {"save_as": save_as or "focused"})
    # 节点 ui_table 读 target（不是 control），实测 control 被静默丢弃
    return await _call("ui_table", {"target": control, "save_as": save_as, "limit": limit})


@mcp.tool
async def media_dl(
    url: str,
    kind: Literal["video", "audio", "playlist", "info", "m3u8"] = "video",
    path: str = "",
    quality: str = "best",
) -> dict[str, Any]:
    """下载：视频 / 音频 / 播放列表 / 只取元数据 / 加密流(m3u8)。"""
    if path:
        path, err = _safe_path(path)
        if err:
            return err
    if kind == "audio":
        return await _call("media_audio", {"url": url, "path": path})
    if kind == "playlist":
        return await _call("media_playlist", {"url": url, "path": path, "quality": quality})
    if kind == "info":
        return await _call("media_info", {"url": url})
    if kind == "m3u8":
        return await _call("media_m3u8", {"url": url, "path": path})
    return await _call("media_get", {"url": url, "path": path, "quality": quality})


# ---------------------------------------------------------------- 通用 4 个


@mcp.tool
async def file_ops(
    action: Literal["read", "write", "append", "copy", "move", "delete", "rename", "exists", "info", "list", "mkdir", "hash"] = "read",
    path: str = "",
    dest: str = "",
    content: str = "",
    save_as: str = "",
    kind: str = "",
) -> dict[str, Any]:
    """文件操作，一个工具 + action 分流。"""
    action, err = _merge_action(kind, action, allowed=('read', 'write', 'append', 'copy', 'move', 'delete', 'rename', 'exists', 'info', 'list', 'mkdir', 'hash'), default='read')
    if err:
        return err
    m = {
        "read": "fs_read", "write": "fs_write", "append": "fs_append",
        "copy": "fs_copy", "move": "fs_move", "delete": "fs_delete",
        "rename": "fs_rename", "exists": "fs_has", "info": "fs_info",
        "list": "fs_list", "mkdir": "fs_mkdir", "hash": "fs_hash",
    }
    if action in ("list", "info") and not (path or "").strip():
        path = "."  # 列表/信息查询默认项目根
    path_abs, err = _safe_path(path, allow_empty=False)
    if err:
        return err
    dest_abs = ""
    if dest:
        dest_abs, err2 = _safe_path(dest, allow_empty=False)
        if err2:
            return err2
    args: dict[str, Any] = {"path": path_abs, "save_as": save_as}
    if action == "rename":
        # 节点的 rename 读 name，且要求纯文件名（不能带路径）——不能走 _safe_path 绝对化
        if (dest or "").strip():
            args["name"] = dest.strip()
    elif dest_abs:
        args["dest"] = dest_abs
    if action in ("write", "append"):
        args["content"] = content  # 空串也是合法内容（写空文件）
    elif content:
        args["content"] = content
    return await _call(m[action], args)


@mcp.tool
async def data_ops(
    action: Literal["data_sheet_read", "data_sheet_write", "data_sheet_append", "data_csv_read", "csv_gen",
                   "seq_order", "seq_uniq", "seq_pick", "regex", "json_query"] = "data_sheet_read",
    path: str = "",
    rows: list | None = None,
    text: str = "",
    save_as: str = "",
    pattern: str = "",
    key: str = "",
    sheet: str = "",
    kind: str = "",
) -> dict[str, Any]:
    """表格与数据：读写 Excel/CSV、列表运算、正则、字典取值。"""
    action, err = _merge_action(kind, action, allowed=('data_sheet_read', 'data_sheet_write', 'data_sheet_append', 'data_csv_read', 'csv_gen', 'seq_order', 'seq_uniq', 'seq_pick', 'regex', 'json_query'), default='data_sheet_read')
    if err:
        return err
    m = {
        "data_sheet_read": "data_sheet_read", "data_sheet_write": "data_sheet_write", "data_sheet_append": "data_sheet_append",
        "data_csv_read": "data_csv_read", "csv_gen": "data_csv_write",
        "seq_order": "seq_order", "seq_uniq": "seq_uniq", "seq_pick": "seq_pick",
        "regex": "data_regex", "json_query": "map_path",
    }
    file_actions = ("data_sheet_read", "data_sheet_write", "data_sheet_append", "data_csv_read")
    # 路径校验统一放在下面的参数映射里做（那里已经转过一次绝对路径，
    # 在这里再 _safe_path 一次会把已解析的绝对路径当"非法绝对路径"拒掉）
    # 参数映射：节点读的名字和 MCP 的习惯名不一致，在这里统一翻译
    # （实测缺陷：list_* 节点要 items 却收到 text；json_query 要 data+path
    #   却收到 text+key；data_sheet_append 是单行 row 不是 rows —— 全部静默空返回）
    args: dict[str, Any] = {"save_as": save_as}

    def _jsonish(t: str):
        t = (t or "").strip()
        if not t:
            return []
        try:
            return json.loads(t)
        except Exception:  # noqa: BLE001
            return [t]

    if action in ("data_sheet_read", "data_sheet_write", "data_sheet_append", "data_csv_read", "csv_gen"):
        if path:
            path, err = _safe_path(path, allow_empty=False)
            if err:
                return err
        args["path"] = path
        args["sheet"] = sheet

    if action == "data_sheet_append":
        args["row"] = (rows[0] if rows else None)  # 追加是单行
    elif action == "data_sheet_write":
        args["rows"] = rows
    elif action in ("seq_order", "seq_uniq", "seq_pick"):
        args["items"] = rows if rows is not None else _jsonish(text)
        args["key"] = key
    elif action == "json_query":
        args["data"] = rows if rows is not None else _jsonish(text)
        args["path"] = key  # 节点的取值路径叫 path
    elif action == "data_csv_read":
        if path and not text:
            from engine.paths import relative_path

            fp = relative_path(path)
            text = fp.read_text(encoding="utf-8") if fp.exists() else ""
        args["text"] = text
    elif action == "csv_gen":
        args["rows"] = rows
    elif action == "regex":
        args["text"] = text
        args["pattern"] = pattern

    return await _call(m[action], args)


@mcp.tool
async def script_run(code: str = "", cmd: str = "", save_as: str = "", limit_ms: int = 60000) -> dict[str, Any]:
    """逃生舱：跑 Python 代码或系统命令。覆盖所有没做成工具的能力。"""
    if cmd:
        # 节点读 timeout_ms（不是 limit_ms），实测传 limit_ms 超时完全失效
        return await _call("sys_cmd", {"cmd": cmd, "timeout_ms": limit_ms, "save_as": save_as})
    return await _call("sys_py", {"code": code, "save_as": save_as, "timeout_ms": limit_ms})


@mcp.tool
async def ai_call(
    task: Literal["chat", "vision", "extract", "normalize", "classify", "find_selector"] = "chat",
    prompt: str = "",
    target: str = "",
    fields: list[str] | None = None,
    save_as: str = "",
    action: str = "",
    api_key: str = "",
    api_url: str = "",
    model: str = "",
) -> dict[str, Any]:
    """调 LLM：对话 / 看图 / 按字段抽取 / 数据规整 / 分类 / 找选择器。

    缺 key 时把 RPA_LLM_KEY 环境变量设上，或在这里传 api_key。
    """
    task, err = _merge_action(action, task, allowed=('chat', 'vision', 'extract', 'normalize', 'classify', 'find_selector'), default='chat')
    if err:
        return err
    m = {
        "chat": "llm_chat", "vision": "llm_vision", "extract": "llm_extract",
        "normalize": "llm_normalize", "classify": "llm_classify",
        "find_selector": "llm_selector",
    }
    args: dict[str, Any] = {"prompt": prompt, "save_as": save_as}
    if api_key:
        args["api_key"] = api_key
    if api_url:
        args["api_url"] = api_url
    if model:
        args["model"] = model
    if target:
        args["target"] = target
    if fields:
        args["fields"] = fields
    return await _call(m[task], args)


@mcp.tool
async def notify(message: str, channel: Literal["webhook", "dingtalk", "wecom", "feishu"] = "webhook", url: str = "") -> dict[str, Any]:
    """发一条通知（一个 webhook POST 就够，不需要 17 个通道）。

    url 必填（钉钉/企微/飞书的机器人 webhook 地址），channel 只决定 body 格式。
    """
    if not url:
        return {"ok": False, "error": "notify 需要 webhook url（钉钉/企微/飞书机器人的地址）"}
    if channel == "feishu":
        body = {"msg_type": "text", "content": {"text": message}}
    else:
        body = {"msgtype": "text", "text": {"content": message}}
    return await _call("net_http", {"url": url, "method": "POST", "json": body})


@mcp.tool
async def net_http(
    url: str,
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"] = "GET",
    json_body: dict | None = None,
    headers: dict | None = None,
    save_as: str = "",
    limit_ms: int = 30000,
) -> dict[str, Any]:
    """调任意 HTTP 接口（Agent 调外部 API 的唯一入口）。"""
    return await _call(
        "net_http",
        {
            "url": url,
            "method": method,
            "json": json_body or None,
            "headers": headers or None,
            "save_as": save_as,
            "timeout_ms": limit_ms,
        },
    )


# ---------------------------------------------------------------- 流程 5 个


@mcp.tool
async def flow_read(flow: str = "") -> dict[str, Any]:
    """不带参数：列出所有流程。带 flow：读该流程的完整 JSON。"""
    if flow:
        flow, err = _safe_flow(flow)
        if err:
            return err
    if not flow:
        items = []
        for p in sorted(FLOW_DIR.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                items.append({"name": p.stem, "title": raw.get("name"), "nodes": len(raw.get("nodes") or [])})
            except Exception:  # noqa: BLE001
                items.append({"name": p.stem, "error": "解析失败"})
        return {"ok": True, "flows": items}
    p = FLOW_DIR / f"{flow}.json"
    if not p.exists():
        return {"ok": False, "error": f"流程不存在: {flow}"}
    return {"ok": True, "flow": json.loads(p.read_text(encoding="utf-8"))}


@mcp.tool
async def flow_save(flow: str, content: dict) -> dict[str, Any]:
    """新建或覆盖一个流程（会用 schema 校验，不合法直接拒绝）。"""
    flow, err = _safe_flow(flow)
    if err:
        return err
    try:
        f = Flow(**content)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"流程校验失败: {exc}"}
    # 空流程没有任何意义，但 schema 允许（只查重复 id / 悬空边）
    # 不拦的话 flow_run 会返回 ok:true + 0 步，Agent 会以为跑通了
    if not f.nodes:
        return {"ok": False, "error": "流程至少要有一个节点（nodes 为空）"}

    p = FLOW_DIR / f"{flow}.json"
    # 覆盖前留快照：flow_patch 一直有 .versions，flow_save 却没有 ——
    # 整体覆盖是不可追溯的，出了问题回不去
    snap = ""
    if p.exists():
        try:
            snap = _snapshot(p, json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - 旧文件坏了也不该挡住覆盖
            snap = ""

    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(f.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return {"ok": True, "path": str(p), "nodes": len(f.nodes), "snapshot": snap}


@mcp.tool
async def flow_patch(
    flow: str,
    node: str,
    args: dict | None = None,
    on_fail: dict | None = None,
    expect: dict | None = None,
    reason: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    """改单个节点的参数（不能改 type/id/边）。

    dry_run 默认 True：先看 diff。reason 必填，写进变更流水。
    """
    flow, err = _safe_flow(flow)
    if err:
        return err
    if not reason:
        return {"ok": False, "error": "必须给 reason（写进审计流水）"}

    p = FLOW_DIR / f"{flow}.json"
    if not p.exists():
        return {"ok": False, "error": f"流程不存在: {flow}"}
    raw = json.loads(p.read_text(encoding="utf-8"))

    target = next((n for n in raw.get("nodes", []) if n.get("id") == node), None)
    if target is None:
        return {"ok": False, "error": f"节点不存在: {node}"}

    before = json.loads(json.dumps(target))
    if args:
        target.setdefault("args", {}).update(args)
    if on_fail is not None:
        # 不校验就落盘会把流程写坏（实测 on_fail={"mode":"bogus"} 照写，
        # 等 flow_run 才炸）
        from engine.schema import OnFail

        try:
            OnFail(**on_fail)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"on_fail 不合法: {exc}"}
        target["on_fail"] = on_fail
    if expect is not None:
        from engine.schema import Expect

        try:
            Expect(**expect)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"expect 不合法: {exc}"}
        target["expect"] = expect
    after = target

    diff = {k: {"from": before.get(k), "to": after.get(k)} for k in set(before) | set(after)
            if before.get(k) != after.get(k)}

    if dry_run:
        return {"ok": True, "dry_run": True, "diff": diff}

    snap = _snapshot(p, raw)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    _log_change(flow, node, diff, reason, snap)
    return {"ok": True, "diff": diff, "snapshot": snap}


@mcp.tool
async def flow_run(
    flow: str,
    vars: dict | None = None,
    start_at: str = "",
    limit_steps: int = 10000,
) -> dict[str, Any]:
    """跑一个流程。vars 的键直接注入成流程变量。"""
    flow, err = _safe_flow(flow)
    if err:
        return err
    p = FLOW_DIR / f"{flow}.json"
    if not p.exists():
        return {"ok": False, "error": f"流程不存在: {flow}"}
    try:
        f = Flow(**json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"流程校验失败: {exc}"}

    from engine.context import ExecutionContext as EC

    # run_id 必须唯一：之前用 r-{秒}，同一秒连跑多次会被 DB 的 ON CONFLICT 覆盖，
    # 只剩最后一条（实测 4 次执行只留 1 条记录，审计数据失真）
    import uuid as _uuid

    run_id = f"r-{int(time.time())}-{_uuid.uuid4().hex[:6]}"
    ctx = EC(run_id=run_id)
    for k, v in (vars or {}).items():
        ctx.set_var(k, v, scope="global")
    for var in f.vars:
        if not ctx.has_var(var.name):
            ctx.set_var(var.name, var.value, scope="global")

    opts = RunOptions(start_at=start_at or None, max_steps=limit_steps)
    try:
        result = await Runner(f, ctx, opts).execute()
    except KeyError as exc:
        # start_at 指向不存在的节点等配置错误：原来裸抛 KeyError，Agent 的通用
        # 错误处理接不住（协议层异常而不是 ok:false）
        return {"ok": False, "error": f"流程配置错误: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"执行器异常: {type(exc).__name__}: {exc}"}

    try:
        from store.db import DB

        db = DB(DB_PATH)
        db.save_run(run_id, f.name, bool(result["ok"]), result["nodes"],
                    stopped=bool(result.get("stopped")),
                    steps=int(result.get("steps", 0)), ms=float(result.get("ms", 0)))
        db.close()
    except Exception:  # noqa: BLE001
        pass

    # 失败时存现场（DOM 摘要 + 表单字段 + 截图），供 read_failure 回看
    if not result["ok"]:
        try:
            await _save_failure(run_id, f.name, result)
        except Exception:  # noqa: BLE001 - 存现场失败不该影响主结果
            pass

    return {
        "ok": result["ok"],
        "run_id": run_id,
        "ms": result["ms"],
        "failed": result["failed"],
        "nodes": [{"id": n["node_id"], "type": n["type"], "ok": n["ok"], "ms": n["ms"]}
                  for n in result["nodes"]],
    }


# ---------------------------------------------------------------- 失败现场


FAILURES_DIR = FLOWS_DIR.parent / "out" / "failures"


async def _save_failure(run_id: str, flow_name: str, result: dict[str, Any]) -> str:
    """把失败现场存下来：页面状态 + 可交互元素 + 表单字段 + 每个节点的错误。

    为什么要存「现场」而不是只存错误信息：
    流程失败时页面往往是"半成品状态"（已经填了一半、弹窗开着），
    光看 "web_click 超时" 根本不知道当时页面上有什么。
    """
    page = getattr(_ctx, "page", None)
    snap: dict[str, Any] = {
        "run_id": run_id,
        "flow": flow_name,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "failed": result.get("failed") or [],
        "nodes": [
            {"id": n.get("node_id"), "type": n.get("type"), "ok": n.get("ok"),
             "ms": n.get("ms"), "error": n.get("error")}
            for n in (result.get("nodes") or [])
            if not n.get("ok")
        ],
    }
    if page is not None:
        try:
            snap["page_url"] = str(getattr(page, "url", ""))
            snap["page_title"] = (await page.title())[:80]
        except Exception:  # noqa: BLE001
            pass
        try:
            r = await _call("web_scan", {"kind": "interactive", "limit": 60})
            snap["interactive"] = r.get("data") or []
        except Exception:  # noqa: BLE001
            pass
        try:
            r = await _call("web_fields", {"max_label": 16})
            snap["fields"] = r.get("data") or []
        except Exception:  # noqa: BLE001
            pass
        try:
            img = await _call("web_shot", {"path": "out/failures", "name": f"{run_id}.png"})
            if img.get("ok"):
                snap["web_shot"] = f"out/failures/{run_id}.png"
        except Exception:  # noqa: BLE001
            pass

    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    p = FAILURES_DIR / f"{run_id}.json"
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    # 只留最近 30 份，避免无限堆积
    olds = sorted(FAILURES_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    for f in olds[30:]:
        try:
            f.unlink()
        except OSError:
            pass
    return str(p)


# ---------------------------------------------------------------- 异步执行


async def _queue_runner(flow: str, variables: dict[str, Any]) -> dict[str, Any]:
    """队列的执行函数：直接复用同步 flow_run 的完整逻辑。"""
    return await flow_run(flow, variables)  # type: ignore[arg-type]


TASK_QUEUE_BOUND = False


def _ensure_queue() -> Any:
    """首次用的时候绑定执行器（避免模块导入顺序问题）。"""
    global TASK_QUEUE_BOUND
    from engine.taskqueue import task_queue

    if not TASK_QUEUE_BOUND:
        task_queue.bind(_queue_runner)
        TASK_QUEUE_BOUND = True
    return task_queue


@mcp.tool
async def flow_run_async(
    flow: str,
    vars: dict | None = None,
) -> dict[str, Any]:
    """**异步跑流程**：立刻返回 task_id，不用等流程跑完。

    为什么需要它：一个 29 秒的流程，同步调用会阻塞 29 秒；
    n8n 并发触发时会排队甚至超时。异步提交后调用方立刻拿到 task_id，
    自己决定什么时候来取结果（`flow_task`）。

    **有队列**：浏览器是独占资源，同时跑两个流程会抢同一个页面。
    所以任务按提交顺序**串行执行**（concurrency=1），返回里有 queue_position。
    """
    q = _ensure_queue()
    name, err = _safe_flow(flow)
    if err:
        return err
    p = FLOW_DIR / f"{name}.json"
    if not p.exists():
        return {"ok": False, "error": f"流程不存在: {name}"}
    out = q.submit(name, vars or {})
    return out


@mcp.tool
async def flow_task(task_id: str, full: bool = True) -> dict[str, Any]:
    """查异步任务的状态与结果。

    状态：queued（排队中）/ running（执行中）/ done / failed / cancelled
    full=True 时带上完整的节点执行明细。
    """
    return _ensure_queue().get(task_id, full=full)


@mcp.tool
async def flow_tasks(limit: int = 20) -> dict[str, Any]:
    """看队列概览：并发度、各状态计数、最近的若干个任务（含排队位置）。"""
    return _ensure_queue().list_recent(limit=min(int(limit or 20), 100))


@mcp.tool
async def flow_cancel(task_id: str) -> dict[str, Any]:
    """取消**还在排队**的任务。已经开始执行的不行（浏览器正在用页面）。"""
    return _ensure_queue().cancel(task_id)


@mcp.tool
async def run_status(flow: str = "", limit: int = 10) -> dict[str, Any]:
    """查最近执行记录 + 各节点的耗时趋势（调参依据）。"""
    try:
        from store.db import DB

        db = DB(DB_PATH)
        # SQLite 把 LIMIT -1 当"不限制"，会把全表吐出来；负数一律当默认 10
        limit = 10 if limit is None or limit < 1 else min(int(limit), 200)
        runs = db.recent_runs(limit=limit, flow=flow or None)
        trend = db.trend(flow=flow or None)
        db.close()
        return {"ok": True, "runs": runs, "trend": trend}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- 内部辅助


def _snapshot(p: Path, raw: dict) -> str:
    """改前快照：.versions/<flow>.snap-<n>.json"""
    d = p.parent / VERSIONS_DIR_NAME
    d.mkdir(exist_ok=True)
    n = len(list(d.glob(f"{p.stem}.snap-*.json"))) + 1
    f = d / f"{p.stem}.snap-{n:04d}.json"
    f.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return f.name


def _log_change(flow: str, node: str, diff: dict, reason: str, snap: str) -> None:
    """变更流水：_meta/<flow>.changes.jsonl"""
    d = FLOW_DIR / META_DIR_NAME
    d.mkdir(exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "flow": flow, "node": node, "diff": diff,
        "reason": reason, "snap": snap,
    }
    with open(d / f"{flow}.changes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    import sys as _sys

    nodes.load_all()
    # 节点模块加载失败 = 整块能力静默消失（比如重名注册抛 DuplicateNodeType，
    # 被 load_all 吞进 failures，谁都不看 —— 实测踩过：新加的 web_pick_list
    # 与 picker 里的同名节点冲突，我的节点直接没注册上，调用打到别人的实现）
    for mod, err in nodes.failures().items():
        print(f"[rpa-mcp] 警告：节点模块 {mod} 加载失败: {err}", file=_sys.stderr, flush=True)
    ghosts = _call_ghost_refs()
    if ghosts:
        # stdio 协议下不能往 stdout 写东西，所以走 stderr
        print(
            f"[rpa-mcp] 警告：以下工具引用了未注册的节点，调用会失败: {', '.join(ghosts)}",
            file=_sys.stderr,
            flush=True,
        )
    mcp.run()


if __name__ == "__main__":
    main()
