"""串行任务队列：让长流程不阻塞调用方。

为什么必须有队列而不是纯并发：
  浏览器是**独占资源**（一个 ctx 一套 page），两个流程同时跑会抢同一个页面，
  结果互相踩。所以默认并发度 1，任务排队串行执行。

能力：
  submit()  提交任务，立刻返回 task_id（不等执行）
  get()     查任务状态 / 结果
  list()    看队列概览（含排队中的）
  cancel()  取消还在排队的任务（已开始跑的不能取消，会说明原因）

任务状态：queued → running → done | failed | cancelled
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

MAX_KEEP = 100  # 保留最近 100 个任务记录


class TaskRecord:
    __slots__ = ("id", "flow", "vars", "status", "created", "started",
                 "finished", "result", "error", "position")

    def __init__(self, task_id: str, flow: str, variables: dict[str, Any]) -> None:
        self.id = task_id
        self.flow = flow
        self.vars = variables
        self.status = "queued"
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None
        self.error: str = ""
        self.position = 0

    def to_dict(self, full: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task_id": self.id,
            "flow": self.flow,
            "status": self.status,
            "queue_position": self.position if self.status == "queued" else 0,
            "waited_s": round((self.started or time.time()) - self.created, 2),
        }
        if self.started:
            d["ran_s"] = round((self.finished or time.time()) - self.started, 2)
        if self.error:
            d["error"] = self.error
        if full and self.result is not None:
            d["result"] = self.result
        return d


class TaskQueue:
    """进程内串行队列。MCP / HTTP 两个入口共用同一个实例。"""

    def __init__(self, max_concurrency: int = 1) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self._sem = asyncio.Semaphore(self.max_concurrency)
        self._records: dict[str, TaskRecord] = {}
        self._order: list[str] = []
        self._runner: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
        self._running = 0
        # 活跃的 asyncio.Task 句柄：执行中要取消就靠它
        self._live: dict[str, asyncio.Task] = {}

    def bind(self, runner: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]) -> None:
        """绑定真正的执行函数（避免 engine 反向 import api）。"""
        self._runner = runner

    # ---------------------------------------------------------- 提交

    def submit(self, flow: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._runner is None:
            return {"ok": False, "error": "队列未绑定执行器（内部错误）"}
        tid = f"t-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        rec = TaskRecord(tid, flow, dict(variables or {}))
        rec.position = sum(1 for r in self._records.values() if r.status == "queued") + 1
        self._records[tid] = rec
        self._order.append(tid)
        self._trim()
        self._live[tid] = asyncio.create_task(self._run(rec))
        return {
            "ok": True,
            "task_id": tid,
            "status": "queued",
            "queue_position": rec.position,
            "message": f"已入队（第 {rec.position} 位），用 flow_task(task_id='{tid}') 查进度",
        }

    async def _run(self, rec: TaskRecord) -> None:
        try:
            async with self._sem:
                if rec.status in ("cancelled", "cancelling"):
                    rec.status = "cancelled"
                    return
                rec.status = "running"
                rec.started = time.time()
                rec.position = 0
                self._running += 1
                try:
                    assert self._runner is not None
                    rec.result = await self._runner(rec.flow, rec.vars)
                    rec.status = "done" if rec.result.get("ok") else "failed"
                    if not rec.result.get("ok"):
                        rec.error = str(rec.result.get("error") or "流程执行失败")
                except asyncio.CancelledError:
                    # 用户在执行中取消：这里接住，标记 cancelled 后继续往外抛
                    rec.status = "cancelled"
                    rec.error = "执行中被取消"
                    raise
                except Exception as exc:  # noqa: BLE001
                    rec.status = "failed"
                    rec.error = f"{type(exc).__name__}: {exc}"
                finally:
                    rec.finished = rec.finished or time.time()
                    self._running = max(0, self._running - 1)
        except asyncio.CancelledError:
            # 排队中被取消（还在等 semaphore）：同上
            if rec.status not in ("done", "failed"):
                rec.status = "cancelled"
                rec.error = rec.error or "排队中被取消"
            rec.finished = rec.finished or time.time()
            raise
        finally:
            self._live.pop(rec.id, None)
            self._reindex()

    # ---------------------------------------------------------- 查询

    def get(self, task_id: str, full: bool = True) -> dict[str, Any]:
        rec = self._records.get(task_id)
        if rec is None:
            return {"ok": False, "error": f"没有这个 task_id: {task_id}",
                    "recent": [r.to_dict() for r in self._tail(5)]}
        out = {"ok": True, **rec.to_dict(full=full)}
        if rec.status in ("done", "failed") and rec.result:
            out["failed_nodes"] = rec.result.get("failed") or []
            out["run_id"] = rec.result.get("run_id")
            out["ms"] = rec.result.get("ms")
        return out

    def list_recent(self, limit: int = 20, full: bool = False) -> dict[str, Any]:
        items = [r.to_dict(full=full) for r in self._tail(limit)]
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
        for r in self._records.values():
            counts[r.status] = counts.get(r.status, 0) + 1
        return {
            "ok": True,
            "concurrency": self.max_concurrency,
            "running": self._running,
            "counts": counts,
            "tasks": items,
        }

    def cancel(self, task_id: str) -> dict[str, Any]:
        rec = self._records.get(task_id)
        if rec is None:
            return {"ok": False, "error": f"没有这个 task_id: {task_id}"}

        if rec.status == "queued":
            rec.status = "cancelled"
            rec.finished = time.time()
            rec.error = "排队中被取消"
            t = self._live.pop(task_id, None)
            if t is not None and not t.done():
                t.cancel()
            self._reindex()
            return {"ok": True, "status": "cancelled", "message": f"{task_id} 已取消（还没开始跑）"}

        if rec.status == "running":
            t = self._live.get(task_id)
            if t is None or t.done():
                return {"ok": False, "error": "任务在运行但没有活跃句柄（内部状态异常）"}
            t.cancel()  # asyncio 会在下一个 await 点抛 CancelledError
            rec.status = "cancelling"
            return {
                "ok": True,
                "status": "cancelling",
                "message": f"{task_id} 已请求中断，当前节点会在最近的 await 点停下",
                "hint": "页面可能停在操作中途（表单填一半等），这和节点报错留下的状态一样",
            }

        return {
            "ok": False,
            "error": f"任务已是 {rec.status}，不需要取消",
        }

    # ---------------------------------------------------------- 内部

    def _tail(self, n: int) -> list[TaskRecord]:
        out = []
        for tid in reversed(self._order):
            rec = self._records.get(tid)
            if rec is not None:
                out.append(rec)
            if len(out) >= n:
                break
        return out

    def _reindex(self) -> None:
        pos = 0
        for tid in self._order:
            rec = self._records.get(tid)
            if rec is not None and rec.status == "queued":
                pos += 1
                rec.position = pos

    def _trim(self) -> None:
        if len(self._order) <= MAX_KEEP:
            return
        drop = len(self._order) - MAX_KEEP
        for tid in self._order[:drop]:
            self._records.pop(tid, None)
        self._order = self._order[drop:]


#: 进程级单例：MCP 与 HTTP 共用（同一个浏览器，所以共用一个队列）
task_queue = TaskQueue(max_concurrency=1)
