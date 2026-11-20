"""实时冒烟：43 个 MCP 工具的 happy path。**默认跳过**（要真浏览器 + 外网）。

    RPA_SMOKE=1 RPA_CDP=http://127.0.0.1:9333 python -m pytest tests/test_smoke_live.py

为什么要这个文件：MCP 工具层是薄封装（参数映射、路径校验、结果转换），
单元测试全在 mock 层面，覆盖不到这一层。实测暴露过的一类问题——
`cdp` 字段读了个不存在的属性、截图不补扩展名、`list_tabs` 与 `web_tab`
下标不是一回事、工具会话自己另拉一只浏览器——全都出在这里，且单元测试
全绿。这个文件是唯一的端到端防线。

结构上的两个刻意选择：
1. **所有浏览器用例挤在一个 test 里**。每条用例一个 test 会各自重连一次浏览器，
   同一只 Chrome 上挂到第 4 个 Playwright 客户端就开始互相干扰（PITFALLS B11），
   `goto` 会卡满超时。一次接管、顺序跑完最稳。
2. 浏览器部分**不遇错即停**：逐步记录，最后一起报，一次跑完能看到全部问题。

`web_dialog` 不在这里测：多客户端下必失败（B11），人工用独立浏览器验证。
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RPA_SMOKE") != "1",
    reason="实时冒烟需要浏览器+外网，设 RPA_SMOKE=1 才跑",
)

LAB = f"_smoke_{int(time.time())}"


@pytest.fixture
async def m():
    import nodes

    nodes.load_all()
    import api.mcp as mod

    return mod


async def ok(tool: str, coro) -> dict:
    """断言调用成功，返回结果体。"""
    r = await coro
    assert r.get("ok"), f"{tool} 失败: {r.get('error') or r.get('message')}"
    return r


# ================================================================ 不依赖浏览器


async def test_meta_queries(m):
    n = await ok("list_node_types", m.list_node_types())
    assert n["count"] >= 100
    assert not [t for t in n["nodes"] if t.startswith("llm_")], "LLM 节点应已移除"

    await ok("inspect_node", m.inspect_node("data_sheet_write"))
    await ok("flow_tasks", m.flow_tasks())
    await ok("run_status", m.run_status(limit=3))

    r = await m.read_failure()          # 没有失败记录时返回 ok:false 属正常
    assert r.get("ok") or "没有失败现场" in str(r.get("error"))


async def test_files_and_data(m):
    name = f"out/{LAB}.txt"      # 落 out/，别脏项目根
    xlsx = f"out/{LAB}.xlsx"
    try:
        await ok("fs_write", m.file_ops(action="write", path=name, content="a{name}b"))
        r = await ok("fs_read", m.file_ops(action="read", path=name))
        assert r["data"] == "a{name}b", "花括号不能被变量解析吃掉"

        await ok("fs_hash", m.file_ops(action="hash", path=name))
        await ok("fs_exists", m.file_ops(action="exists", path=name))
        await ok("fs_list", m.file_ops(action="list", path="out"))

        await ok("sheet_write", m.data_ops(action="data_sheet_write", path=xlsx,
                                           rows=[{"a": 1}, {"a": 2}]))
        r = await ok("sheet_read", m.data_ops(action="data_sheet_read", path=xlsx))
        assert len(r["data"]) == 2

        r = await ok("regex", m.data_ops(action="regex", text="a1b22c333", pattern=r"\d+"))
        assert len(r["data"]) == 3
        await ok("json_query", m.data_ops(action="json_query",
                                          text='{"x":{"y":7}}', key="x.y"))
    finally:
        for p in (name, xlsx):
            await m.file_ops(action="delete", path=p)


async def test_flow_editing(m):
    base = {"name": "smoke", "nodes": [
        {"id": "a1", "type": "set_var", "args": {"name": "v", "value": 1}},
        {"id": "a2", "type": "noop", "args": {}}],
        "edges": [{"id": "e1", "from": "a1", "to": "a2"}], "vars": []}
    try:
        await ok("flow_save", m.flow_save(LAB, base))
        await ok("flow_read", m.flow_read(LAB))

        bad = dict(base, nodes=[{"id": "x", "type": "web_clik", "args": {}}], edges=[])
        r = await m.flow_save("_smoke_bad", bad)
        assert not r.get("ok") and "web_clik" in str(r.get("error")), \
            "拼错的节点类型必须在保存时被拦住"

        await ok("patch_dry", m.flow_patch(LAB, "a1", args={"value": 9},
                                           reason="smoke", dry_run=True))
        await ok("add_dry", m.flow_add_node(LAB, "a1", "noop", reason="smoke", dry_run=True))
        await ok("add", m.flow_add_node(LAB, "a1", "noop", node_id="mid",
                                        reason="smoke", dry_run=False))
        await ok("drop", m.flow_drop_node(LAB, "mid", reason="smoke", dry_run=False))

        await ok("rollback", m.flow_rollback(LAB))
        f = await ok("read_after_rollback", m.flow_read(LAB))
        ids = [n["id"] for n in f["flow"]["nodes"]]
        assert "mid" in ids, f"回滚应撤回上一次删除，实际 {ids}"
    finally:
        await m.flow_delete(LAB)
        await m.flow_delete("_smoke_bad", purge_snapshots=True)


async def test_misc_tools(m):
    r = await ok("uniq", m.call_node("seq_uniq", {"items": ["a", "a", "b"]}))
    assert r["data"] == ["a", "b"]

    await ok("script_run", m.script_run(code="print(1+1)"))
    await ok("net_http", m.net_http(url="https://httpbin.org/json"))
    await ok("notify", m.notify(message="smoke", channel="dingtalk",
                                url="https://httpbin.org/post"))


# ================================================================ 浏览器（一次接管跑完）


async def test_browser_stack(m):
    """元素库 → 页面理解 → 交互 → 抓取 → 流程。全在一个事件循环、一次接管里。"""
    fails: list[str] = []

    async def step(name: str, coro):
        try:
            r = await coro
        except Exception as exc:  # noqa: BLE001
            fails.append(f"{name}: {type(exc).__name__}: {exc}")
            return None
        if not r.get("ok"):
            fails.append(f"{name}: {r.get('error') or r.get('message')}")
            return None
        return r

    if not (await step("web_open", m.web_open(kind="new",
                                             url="https://quotes.toscrape.com/login"))):
        pytest.skip("没有可接管的浏览器: " + "; ".join(fails))

    try:
        # --- 元素库
        await step("el_pick", m.element_pick(key=f"{LAB}_el", role="link", text="Login"))
        r = await step("el_verify", m.element_verify(keys=[f"{LAB}_el"], detail=True))
        if r and r.get("passed") != 1:
            fails.append(f"el_verify: passed={r.get('passed')}")
        await step("el_list", m.element_list())
        await step("el_drop", m.element_drop(key=f"{LAB}_el"))
        r = await step("el_list_after", m.element_list())
        if r and f"{LAB}_el" in {e.get("key") for e in (r.get("data") or [])}:
            fails.append("el_drop: 删了还在（假删除）")

        # --- 页面理解
        await step("check", m.web_check(kind="page_ready"))
        await step("scan", m.web_scan(limit=5))
        await step("snapshot", m.web_snapshot(limit=5))
        await step("fields", m.web_fields())
        await step("wait", m.web_wait(target="input[name=username]", when="selector"))
        await step("read", m.web_read(target="input[name=username]", kind="value"))
        await step("dom", m.dom_inspect(target="form", depth=2))
        r = await step("shot", m.web_capture(kind="shot", name=f"{LAB}_shot", path="out"))
        if r and not str(r.get("data", "")).endswith(".png"):
            fails.append(f"shot: 没补扩展名 {r.get('data')}")
        await m.file_ops(action="delete", path=f"out/{LAB}_shot.png")
        await step("tabs", m.list_tabs())
        await step("switch_url", m.web_switch(kind="tab", title="login"))

        # --- 交互
        await step("fill", m.web_fill(kind="form", verify=True, pairs=[
            {"label": "Username", "target": "input[name=username]", "value": "smoke_u"},
            {"label": "Password", "target": "input[name=password]", "value": "smoke_p"}]))
        r = await step("read_back", m.web_read(target="input[name=username]", kind="value"))
        if r and r.get("data") != "smoke_u":
            fails.append(f"fill: 回读是 {r.get('data')}")
        await step("click", m.web_click(target="input[type=submit]", kind="left"))
        await step("key", m.web_key(kind="press", key="Escape"))

        # --- 抓取（列表页才有 div.quote）
        await step("open_list", m.web_open(kind="reuse", url="https://quotes.toscrape.com/"))
        r = await step("extract", m.web_extract(
            item="div.quote", limit=3,
            fields=[{"name": "t", "selector": "span.text"},
                    {"name": "a", "selector": "small.author"}]))
        if r is not None and len(r.get("data") or []) != 3:
            fails.append(f"extract: 抓到 {len(r.get('data') or [])} 项")

        # --- 网络监听 + 流程
        await step("net_start", m.web_network(action="start", pattern="quotes"))
        await step("flow_run", m.flow_run("real_1_quotes"))
        r = await step("run_async", m.flow_run_async("real_1_quotes"))
        if r:
            time.sleep(4)
            await step("task", m.flow_task(r["task_id"], full=False))
    finally:
        await m.web_open(kind="all")

    assert not fails, "浏览器链路有失败：\n  - " + "\n  - ".join(fails)


# ================================================================ 收尾


async def test_zz_no_residue(m):
    left = [p.stem for p in m.FLOW_DIR.glob("_smoke*")]
    assert not left, f"残留测试流程: {left}"
