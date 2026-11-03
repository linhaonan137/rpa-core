"""定时调度器：不引第三方依赖，标准库轮询。

  · add/remove/jobs 持久化到 flows/_schedule.json（JSON，启动时加载）
  · due() 到期判定接收 now 参数（测试可控），不直接取系统时间
  · tick() 检查全部 job，到期的执行：加载 Flow → 注入 vars → Runner → DB.save_run
    同一流程正在跑时跳过本轮（防重叠）；单个失败落 DB 后继续跑下一个
  · serve_forever() 每隔 interval_sec 轮询一次，直到 should_stop

job 说明：
  every_minutes > 0：距 last_run 超过 N 分钟即到期（首次立即跑）
  daily_at "HH:MM" ：当前 HH:MM 等于 daily_at 且今天还没跑过
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import nodes
from engine.context import ExecutionContext
from engine.paths import DB_PATH, FLOWS_DIR
from engine.runner import RunOptions, Runner
from engine.schema import Flow


# ---------------------------------------------------------------- 数据

@dataclass
class Job:
    flow: str                 # 流程名（flows/<flow>.json）
    every_minutes: int = 0    # 每 N 分钟跑一次（0=不用这个）
    daily_at: str = ""        # "HH:MM" 每天定点
    enabled: bool = True
    last_run: str = ""        # ISO 时间，用于去重
    vars: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 调度器

class FlowScheduler:
    def __init__(
        self,
        flow_dir: str | Path | None = None,
        db_path: str | Path | None = None,
        jobs_path: str | Path | None = None,
    ) -> None:
        self.flow_dir = Path(flow_dir) if flow_dir is not None else FLOWS_DIR
        self.db_path = Path(db_path) if db_path is not None else DB_PATH
        self.jobs_path = (
            Path(jobs_path) if jobs_path is not None
            else self.flow_dir / "_schedule.json"
        )
        self.should_stop = False
        self._jobs: dict[str, Job] = {}
        self._running: set[str] = set()   # 正在跑的 flow 名（防重叠）
        self._db: Any = None              # 懒加载的 DB
        self._load()

    # ------------------------------------------------------ 持久化

    def _load(self) -> None:
        if not self.jobs_path.exists():
            return
        try:
            raw = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in raw:
            try:
                job = Job(**item)
            except TypeError:
                continue
            self._jobs[job.flow] = job

    def _save(self) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(j) for j in self._jobs.values()]
        self.jobs_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------ job 管理

    def add(self, job: Job) -> None:
        if not job.flow:
            raise ValueError("job.flow 不能为空")
        if job.every_minutes < 0:
            raise ValueError("every_minutes 不能为负")
        if job.daily_at:
            _check_hhmm(job.daily_at)
        if job.every_minutes == 0 and not job.daily_at:
            raise ValueError("every_minutes 与 daily_at 至少要配一个")
        self._jobs[job.flow] = job
        self._save()

    def remove(self, flow: str) -> None:
        self._jobs.pop(flow, None)
        self._save()

    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    # ------------------------------------------------------ 到期判定

    def due(self, job: Job, now: datetime) -> bool:
        if not job.enabled:
            return False
        if job.every_minutes > 0:
            if not job.last_run:
                return True
            last = datetime.fromisoformat(job.last_run)
            return (now - last).total_seconds() >= job.every_minutes * 60
        if job.daily_at:
            if now.strftime("%H:%M") != job.daily_at:
                return False
            if not job.last_run:
                return True
            last = datetime.fromisoformat(job.last_run)
            return last.date() != now.date()
        return False

    # ------------------------------------------------------ 执行

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """检查全部 job，跑到期且未在跑的。返回 [{flow, ok, error}]。"""
        now = now or datetime.now()
        try:
            nodes.load_all()
        except Exception:  # noqa: BLE001 - 节点加载失败不该拖垮调度
            pass

        results: list[dict[str, Any]] = []
        dirty = False
        for job in self._jobs.values():
            if not self.due(job, now):
                continue
            if job.flow in self._running:   # 防重叠：正在跑就跳过本轮
                continue
            self._running.add(job.flow)
            try:
                r = await self._run_job(job, now)
            except Exception as exc:  # noqa: BLE001 - 单个失败不能中断调度
                r = {"flow": job.flow, "ok": False,
                     "error": f"{type(exc).__name__}: {exc}"}
            finally:
                self._running.discard(job.flow)
            results.append(r)
            job.last_run = now.isoformat(timespec="seconds")  # 去重，成败都记
            dirty = True
        if dirty:
            self._save()
        return results

    async def _run_job(self, job: Job, now: datetime) -> dict[str, Any]:
        """跑单个 job：加载流程 → 注入变量 → Runner → 落 DB。"""
        run_id = f"sched-{job.flow}-{now.strftime('%Y%m%d%H%M%S')}"
        try:
            flow = self._load_flow(job.flow)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            self._save_run(run_id, job.flow, False, [], error=error)
            return {"flow": job.flow, "ok": False, "error": error}

        ctx = ExecutionContext(run_id=run_id)
        for k, v in job.vars.items():
            ctx.set_var(k, v, scope="global")
        for var in flow.vars:
            if not ctx.has_var(var.name):
                ctx.set_var(var.name, var.value, scope="global")

        try:
            result = await Runner(flow, ctx, RunOptions()).execute()
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            self._save_run(run_id, flow.name, False, [], error=error)
            return {"flow": job.flow, "ok": False, "error": error}

        ok = bool(result.get("ok"))
        failed = result.get("failed") or []
        error = "" if ok else f"失败节点: {', '.join(map(str, failed))}"
        self._save_run(
            run_id, flow.name, ok, result.get("nodes", []),
            stopped=bool(result.get("stopped")),
            steps=int(result.get("steps", 0)), ms=float(result.get("ms", 0)),
            error=error or None,
        )
        return {"flow": job.flow, "ok": ok, "error": error}

    def _load_flow(self, name: str) -> Flow:
        p = self.flow_dir / f"{name}.json"
        if not p.exists():
            raise FileNotFoundError(f"流程文件不存在: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        return Flow(**raw)

    def _save_run(self, run_id: str, flow: str, ok: bool,
                  node_logs: list[dict], **kw: Any) -> None:
        try:
            from store.db import DB

            if self._db is None:
                self._db = DB(self.db_path)
            self._db.save_run(run_id, flow, ok, node_logs, **kw)
        except Exception:  # noqa: BLE001 - 落盘失败不能中断调度
            pass

    # ------------------------------------------------------ 常驻

    async def serve_forever(self, interval_sec: int = 60) -> None:
        """循环 tick，直到 should_stop。"""
        while not self.should_stop:
            await self.tick()
            for _ in range(max(1, interval_sec * 10)):
                if self.should_stop:
                    break
                await asyncio.sleep(0.1)

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = None


# ---------------------------------------------------------------- 工具

def _check_hhmm(s: str) -> None:
    import re

    if not re.fullmatch(r"\d{2}:\d{2}", s):
        raise ValueError(f"daily_at 应为零填充的 HH:MM（如 08:30）: {s}")
    h, m = int(s[:2]), int(s[3:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"daily_at 时间越界: {s}")
