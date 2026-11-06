"""HTTP 入口：把流程发布成端点（n8n / 其它编排器调这个，不走 MCP）。

  POST /run/{name}          同步执行，body 的键直接注入成变量
  POST /run-async/{name}    异步提交，立刻返回 task_id
  GET  /task/{task_id}      查异步任务（full=True 带节点明细）
  GET  /tasks               队列概览
  GET  /flows               列出可用流程
  GET  /flow/{name}         读单个流程定义 + 上次执行
  GET  /elements            元素库清单（hits / misses / stale）
  GET  /runs                最近执行记录
  GET  /trend               各节点耗时趋势（P95）
  GET  /health              健康检查（唯一不校验 token 的）

鉴权：环境变量 RPA_TOKEN 非空时，要求 `X-RPA-Token` 头或 `?token=` 一致。
默认只绑 127.0.0.1（见 main.py 的启动参数）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from engine.context import ContextError, ExecutionContext
from engine.paths import DB_PATH, ELEMENTS_PATH, FLOWS_DIR
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

    # ---------------------------------------------------------- 共享浏览器
    #
    # 为什么需要：`_ensure_page` 只看 `ctx.browser`，而 HTTP 每次请求都新建
    # ExecutionContext → 每次请求都 launch 一个新浏览器（2~3 秒 + 登录态全丢）。
    # 这里在服务层维护一个长驻浏览器，所有请求复用同一个 context
    # （cookie / localStorage 都在这层，登录一次后续都带着）。

    async def _attach_shared(ctx: ExecutionContext) -> Any:
        """把共享浏览器挂到本次请求的 ctx 上，返回共享 context。

        真正的实现在 api/mcp.py（`_attach_shared_browser`）—— 那边 flow_run
        内部也要用同一份，不能在这里各起一套。
        """
        from api.mcp import _SHARED_BROWSER, _attach_shared_browser

        await _attach_shared_browser(ctx)
        return _SHARED_BROWSER["context"]

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
        return {"ok": True, "flows": items}

    @app.post("/run/{name}")
    async def run_flow(name: str, request: Request, keep_pages: bool = False) -> JSONResponse:
        """跑一个流程。

        `?keep_pages=1` 保留本次新开的标签页 —— 交互探索时要看结果页；
        默认关掉（n8n 无人跑，不关就积标签）。
        """
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

        from engine.browser_lock import BrowserBusy, browser_lock

        context = await _attach_shared(ctx)
        pages_before = list(getattr(context, "pages", None) or [])
        try:
            # 跨进程独占：MCP 服务 / 另一个并发请求正在用这只浏览器时排队等。
            # 不加锁两边会抢同一个标签页（互相导航、互相关页 → 随机"元素找不到"）。
            async with browser_lock():
                result = await Runner(flow, ctx, RunOptions()).execute()
        except ContextError as exc:
            raise HTTPException(400, f"上下文错误: {exc}") from exc
        except BrowserBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            # 关掉本次请求新开的标签页：复用浏览器但别让标签越积越多。
            # keep_pages=1 时不关 —— 交互探索要看到流程最后停在哪个页面。
            if not keep_pages:
                for _p in list(getattr(context, "pages", None) or []):
                    if _p not in pages_before:
                        try:
                            await _p.close()
                        except Exception:  # noqa: BLE001
                            pass

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
                # 业务结果回传：流程里 save_as 存的变量（draft_hits / filled_title…）。
                # 只看 ok 不知道"草稿真的进库了没有"，n8n 靠这个字段判断业务成功。
                "vars": {
                    k: v
                    for k, v in (ctx.all_vars() or {}).items()
                    if isinstance(v, (str, int, float, bool)) or v is None
                },
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
        # 不用在这里挂浏览器：_queue_runner -> flow_run 内部会挂进程级共享浏览器
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

    @app.get("/flow/{name}")
    async def flow_detail(name: str, request: Request) -> dict[str, Any]:
        """读单个流程的完整定义（UI 用：显示节点、变量、上次结果）。"""
        _check(request)
        flow = _load(name)
        # 用外层的 db（DB 没有 last_run 方法，原来 hasattr 判断恒为 False → 永远返回 null）
        last = None
        try:
            rows = db.recent_runs(limit=1, flow=flow.name)
            last = rows[0] if rows else None
        except Exception:  # noqa: BLE001
            last = None
        return {
            "ok": True,
            "name": flow.name,
            "version": flow.version,
            "nodes": [
                {"id": n.id, "type": n.type, "label": n.label or n.type, "off": n.off}
                for n in flow.nodes
            ],
            "vars": [{"name": v.name, "value": v.value} for v in flow.vars],
            "node_count": len(flow.nodes),
            "last_run": last,
        }

    @app.get("/elements")
    async def list_elements(request: Request) -> dict[str, Any]:
        """元素库清单（UI 用：看拾取了哪些元素、有没有失效）。"""
        _check(request)
        try:
            from elements.store import ElementStore

            store = ElementStore(str(ELEMENTS_PATH))
            items = []
            for k in store.keys():
                e = store.get(k)
                items.append({
                    "key": k,
                    "role": e.role,
                    "name": e.name,
                    # hits/misses 存在 meta 里（store.py 的 property），stale 是算出来的
                    "hits": e.hits,
                    "misses": e.misses,
                    "stale": e.is_stale(),
                })
            return {"ok": True, "count": len(items), "elements": items}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @app.get("/runs")
    async def recent_runs(request: Request, limit: int = 20) -> dict[str, Any]:
        _check(request)
        return {"ok": True, "runs": db.recent_runs(limit=limit)}

    @app.get("/trend")
    async def trend(request: Request, flow: str | None = None) -> dict[str, Any]:
        _check(request)
        return {"ok": True, "trend": db.trend(flow=flow)}

    @app.on_event("shutdown")
    async def _close_shared_browser() -> None:
        """优雅关闭共享浏览器。

        为什么必须有：persistent context 的 cookie 只在 close() 时落盘。
        直接强杀进程 → 内存里的登录态全丢，下次启动还得重新登录。
        """
        from api.mcp import _SHARED_BROWSER

        context = _SHARED_BROWSER.get("context")
        if context is None:
            return
        # 接管来的浏览器（CDP attach / 复用 _ctx）不归我们管：
        # context.close() 对 CDP 默认 context 会退化成"断整个连接"，
        # cookie 也不会因此落盘（profile 由 Ant 自己管）。只有自己 launch 的才关。
        if _SHARED_BROWSER.get("cdp") or _SHARED_BROWSER.get("_pw") is None:
            return
        try:
            await context.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            pass

    return app


# ---------------------------------------------------------------- 便捷启动


def serve(host: str = "127.0.0.1", port: int = 8100, **kw: Any) -> None:
    import uvicorn

    uvicorn.run(create_app(**kw), host=host, port=port, log_level="info")
