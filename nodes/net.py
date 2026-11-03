"""网络类节点：HTTP 请求 + 页面接口监听。

这两个是 MCP 工具 `net_http` / `web_network` 的底层实现 —— 之前它们指向
`api_request`/`http_post`/`network_monitor_*` 等**从未存在**的节点，一调就报
"未注册的节点类型"，工具在、能力不在。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from engine.context import ExecutionContext
from engine.registry import Result, register

__all__ = ["net_http", "web_net"]

MAX_BODY = 200_000  # 响应体截断（避免把上下文灌爆）


def _jsonable_body(text: str, ctype: str) -> Any:
    """能解析成 JSON 就给结构，否则给文本（截断）。"""
    if "json" in (ctype or "").lower() or text.strip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001
            pass
    return text[:MAX_BODY]


@register("net_http")
async def net_http(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """发 HTTP 请求（不启浏览器）：args: {url, method=GET, headers?, json?, data?, params?, timeout_ms=30000, save_as, fail_on_4xx=true}

    - 走系统代理（环境变量 HTTP_PROXY/HTTPS_PROXY），和浏览器行为一致
    - 4xx/5xx 默认按失败上报，但 data 仍然给出（含状态码和 body，便于诊断）
    """
    import httpx

    url = str(args.get("url") or "").strip()
    if not url:
        return Result.fail("net_http 缺 url")
    if not url.lower().startswith(("http://", "https://")):
        return Result.fail(f"url 必须是 http(s) 绝对地址: {url[:80]}")

    method = str(args.get("method") or "GET").upper()
    timeout_ms = int(args.get("timeout_ms", 30000) or 30000)

    headers = args.get("headers") or None
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except Exception:  # noqa: BLE001
            return Result.fail("headers 不是合法 JSON 对象")

    kwargs: dict[str, Any] = {"headers": headers}
    if args.get("json") is not None:
        kwargs["json"] = args["json"]
    if args.get("data") is not None:
        kwargs["data"] = args["data"]
        kwargs["json"] = None  # 两者互斥
    if args.get("params") is not None:
        kwargs["params"] = args["params"]

    try:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000, follow_redirects=True) as c:
            r = await c.request(method, url, **{k: v for k, v in kwargs.items() if v is not None})
    except Exception as exc:  # noqa: BLE001
        return Result.fail(f"请求失败：{type(exc).__name__}: {exc}")

    body = _jsonable_body(r.text, r.headers.get("content-type", ""))
    data = {
        "status": r.status_code,
        "ok": r.is_success,
        "headers": dict(r.headers),
        "body": body,
        "elapsed_ms": round(r.elapsed.total_seconds() * 1000, 1),
    }
    if args.get("save_as"):
        ctx.set_var(str(args["save_as"]), data, scope=args.get("scope", "local"))

    if not r.is_success and args.get("fail_on_4xx", True):
        return Result.fail(f"HTTP {r.status_code} {method} {url[:80]}", data=data)
    return Result.done(f"HTTP {r.status_code} {method}（{data['elapsed_ms']}ms）", data=data)


# ---------------------------------------------------------------- 接口监听


def _netlog(ctx: ExecutionContext) -> list[dict[str, Any]]:
    """当前页的响应记录（第一次调用时装监听器）。"""
    log = getattr(ctx, "_net_log", None)
    if log is not None:
        return log

    log = []
    setattr(ctx, "_net_log", log)
    page = ctx.page
    if page is None:
        return log

    def _on_response(resp: Any) -> None:
        try:
            req = resp.request
            log.append({
                "url": resp.url,
                "status": resp.status,
                "method": req.method,
                "resource_type": req.resource_type,
                "at": time.strftime("%H:%M:%S"),
            })
            if len(log) > 500:  # 只留最近 500 条
                del log[: len(log) - 500]
        except Exception:  # noqa: BLE001 - 监听器里不能抛
            pass

    try:
        page.on("response", _on_response)
    except Exception:  # noqa: BLE001
        pass
    return log


@register("web_net")
async def web_net(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """页面接口监听（观察型，不拦截）：args: {action=listen|wait|log, pattern?, limit=50, limit_ms=30000}

    - listen：开始记录（幂等）
    - wait：等某个 URL 包含 pattern 的响应出现（不阻塞页面）
    - log：返回最近记录
    """
    action = str(args.get("action") or "listen").lower()
    log = _netlog(ctx)

    if ctx.page is None:
        return Result.fail("web_net: 没有打开的页面")

    if action == "log":
        limit = int(args.get("limit", 50) or 50)
        recent = log[-limit:]
        return Result.done(f"最近 {len(recent)} 条响应", data=recent)

    if action == "listen":
        return Result.done(f"已开始监听（当前已记录 {len(log)} 条）", data={"listening": True})

    if action == "wait":
        pattern = str(args.get("pattern") or "").strip()
        if not pattern:
            return Result.fail("web_net wait 需要 pattern")
        timeout_ms = int(args.get("limit_ms", 30000) or 30000)
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for item in reversed(log):
                if pattern in item["url"]:
                    if args.get("save_as"):
                        ctx.set_var(str(args["save_as"]), item, scope=args.get("scope", "local"))
                    return Result.done(f"命中 {pattern}（HTTP {item['status']}）", data=item)
            await asyncio.sleep(0.2)
        # 超时时把最近的 URL 给出来，方便判断 pattern 写错了
        hints = [x["url"].split("?")[0][-80:] for x in log[-8:]]
        return Result.fail(
            f"等 {pattern} 超时（{timeout_ms}ms），最近请求: {hints}",
            data={"recent": hints},
        )

    return Result.fail(f"未知 action: {action}（支持 listen/wait/log）")
