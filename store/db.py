"""SQLite 落盘：执行记录、节点级耗时、业务数据 UPSERT、趋势统计。

为什么不用 Excel：没有真追加、并发会坏文件、104 万行上限、类型会丢。
SQLite 用 `ON CONFLICT DO UPDATE` 一句话解决"存在就更新，不存在就插入"。

表设计：
  runs       一次执行一条（run_id 主键）
  node_runs  每节点一条（run_id + node_id 复合键），用于 P95 统计
  items      业务数据，由调用方给表名与主键，支持 UPSERT
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import time
from typing import Any, Iterable, Sequence

from engine.paths import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    flow       TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    stopped    INTEGER DEFAULT 0,
    steps      INTEGER DEFAULT 0,
    ms         REAL DEFAULT 0,
    started_at TEXT NOT NULL,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS node_runs (
    run_id   TEXT NOT NULL,
    node_id  TEXT NOT NULL,
    type     TEXT NOT NULL,
    ok       INTEGER NOT NULL,
    ms       REAL NOT NULL,
    attempts INTEGER DEFAULT 1,
    error    TEXT,
    PRIMARY KEY (run_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_node_runs_type ON node_runs(type);
CREATE INDEX IF NOT EXISTS idx_runs_flow ON runs(flow);
"""


class DBError(Exception):
    pass


class DB:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        path = os.fspath(DB_PATH if path is None else path)
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------- 执行记录

    def save_run(
        self,
        run_id: str,
        flow: str,
        ok: bool,
        nodes: Sequence[dict[str, Any]] = (),
        *,
        stopped: bool = False,
        steps: int = 0,
        ms: float = 0.0,
        started_at: str | None = None,
        error: str | None = None,
    ) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO runs(run_id, flow, ok, stopped, steps, ms, started_at, error)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                 ok=excluded.ok, stopped=excluded.stopped, steps=excluded.steps,
                 ms=excluded.ms, error=excluded.error""",
            (run_id, flow, int(ok), int(stopped), steps, ms, started_at or _now(), error),
        )
        for n in nodes:
            cur.execute(
                """INSERT INTO node_runs(run_id, node_id, type, ok, ms, attempts, error)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(run_id, node_id) DO UPDATE SET
                     ok=excluded.ok, ms=excluded.ms,
                     attempts=excluded.attempts, error=excluded.error""",
                (
                    run_id,
                    n.get("node_id", "?"),
                    n.get("type", "?"),
                    int(bool(n.get("ok"))),
                    float(n.get("ms", 0)),
                    int(n.get("attempts", 1)),
                    n.get("error"),
                ),
            )
        self.conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def recent_runs(self, limit: int = 20, flow: str | None = None) -> list[dict[str, Any]]:
        if flow:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE flow=? ORDER BY started_at DESC LIMIT ?", (flow, limit)
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- 趋势统计

    def trend(self, flow: str | None = None, node_type: str | None = None) -> list[dict[str, Any]]:
        """按节点类型统计耗时分布与成功率 —— tuning_rules 的输入。"""
        sql = """
            SELECT n.type,
                   COUNT(*) AS runs,
                   SUM(n.ok) AS ok_runs,
                   AVG(n.ms) AS avg_ms,
                   MIN(n.ms) AS min_ms,
                   MAX(n.ms) AS max_ms,
                   AVG(n.attempts) AS avg_attempts
            FROM node_runs n
            JOIN runs r ON r.run_id = n.run_id
        """
        where, params = [], []
        if flow:
            where.append("r.flow = ?")
            params.append(flow)
        if node_type:
            where.append("n.type = ?")
            params.append(node_type)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY n.type ORDER BY runs DESC"

        out: list[dict[str, Any]] = []
        for row in self.conn.execute(sql, params):
            d = dict(row)
            ms_list = [
                r["ms"]
                for r in self.conn.execute(
                    "SELECT ms FROM node_runs WHERE type=?" + (" AND run_id IN (SELECT run_id FROM runs WHERE flow=?)" if flow else ""),
                    (d["type"], flow) if flow else (d["type"],),
                )
            ]
            d["p50_ms"] = round(_pct(ms_list, 50), 1)
            d["p95_ms"] = round(_pct(ms_list, 95), 1)
            d["ok_rate"] = round((d["ok_runs"] or 0) / d["runs"], 3) if d["runs"] else 0.0
            d["avg_ms"] = round(d["avg_ms"] or 0, 1)
            out.append(d)
        return out

    def suggest_timeout(self, node_type: str, factor: float = 2.0, floor_ms: int = 3_000) -> int | None:
        """按 P95 × factor 给出建议超时；没有数据返回 None。"""
        for t in self.trend(node_type=node_type):
            if t["p95_ms"]:
                return max(floor_ms, int(t["p95_ms"] * factor / 1000) * 1000)
        return None

    def top_failures(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT type, COUNT(*) AS fails,
                      (SELECT error FROM node_runs x
                        WHERE x.type=n.type AND x.ok=0 AND x.error IS NOT NULL
                        ORDER BY x.ms DESC LIMIT 1) AS sample_error
               FROM node_runs n WHERE ok=0 GROUP BY type
               ORDER BY fails DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- 业务数据

    def upsert(self, table: str, rows: Iterable[dict[str, Any]], key: str = "id") -> int:
        """把一批字典写进指定表：存在就更新，不存在就插入。

        表不存在时按第一批数据的键自动建表（全部 TEXT/REAL 宽松类型）。
        """
        rows = list(rows)
        if not rows:
            return 0
        self._ensure_table(table, rows[0], key)

        cols = list(rows[0].keys())
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != key)
        if updates:
            tail = f"ON CONFLICT({_qi(key)}) DO UPDATE SET {updates}"
        else:
            # 只有主键列时没有可更新的，用 DO NOTHING（否则 SET 后为空会语法错误）
            tail = f"ON CONFLICT({_qi(key)}) DO NOTHING"
        sql = (
            f"INSERT INTO {_qi(table)}({','.join(_qi(c) for c in cols)}) VALUES({placeholders}) "
            f"{tail}"
        )
        cur = self.conn.cursor()
        cur.executemany(sql, [tuple(_norm(r.get(c)) for c in cols) for r in rows])
        self.conn.commit()
        return cur.rowcount

    def query(self, table: str, where: str = "", params: Sequence[Any] = (), limit: int = 0) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {_qi(table)}"
        if where:
            sql += f" WHERE {where}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def delete(self, table: str, where: str, params: Sequence[Any] = ()) -> int:
        if not where.strip():
            raise DBError("delete 必须给 where 条件（防全表删除）")
        cur = self.conn.execute(f"DELETE FROM {_qi(table)} WHERE {where}", params)
        self.conn.commit()
        return cur.rowcount

    def tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        return [r["name"] for r in rows]

    def _ensure_table(self, table: str, sample: dict[str, Any], key: str) -> None:
        if table in self.tables():
            return
        if key not in sample:
            raise DBError(f"upsert 需要主键列 {key}，但数据里没有")
        cols = []
        for k, v in sample.items():
            t = "REAL" if isinstance(v, (int, float)) and not isinstance(v, bool) else "TEXT"
            cols.append(f'{_qi(k)} {t}')
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_qi(table)} ({','.join(cols)}, PRIMARY KEY({_qi(key)}))"
        )
        self.conn.commit()

    # ---------------------------------------------------------- 收尾

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------- 工具


def _norm(v: Any) -> Any:
    """把 dict/list 序列化成 JSON 字符串，None 保持 None。"""
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def _qi(name: str) -> str:
    """给标识符加引号，防保留字冲突。"""
    if not name or not all(c.isalnum() or c == "_" for c in name):
        raise DBError(f"非法表名/列名: {name!r}")
    return f'"{name}"'


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    vals = sorted(values)
    return float(vals[lo] + (vals[hi] - vals[lo]) * (k - lo))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def open_db(path: str | os.PathLike[str] | None = None) -> DB:
    return DB(path)
