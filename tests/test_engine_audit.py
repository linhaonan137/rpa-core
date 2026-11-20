"""引擎模块审计修复的回归测试（2026-11-06）。对应 docs/PITFALLS.md §六：

  K7  装载期校验节点类型（原来跑到第 N 步才报，前面步骤已有副作用）
  E4  元素库合并写（两个进程交替保存不能互相覆盖）
  D1  DB busy_timeout（两进程写同一个 rpa.db）
"""

from __future__ import annotations

import pytest

import nodes  # noqa: F401  触发节点注册（否则 preflight 会把所有类型都判成未注册）
from elements.store import ElementEntry, ElementStore
from engine.context import ExecutionContext
from engine.runner import RunOptions, Runner
from engine.schema import Flow


def _entry(key: str) -> ElementEntry:
    # validate 要求 path 或 css 至少一个，给个兜底 css
    return ElementEntry(key=key, role="button", name=key, css=f"#{key}")


# ---------------------------------------------------------------- E4 合并写


def test_store_save_merges_entries_written_by_other_instance(tmp_path):
    """两个 store 实例交替保存，条目不能互相覆盖（原来全量重写会丢拾取结果）。"""
    path = tmp_path / "elements.json"
    a = ElementStore(str(path))
    a.add(_entry("k1"))
    a.save()

    b = ElementStore(str(path))
    assert "k1" in b.keys()

    a2 = ElementStore(str(path))
    a2.add(_entry("k2"))     # a2 内存：k1 + k2
    b.add(_entry("k3"))      # b 内存：k1 + k3
    b.save()                 # 先 b 写
    a2.save()                # 再 a2 写 —— 不能把 k3 覆盖掉

    c = ElementStore(str(path))
    assert set(c.keys()) == {"k1", "k2", "k3"}


def test_store_save_leaves_no_shared_tmp(tmp_path):
    """tmp 文件名带 pid，两个进程不会撞同一个 .tmp。"""
    path = tmp_path / "elements.json"
    s = ElementStore(str(path))
    s.add(_entry("k1"))
    s.save()

    assert not (tmp_path / "elements.json.tmp").exists()


# ---------------------------------------------------------------- D1 DB


def test_db_sets_busy_timeout(tmp_path):
    from store.db import DB

    db = DB(tmp_path / "t.db")
    try:
        v = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(v) >= 30000
    finally:
        db.close()


# ---------------------------------------------------------------- K7 装载期校验


async def test_preflight_rejects_unknown_node_type():
    """把 MCP 工具名当节点名写进流程：装载期就报，不进主循环。"""
    f = Flow(name="t", version=1, nodes=[{"id": "n1", "type": "web_switch"}], edges=[])

    with pytest.raises(KeyError, match="未注册"):
        await Runner(f, ExecutionContext(), RunOptions()).execute()


async def test_preflight_error_names_the_bad_node():
    f = Flow(name="t", version=1, nodes=[{"id": "n1", "type": "web_switch"}], edges=[])
    with pytest.raises(KeyError) as exc:
        await Runner(f, ExecutionContext(), RunOptions()).execute()

    msg = str(exc.value)
    assert "n1" in msg and "web_switch" in msg     # 节点 id + 错误类型名都要出现
    assert "web_tab" in msg                        # 顺带提示正确的节点名


async def test_preflight_lets_valid_flow_through():
    f = Flow(name="t", version=1, nodes=[{"id": "n1", "type": "noop"}], edges=[])
    r = await Runner(f, ExecutionContext(), RunOptions()).execute()
    assert r["ok"] is True


# ---------------------------------------------------------------- K2 off 用于分支


def test_off_true_rejected_on_branch_nodes():
    """off=true 用在 if/loop 上会被跳过并切断所有下游出口，仍报成功 —— 装载期就拒。"""
    from engine.schema import Node

    for t in ("if", "loop", "while", "foreach"):
        with pytest.raises(ValueError, match="不能用在分支/循环节点"):
            Node(id="n1", type=t, off=True)

    # 普通节点仍然允许 off（调试时临时关掉某一步的合法用法）
    assert Node(id="n2", type="noop", off=True).off is True


# ---------------------------------------------------------------- B3 _settle 容错


async def test_settle_never_raises_on_bad_jitter():
    """_settle 在动作执行**之后**调用：它抛异常 = 点击已发生却报失败 → 重试重复点击。"""
    from nodes.web import _settle

    await _settle({"jitter": "abc"})          # 无法解析 → 不 sleep，但不能抛
    await _settle({"jitter": "1-2"})          # 字符串区间
    await _settle({"jitter": "[1, 2]"})       # 字符串形式的列表
    await _settle({"jitter": [1, 2]})         # 正常列表
    await _settle({"jitter": 1})              # 单值
    await _settle({})                         # 没配


# ---------------------------------------------------------------- 交互配方节点


class _FakeEl:
    def __init__(self, log: list[str], name: str = "", n: int = 0) -> None:
        self.log, self.name, self.n = log, name, n

    async def click(self, **_kw) -> None:
        self.log.append("click:" + self.name)

    async def hover(self, **_kw) -> None:
        self.log.append("hover:" + self.name)

    async def count(self) -> int:
        return self.n


class _FakeLoc:
    def __init__(self, log: list[str], name: str, n: int = 1) -> None:
        self.log, self.name, self.n = log, name, n
        self.first = _FakeEl(log, name, n)

    async def count(self) -> int:
        return self.n

    def nth(self, i: int) -> _FakeEl:
        return _FakeEl(self.log, f"{self.name}#{i}", self.n)


class _FakePage:
    def __init__(self, log: list[str], counts: dict[str, int] | None = None) -> None:
        self.log = log
        self.counts = counts or {}
        self.keyboard = self

    def locator(self, sel: str) -> _FakeLoc:
        return _FakeLoc(self.log, sel, self.counts.get(sel, 0))

    def get_by_text(self, _t: str, **_kw) -> _FakeLoc:
        return _FakeLoc(self.log, "text", self.counts.get("text", 0))

    async def press(self, key: str) -> None:
        self.log.append("key:" + key)

    async def type(self, text: str, **_kw) -> None:
        self.log.append("type:" + text)


async def test_el_action_runs_interaction_recipe(monkeypatch):
    """交互规则写进数据：点开 → 选第一项 → 点容器外空白（不再每个字段硬写节点）。"""
    import nodes.web as web
    from nodes.el_action import web_el_action

    log: list[str] = []
    page = _FakePage(log)

    async def _fake_locate(_page, _target, _hints, _ctx, _limit):
        return _FakeLoc(log, "target")

    monkeypatch.setattr(web, "_locate", _fake_locate)
    ctx = ExecutionContext()
    ctx.page = page

    r = await web_el_action(
        {"target": "css=input.x",
         "interaction": {"open": "click", "choose": "first-option",
                         "close": "click-blank", "close_target": "text=商品属性",
                         "wait_ms": 0}},
        ctx,
    )
    assert r.ok is True
    assert any(s.startswith("click:target") for s in log)
    assert any(s.startswith("click:div.options-item") for s in log)
    assert any(s.startswith("click:text=商品属性") for s in log)


async def test_el_action_escape_close(monkeypatch):
    import nodes.web as web
    from nodes.el_action import web_el_action

    log: list[str] = []

    async def _fake_locate(_page, _target, _hints, _ctx, _limit):
        return _FakeLoc(log, "target")

    monkeypatch.setattr(web, "_locate", _fake_locate)
    ctx = ExecutionContext()
    ctx.page = _FakePage(log)
    r = await web_el_action(
        {"target": "css=input.x",
         "interaction": {"open": "hover", "close": "escape", "wait_ms": 0}},
        ctx,
    )
    assert r.ok is True
    assert "hover:target" in log and "key:Escape" in log


async def test_el_action_type_and_after(monkeypatch):
    """输入类配方：真实键盘 type + 收尾按键（总库存靠 Tab 失焦汇总）。"""
    import nodes.web as web
    from nodes.el_action import web_el_action

    log: list[str] = []

    async def _fake_locate(_page, _target, _hints, _ctx, _limit):
        return _FakeLoc(log, "target")

    monkeypatch.setattr(web, "_locate", _fake_locate)
    page = _FakePage(log)
    ctx = ExecutionContext()
    ctx.page = page
    r = await web_el_action(
        {"target": "css=input.x",
         "interaction": {"open": "click", "text": "99", "close": "none",
                         "after": "Tab", "wait_ms": 0}},
        ctx,
    )
    assert r.ok is True
    assert "after:Tab" in r.data["steps"]


async def test_el_action_rejects_readonly_and_iframe(monkeypatch):
    """只读元素不点；需要 iframe 进出的明确拒绝，不乱点。"""
    from nodes import el_action
    import nodes.web as web

    class _Entry(dict):
        def __init__(self, inter):
            super().__init__(hints={"interaction": inter})

    ctx = ExecutionContext()
    ctx.page = _FakePage([])

    monkeypatch.setattr(web, "_library_entry", lambda k, c: _Entry({"readonly": True}))
    r = await el_action.web_el_action({"key": "db_heading"}, ctx)
    assert r.ok is True and r.data["readonly"] is True

    monkeypatch.setattr(web, "_library_entry", lambda k, c: _Entry({"requires": "iframe_in_out"}))
    r2 = await el_action.web_el_action({"key": "qn_main_img_slot"}, ctx)
    assert r2.ok is False and "iframe" in r2.error


class _AutoPage:
    """点击目标后弹出浮层；Escape 关掉。用来验证运行时自动观测。"""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.overlay = 0
        self.keyboard = self

    def locator(self, sel: str) -> _FakeLoc:
        if "overlay" in sel or "dialog" in sel or "listbox" in sel:
            return _FakeLoc(self.log, sel, self.overlay)
        if sel == "div.options-item":
            return _FakeLoc(self.log, sel, 3 if self.overlay else 0)
        return _FakeLoc(self.log, sel, 1)

    def get_by_text(self, _t: str, **_kw) -> _FakeLoc:
        return _FakeLoc(self.log, "text", 0)

    async def type(self, text: str, **_kw) -> None:
        self.log.append("type:" + text)

    async def press(self, key: str) -> None:
        self.log.append("key:" + key)
        if key == "Escape":
            self.overlay = 0


async def test_el_action_auto_observes_overlay(monkeypatch):
    """默认兜底配方不用人确认：自己看到浮层 -> 自动选第一项 -> 自动试出关法。"""
    import nodes.web as web
    from nodes.el_action import web_el_action

    log: list[str] = []
    page = _AutoPage(log)

    class _Trigger:
        def __init__(self) -> None:
            self.first = self

        async def click(self, **_kw) -> None:
            log.append("click:target")
            page.overlay = 1

    async def _fake_locate(_p, _t, _h, _c, _l):
        return _Trigger()

    monkeypatch.setattr(web, "_locate", _fake_locate)
    ctx = ExecutionContext()
    ctx.page = page
    r = await web_el_action(
        {"target": "css=x",
         "interaction": {"open": "click", "close": "none", "wait_ms": 0,
                         "source": "default", "auto": True}},
        ctx,
    )
    assert r.ok is True
    obs = r.data["observed"]
    assert obs["overlay_after_open"] == 1 and obs["options"] == 3
    assert obs["close_method"] == "escape" and obs["overlay_after_close"] == 0
    assert any("choose:first-option" in s for s in r.data["steps"])
    assert "auto-close:escape" in r.data["steps"]


def test_observation_upgrades_default_to_measured(tmp_path, monkeypatch):
    """两次一致观测 -> 自动升级，不需要人确认。"""
    from elements.store import ElementEntry, ElementStore
    from nodes.el_action import _record_observation

    p = tmp_path / "e.json"
    st = ElementStore(str(p))
    st.add(ElementEntry(
        key="x", role="button", name="x", css="#x",
        hints={"interaction": {"source": "default", "auto": True, "observations": 0}},
    ))
    st.save()
    monkeypatch.setenv("RPA_ELEMENTS", str(p))

    _record_observation("x", {"overlay_after_open": 1}, "escape", True)
    assert ElementStore(str(p)).get("x").hints["interaction"]["observations"] == 1
    _record_observation("x", {"overlay_after_open": 1}, "escape", True)
    it = ElementStore(str(p)).get("x").hints["interaction"]
    assert it["observations"] == 2 and it["source"] == "measured"
    assert it["close"] == "escape" and it["choose"] == "first-option"
