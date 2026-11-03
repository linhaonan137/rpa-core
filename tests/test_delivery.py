"""交付模块测试：api/scheduler.py 定时器 + api/package.py 打包。

原则：全部用 tmp_path，不真跑 PyInstaller（mock _run_pyinstaller），
不真执行流程（mock api.scheduler.Runner），到期判定用注入的 now。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import api.package as package
import api.scheduler as scheduler_mod
import nodes
from api.package import build_exe, build_spec_text
from api.scheduler import FlowScheduler, Job


# ---------------------------------------------------------------- 工具

_MIN_FLOW = {"name": "f1", "nodes": [{"id": "n1", "type": "noop"}], "edges": []}


def _write_flow(flow_dir: Path, name: str = "f1") -> None:
    flow_dir.mkdir(parents=True, exist_ok=True)
    data = dict(_MIN_FLOW, name=name)
    (flow_dir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


class _FakeResult(dict):
    pass


class _FakeRunner:
    """替身 Runner：记录收到的 flow / ctx，行为可编程。"""

    instances: list["_FakeRunner"] = []
    behavior: str = "ok"           # ok | fail
    raise_flows: set[str] = set()  # 这些流程名在 execute 时抛异常

    def __init__(self, flow, ctx, opts=None):
        self.flow = flow
        self.ctx = ctx
        self.instances.append(self)

    async def execute(self):
        cls = type(self)
        if self.flow.name in cls.raise_flows:
            raise RuntimeError("runner 爆炸")
        if cls.behavior == "fail":
            return _FakeResult(ok=False, stopped=False, ms=1.0, steps=1,
                               nodes=[{"node_id": "n1", "type": "noop",
                                       "ok": False, "ms": 1.0, "attempts": 1}],
                               failed=["n1"], scheduler={})
        return _FakeResult(ok=True, stopped=False, ms=1.0, steps=1,
                           nodes=[], failed=[], scheduler={})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """标准环境：tmp 下的 flows 目录 + jobs 文件 + mock Runner / load_all。"""
    flow_dir = tmp_path / "flows"
    _write_flow(flow_dir, "f1")
    _write_flow(flow_dir, "f2")
    sched = FlowScheduler(
        flow_dir=str(flow_dir),
        db_path=str(tmp_path / "rpa.db"),
        jobs_path=str(flow_dir / "_schedule.json"),
    )
    monkeypatch.setattr(scheduler_mod, "Runner", _FakeRunner)
    monkeypatch.setattr(nodes, "load_all", lambda: {})
    _FakeRunner.instances.clear()
    _FakeRunner.behavior = "ok"
    _FakeRunner.raise_flows = set()
    yield {"sched": sched, "flow_dir": flow_dir, "tmp": tmp_path,
           "runner": _FakeRunner}
    sched.close()


def _db(sched: FlowScheduler):
    from store.db import DB

    return DB(sched.db_path)


# ================================================================ scheduler

class TestPersist:
    def test_add_and_reload(self, env):
        sched: FlowScheduler = env["sched"]
        sched.add(Job(flow="f1", every_minutes=5, vars={"k": "v"}))
        sched.add(Job(flow="f2", daily_at="08:30"))

        # 重新实例化应从 JSON 加载回来
        sched2 = FlowScheduler(
            flow_dir=str(env["flow_dir"]),
            db_path=str(env["tmp"] / "rpa.db"),
            jobs_path=str(env["flow_dir"] / "_schedule.json"),
        )
        jobs = {j.flow: j for j in sched2.jobs()}
        assert jobs["f1"].every_minutes == 5
        assert jobs["f1"].vars == {"k": "v"}
        assert jobs["f2"].daily_at == "08:30"
        assert jobs["f1"].enabled is True
        sched2.close()

    def test_add_upsert_same_flow(self, env):
        sched: FlowScheduler = env["sched"]
        sched.add(Job(flow="f1", every_minutes=5))
        sched.add(Job(flow="f1", daily_at="09:00"))
        assert len(sched.jobs()) == 1
        assert sched.jobs()[0].daily_at == "09:00"

    def test_remove_persists(self, env):
        sched: FlowScheduler = env["sched"]
        sched.add(Job(flow="f1", every_minutes=5))
        sched.add(Job(flow="f2", every_minutes=5))
        sched.remove("f1")
        sched2 = FlowScheduler(
            flow_dir=str(env["flow_dir"]),
            db_path=str(env["tmp"] / "rpa.db"),
            jobs_path=str(env["flow_dir"] / "_schedule.json"),
        )
        assert [j.flow for j in sched2.jobs()] == ["f2"]
        sched2.close()

    def test_add_validates(self, env):
        sched: FlowScheduler = env["sched"]
        with pytest.raises(ValueError):
            sched.add(Job(flow="f1"))                    # 什么都没配
        with pytest.raises(ValueError):
            sched.add(Job(flow="f1", daily_at="25:00"))  # 时间越界
        with pytest.raises(ValueError):
            sched.add(Job(flow="f1", daily_at="8:30"))   # 格式错


class TestDue:
    def test_every_minutes(self, env):
        sched: FlowScheduler = env["sched"]
        job = Job(flow="f1", every_minutes=5)
        now = datetime(2026, 11, 3, 10, 0, 0)

        assert sched.due(job, now) is True          # 从未跑过 → 立即到期

        job.last_run = (now - timedelta(minutes=4, seconds=59)).isoformat()
        assert sched.due(job, now) is False         # 还没到 5 分钟

        job.last_run = (now - timedelta(minutes=5)).isoformat()
        assert sched.due(job, now) is True          # 边界：刚好 N 分钟

        job.last_run = (now - timedelta(minutes=10)).isoformat()
        assert sched.due(job, now) is True

    def test_disabled_never_due(self, env):
        sched: FlowScheduler = env["sched"]
        job = Job(flow="f1", every_minutes=5, enabled=False)
        assert sched.due(job, datetime.now()) is False

    def test_daily_at(self, env):
        sched: FlowScheduler = env["sched"]
        job = Job(flow="f1", daily_at="08:30")

        assert sched.due(job, datetime(2026, 11, 3, 8, 30, 0)) is True
        assert sched.due(job, datetime(2026, 11, 3, 8, 31, 0)) is False
        assert sched.due(job, datetime(2026, 11, 3, 8, 29, 59)) is False

        # 今天已经跑过 → 不再跑；昨天跑过、今天到点 → 再跑
        job.last_run = "2026-11-03T08:30:00"
        assert sched.due(job, datetime(2026, 11, 3, 8, 30, 30)) is False
        job.last_run = "2026-11-02T08:30:00"
        assert sched.due(job, datetime(2026, 11, 3, 8, 30, 30)) is True


class TestTick:
    async def test_runs_due_and_updates_last_run(self, env):
        sched: FlowScheduler = env["sched"]
        sched.add(Job(flow="f1", every_minutes=5))
        sched.add(Job(flow="f2", daily_at="08:30"))
        now = datetime(2026, 11, 3, 8, 30, 0)

        results = await sched.tick(now)

        assert sorted(r["flow"] for r in results) == ["f1", "f2"]
        assert all(r["ok"] for r in results)
        assert all(r["error"] == "" for r in results)
        # last_run 已更新且持久化
        sched2 = FlowScheduler(
            flow_dir=str(env["flow_dir"]),
            db_path=str(env["tmp"] / "rpa.db"),
            jobs_path=str(env["flow_dir"] / "_schedule.json"),
        )
        for j in sched2.jobs():
            assert j.last_run == now.isoformat(timespec="seconds")
        sched2.close()
        # 再 tick 一次同 now：都不该到期（去重）
        assert await sched.tick(now) == []

    async def test_vars_injected(self, env):
        sched: FlowScheduler = env["sched"]
        sched.add(Job(flow="f1", every_minutes=5, vars={"target": "abc"}))
        await sched.tick(datetime(2026, 11, 3, 8, 30, 0))
        runner = _FakeRunner.instances[-1]
        assert runner.ctx.get_var("target") == "abc"

    async def test_runner_failure_recorded_and_continues(self, env):
        """第一个 job 的 Runner 失败：落 DB、报 ok=False，第二个照跑。"""
        sched: FlowScheduler = env["sched"]
        _FakeRunner.raise_flows = {"f1"}
        sched.add(Job(flow="f1", every_minutes=5))
        sched.add(Job(flow="f2", every_minutes=5))

        results = await sched.tick(datetime(2026, 11, 3, 8, 30, 0))

        by_flow = {r["flow"]: r for r in results}
        assert by_flow["f1"]["ok"] is False
        assert "RuntimeError" in by_flow["f1"]["error"]
        assert by_flow["f2"]["ok"] is True   # 失败没有中断调度
        # 失败也落了 DB（ok=0）
        db = _db(sched)
        rows = db.recent_runs(flow="f1")
        assert rows and rows[0]["ok"] == 0
        db.close()

    async def test_flow_missing_recorded_in_db(self, env):
        sched: FlowScheduler = env["sched"]
        sched.add(Job(flow="nope", every_minutes=5))
        results = await sched.tick(datetime(2026, 11, 3, 8, 30, 0))
        assert results == [{"flow": "nope", "ok": False,
                            "error": results[0]["error"]}]
        assert "nope" in results[0]["error"]
        db = _db(sched)
        rows = db.recent_runs(flow="nope")
        assert rows and rows[0]["ok"] == 0
        db.close()

    async def test_result_fail_reported(self, env):
        """Runner 正常返回但 result.ok=False → 任务算失败。"""
        sched: FlowScheduler = env["sched"]
        _FakeRunner.behavior = "fail"
        sched.add(Job(flow="f1", every_minutes=5))
        results = await sched.tick(datetime(2026, 11, 3, 8, 30, 0))
        assert results[0]["ok"] is False
        assert "n1" in results[0]["error"]

    async def test_no_overlap(self, env):
        """job 正在跑时，重入的 tick 必须跳过它。"""
        sched: FlowScheduler = env["sched"]
        sched.add(Job(flow="f1", every_minutes=5))
        reentrant_results: list = []
        calls = 0

        real_cls = scheduler_mod.Runner

        class OverlapRunner(real_cls):
            async def execute(self):
                nonlocal calls
                calls += 1
                # 重入 tick：f1 正在跑，应被跳过
                reentrant_results.append(await sched.tick(datetime(2026, 11, 3, 8, 30, 0)))
                return _FakeResult(ok=True, stopped=False, ms=1.0, steps=1,
                                   nodes=[], failed=[], scheduler={})

        monkey = pytest.MonkeyPatch()
        monkey.setattr(scheduler_mod, "Runner", OverlapRunner)
        try:
            results = await sched.tick(datetime(2026, 11, 3, 8, 30, 0))
        finally:
            monkey.undo()

        assert calls == 1
        assert reentrant_results == [[]]     # 重入那轮什么都没跑
        assert results[0]["ok"] is True


class TestServeForever:
    async def test_stops_on_flag(self, env):
        """should_stop 置位后循环退出；tick 被调用过至少一次。"""
        sched: FlowScheduler = env["sched"]
        sched.add(Job(flow="f1", every_minutes=5))
        ran = []

        async def fake_tick(now=None):
            ran.append(1)
            sched.should_stop = True   # 跑一轮后要求停
            return []

        sched.tick = fake_tick  # type: ignore[method-assign]
        await sched.serve_forever(interval_sec=1)  # 0.1s 步进的休眠可接受
        assert ran


# ================================================================ package

def _fake_available_ok(monkeypatch) -> None:
    monkeypatch.setattr(package, "_pyinstaller_available", lambda: (True, "6.0.0"))


class TestBuildExe:
    def test_pyinstaller_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            package, "_pyinstaller_available", lambda: (False, "no module")
        )
        r = build_exe(workdir=str(tmp_path / "dist"))
        assert r["ok"] is False
        assert "pip install pyinstaller" in r["error"]

    def test_command_assembly(self, monkeypatch, tmp_path):
        _fake_available_ok(monkeypatch)
        captured: dict = {}

        def fake_run(cmd):
            captured["cmd"] = cmd
            dist = tmp_path / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "myrpa.exe").write_bytes(b"x" * 2048)
            return 0, "build ok"

        monkeypatch.setattr(package, "_run_pyinstaller", fake_run)
        r = build_exe(exe_name="myrpa", workdir=str(tmp_path / "dist"),
                      extra_args=["--clean"])

        cmd = captured["cmd"]
        assert r["ok"] is True
        assert "--onefile" in cmd
        assert cmd[cmd.index("--name") + 1] == "myrpa"
        assert cmd[cmd.index("--distpath") + 1] == str(tmp_path / "dist")
        # --add-data 带 flows 目录，分隔符正确
        add_data = next(c for c in cmd if c.startswith("--add-data"))
        src, dst = add_data.split("=", 1)[1].split(os.pathsep)
        assert src == "flows" and dst == "flows"
        assert "--clean" in cmd
        assert cmd[-1] == "main.py"
        # 返回值
        assert r["exe"].endswith("myrpa.exe")
        assert r["size_mb"] == 0.0  # 2048B ≈ 0.0MB

    def test_no_flows_no_add_data(self, monkeypatch, tmp_path):
        _fake_available_ok(monkeypatch)
        captured: dict = {}

        def fake_run(cmd):
            captured["cmd"] = cmd
            dist = tmp_path / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "rpa.exe").write_bytes(b"x" * 1024 * 1024)
            return 0, ""

        monkeypatch.setattr(package, "_run_pyinstaller", fake_run)
        r = build_exe(workdir=str(tmp_path / "dist"), include_flows=False)
        assert r["ok"] is True
        assert not any(c.startswith("--add-data") for c in captured["cmd"])

    def test_run_pyinstaller_fail_path(self, monkeypatch, tmp_path):
        _fake_available_ok(monkeypatch)
        long_out = "x" * 5000 + "FATAL_ERROR"
        monkeypatch.setattr(
            package, "_run_pyinstaller", lambda cmd: (1, long_out)
        )
        r = build_exe(workdir=str(tmp_path / "dist"))
        assert r["ok"] is False
        assert "退出码 1" in r["error"]
        assert len(r["output"]) == 2000          # 只留尾部 2000 字符
        assert r["output"].endswith("FATAL_ERROR")

    def test_missing_artifact(self, monkeypatch, tmp_path):
        _fake_available_ok(monkeypatch)
        monkeypatch.setattr(package, "_run_pyinstaller", lambda cmd: (0, "ok"))
        r = build_exe(workdir=str(tmp_path / "dist"))
        assert r["ok"] is False
        assert "找不到产物" in r["error"]


class TestSpec:
    def test_spec_content(self):
        spec = build_spec_text()
        assert "'main.py'" in spec
        assert "('flows', 'flows')" in spec
        assert "EXE(" in spec
        assert "name='rpa'" in spec
