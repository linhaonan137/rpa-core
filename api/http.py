"""HTTP 入口：把流程发布成端点。

  POST /run/{name}     执行流程，body 的键直接注入成变量
  GET  /flows          列出可用流程
  GET  /runs           最近执行记录
  GET  /health         健康检查

鉴权：环境变量 RPA_TOKEN 非空时，要求 `X-RPA-Token` 头或 `?token=` 一致。
默认只绑 127.0.0.1（见 main.py 的启动参数）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from engine.context import ContextError, ExecutionContext
from engine.paths import DB_PATH, FLOWS_DIR
from engine.registry import registry
from engine.runner import RunOptions, Runner
from engine.schema import Flow


def create_app(
    flow_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    *,
    token: str | None = None,
) -> FastAPI:
    # 节点必须显式加载：CLI 走 main.py 会加载，但直接 import api.http 起服务
    # 时 registry 是空的（health 报 nodes=0，任何流程都跑不起来）
    import nodes as _nodes

    _nodes.load_all()

    app = FastAPI(title="RPA 内核", version="0.1.0")
    flow_path = Path(flow_dir) if flow_dir is not None else FLOWS_DIR
    db_file = Path(db_path) if db_path is not None else DB_PATH
    flow_path.mkdir(parents=True, exist_ok=True)
    _token = token if token is not None else os.getenv("RPA_TOKEN", "")

    # 延迟导入，避免没装 sqlite 依赖时整个服务起不来
    from store.db import DB

    db = DB(db_file)

    # ---------------------------------------------------------- 鉴权

    def _check(request: Request) -> None:
        if not _token:
            return
        got = request.headers.get("X-RPA-Token") or request.query_params.get("token")
        if got != _token:
            raise HTTPException(401, "token 无效或缺失")

    # ---------------------------------------------------------- 流程读写

    def _load(name: str) -> Flow:
        p = flow_path / f"{name}.json"
        if not p.exists():
            raise HTTPException(404, f"流程不存在: {name}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"流程 JSON 解析失败: {exc}") from exc
        try:
            return Flow(**raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"流程校验失败: {exc}") from exc

    # ---------------------------------------------------------- 路由

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "nodes": len(registry),
            "flows": len(list(flow_path.glob("*.json"))),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    @app.get("/flows")
    async def list_flows(request: Request) -> dict[str, Any]:
        _check(request)
        items = []
        for p in sorted(flow_path.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                items.append({"name": p.stem, "title": raw.get("name"), "nodes": len(raw.get("nodes") or [])})
            except Exception:  # noqa: BLE001
                items.append({"name": p.stem, "error": "解析失败"})
        return {"flows": items}

    @app.post("/run/{name}")
    async def run_flow(name: str, request: Request) -> JSONResponse:
        _check(request)
        flow = _load(name)

        # body 的键直接当变量（这是"参数化执行"的唯一入口）
        init_vars: dict[str, Any] = {}
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                body = await request.json()
                if isinstance(body, dict):
                    init_vars = body
        except Exception:  # noqa: BLE001
            init_vars = {}

        run_id = f"r-{uuid.uuid4().hex[:8]}"
        ctx = ExecutionContext(run_id=run_id)
        for k, v in init_vars.items():
            ctx.set_var(k, v, scope="global")
        for var in flow.vars:
            if not ctx.has_var(var.name):  # body 传的优先
                ctx.set_var(var.name, var.value, scope="global")

        try:
            result = await Runner(flow, ctx, RunOptions()).execute()
        except ContextError as exc:
            raise HTTPException(400, f"上下文错误: {exc}") from exc

        db.save_run(
            run_id,
            flow.name,
            bool(result["ok"]),
            result["nodes"],
            stopped=bool(result.get("stopped")),
            steps=int(result.get("steps", 0)),
            ms=float(result.get("ms", 0)),
        )
        return JSONResponse(
            {
                "run_id": run_id,
                "ok": result["ok"],
                "ms": result["ms"],
                "failed": result["failed"],
                "nodes": [
                    {"id": n["node_id"], "type": n["type"], "ok": n["ok"], "ms": n["ms"]}
                    for n in result["nodes"]
                ],
            }
        )

    @app.post("/run-async/{name}")
    async def run_flow_async_route(name: str, request: Request) -> JSONResponse:
        """异步跑：立刻返回 task_id，不等执行完（长流程 / 并发场景用这个）。"""
        _check(request)
        _load(name)  # 校验流程存在
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        variables = body if isinstance(body, dict) else {}

        from engine.taskqueue import task_queue

        if not getattr(task_queue, "_runner", None):
            from api.mcp import _queue_runner

            task_queue.bind(_queue_runner)
        out = task_queue.submit(name, variables)
        return JSONResponse(out)

    @app.get("/task/{task_id}")
    async def task_status(task_id: str, request: Request) -> dict[str, Any]:
        """查异步任务状态。queued / running / done / failed / cancelled。"""
        _check(request)
        from engine.taskqueue import task_queue

        return task_queue.get(task_id, full=True)

    @app.get("/tasks")
    async def task_list(request: Request, limit: int = 20) -> dict[str, Any]:
        """队列概览：并发度 + 各状态计数 + 最近任务。"""
        _check(request)
        from engine.taskqueue import task_queue

        return task_queue.list_recent(limit=limit)

    @app.get("/runs")
    async def recent_runs(request: Request, limit: int = 20) -> dict[str, Any]:
        _check(request)
        return {"runs": db.recent_runs(limit=limit)}

    @app.get("/trend")
    async def trend(request: Request, flow: str | None = None) -> dict[str, Any]:
        _check(request)
        return {"trend": db.trend(flow=flow)}

    return app


# ---------------------------------------------------------------- 便捷启动


def serve(host: str = "127.0.0.1", port: int = 8100, **kw: Any) -> None:
    import uvicorn

    uvicorn.run(create_app(**kw), host=host, port=port, log_level="info")
