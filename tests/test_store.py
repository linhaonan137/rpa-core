"""落盘测试：执行记录、UPSERT、趋势统计、删除保护。"""

from __future__ import annotations

import pytest

from store.db import DB, DBError


@pytest.fixture()
def db(tmp_path) -> DB:
    d = DB(str(tmp_path / "t.db"))
    yield d
    d.close()


# ------------------------------------------------------------ 执行记录


def test_save_and_get_run(db):
    db.save_run("r1", "发票查验", True, steps=5, ms=1234.5)
    got = db.get_run("r1")
    assert got is not None
    assert got["flow"] == "发票查验"
    assert got["ok"] == 1
    assert got["ms"] == pytest.approx(1234.5)


def test_save_run_with_nodes(db):
    nodes = [
        {"node_id": "n1", "type": "web_open", "ok": True, "ms": 800},
        {"node_id": "n2", "type": "web_click", "ok": False, "ms": 30000, "error": "超时"},
    ]
    db.save_run("r1", "f", False, nodes)
    rows = db.query("node_runs", "run_id=?", ("r1",))
    assert len(rows) == 2
    assert rows[0]["type"] == "web_open"


def test_rerun_same_id_updates(db):
    db.save_run("r1", "f", False, steps=1, ms=100)
    db.save_run("r1", "f", True, steps=3, ms=500)
    got = db.get_run("r1")
    assert got["ok"] == 1
    assert got["steps"] == 3


def test_node_upsert_no_duplicate(db):
    n = [{"node_id": "n1", "type": "click", "ok": True, "ms": 10}]
    db.save_run("r1", "f", True, n)
    db.save_run("r1", "f", True, [{"node_id": "n1", "type": "click", "ok": True, "ms": 99}])
    rows = db.query("node_runs", "run_id=?", ("r1",))
    assert len(rows) == 1
    assert rows[0]["ms"] == 99


def test_recent_runs(db):
    for i in range(5):
        db.save_run(f"r{i}", "f", True, started_at=f"2026-11-03T0{i}:00:00")
    assert len(db.recent_runs(limit=3)) == 3
    assert len(db.recent_runs(flow="f")) == 5


# ------------------------------------------------------------ 趋势


def test_trend_p50_p95(db):
    for i in range(10):
        db.save_run(
            f"r{i}", "f", True,
            [{"node_id": "n", "type": "click", "ok": True, "ms": (i + 1) * 100}],
        )
    t = db.trend(node_type="click")
    assert len(t) == 1
    row = t[0]
    assert row["runs"] == 10
    assert row["min_ms"] == 100
    assert row["max_ms"] == 1000
    assert 500 <= row["p50_ms"] <= 600
    assert row["p95_ms"] >= 900


def test_ok_rate(db):
    db.save_run("r1", "f", True, [{"node_id": "n", "type": "t", "ok": True, "ms": 1}])
    db.save_run("r2", "f", False, [{"node_id": "n", "type": "t", "ok": False, "ms": 1}])
    row = db.trend(node_type="t")[0]
    assert row["ok_rate"] == pytest.approx(0.5)


def test_suggest_timeout(db):
    for i in range(10):
        db.save_run(f"r{i}", "f", True, [{"node_id": "n", "type": "slow", "ok": True, "ms": 20000}])
    # P95 ≈ 20000，×2 = 40000 → 向上取整到 40s
    assert db.suggest_timeout("slow") == 40000


def test_suggest_timeout_floor(db):
    db.save_run("r1", "f", True, [{"node_id": "n", "type": "fast", "ok": True, "ms": 50}])
    # P95=50，×2=100 → 低于下限 3000，取 3000
    assert db.suggest_timeout("fast") == 3000


def test_suggest_timeout_none_when_no_data(db):
    assert db.suggest_timeout("never_run") is None


def test_top_failures(db):
    for i in range(3):
        db.save_run(f"r{i}", "f", False, [
            {"node_id": "n", "type": "click", "ok": False, "ms": 100, "error": "元素不存在"},
        ])
    db.save_run("rx", "f", False, [{"node_id": "m", "type": "read", "ok": False, "ms": 1, "error": "x"}])
    top = db.top_failures()
    assert top[0]["type"] == "click"
    assert top[0]["fails"] == 3
    assert top[0]["sample_error"] == "元素不存在"


# ------------------------------------------------------------ 业务 UPSERT


def test_upsert_creates_table(db):
    n = db.upsert("invoices", [{"id": "A1", "amount": 100.5, "no": "001"}])
    assert n == 1
    assert "invoices" in db.tables()
    rows = db.query("invoices")
    assert rows[0]["id"] == "A1"
    assert rows[0]["amount"] == pytest.approx(100.5)


def test_upsert_updates_existing(db):
    db.upsert("invoices", [{"id": "A1", "amount": 100.0}])
    db.upsert("invoices", [{"id": "A1", "amount": 200.0}])
    rows = db.query("invoices")
    assert len(rows) == 1, "同 id 不该产生第二行"
    assert rows[0]["amount"] == pytest.approx(200.0)


def test_upsert_batch(db):
    n = db.upsert("t", [{"id": str(i), "v": i} for i in range(5)])
    assert n == 5
    assert len(db.query("t")) == 5


def test_upsert_requires_key_column(db):
    with pytest.raises(DBError, match="主键列"):
        db.upsert("t", [{"name": "no-id"}])


def test_upsert_serializes_dict(db):
    db.upsert("t", [{"id": "1", "meta": {"a": 1}}])
    assert db.query("t")[0]["meta"] == '{"a": 1}'


# ------------------------------------------------------------ 删除保护


def test_delete_requires_where(db):
    db.upsert("t", [{"id": "1"}])
    with pytest.raises(DBError, match="必须给 where"):
        db.delete("t", "  ")


def test_delete_with_condition(db):
    db.upsert("t", [{"id": "1", "v": 1}, {"id": "2", "v": 2}])
    n = db.delete("t", "v > ?", (1,))
    assert n == 1
    assert len(db.query("t")) == 1


def test_illegal_identifier_rejected(db):
    with pytest.raises(DBError, match="非法"):
        db.query("bad; drop table x")


def test_context_manager(tmp_path):
    with DB(str(tmp_path / "c.db")) as d:
        d.save_run("r", "f", True)
    # 关闭后再用会抛
    with pytest.raises(Exception):
        d.get_run("r")
