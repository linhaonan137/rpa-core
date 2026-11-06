"""引擎模块审计修复的回归测试（2026-11-06）。对应 docs/ENGINE-AUDIT.md：

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
