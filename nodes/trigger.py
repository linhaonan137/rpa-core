"""触发器节点：五种「等到事件发生」的阻塞节点。

设计取舍：**轮询，不用事件循环黑魔法**。
  · Webhook 用文件队列（spool 目录）而不是内置 HTTP server ——
    api 层收到请求后落盘，这里轮询取走。解耦、可重放、崩溃不丢事件。
  · api 触发器轮询接口 + 条件判断，200ms~1s 一次，够用且不烧 CPU。
  · 全部遵守同一个约定：`limit_ms` 到点就 fail（超时是失败，不是"跳过"），
    0 表示不限时（只在明确要长期等的时候用）。

轮询间隔默认 200ms，统一 `interval_ms` 可调；测试里调小到 1ms。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from engine.context import ExecutionContext
from engine.registry import Result, register

DEFAULT_POLL_MS = 200
DEFAULT_LIMIT_MS = 30_000

#: 测试注入点：httpx.MockTransport。生产为 None。
_TRANSPORT: httpx.AsyncBaseTransport | None = None

_SPOOL_EXTS = (".json", ".txt", ".csv")


class TriggerError(RuntimeError):
    """可预期的等待类失败（参数错、库缺失）。"""


# ---------------------------------------------------------------- 公共


def _limit_ms(args: dict[str, Any], default: int = DEFAULT_LIMIT_MS) -> int:
    """0 / 缺省语义：缺省用 default；显式 0 表示不限时。"""
    if "limit_ms" not in args or args["limit_ms"] is None:
        return default
    try:
        return max(int(args["limit_ms"]), 0)
    except (TypeError, ValueError):
        return default


def _interval_s(args: dict[str, Any]) -> float:
    try:
        ms = int(args.get("interval_ms") or DEFAULT_POLL_MS)
    except (TypeError, ValueError):
        ms = DEFAULT_POLL_MS
    return max(ms, 1) / 1000


def _deadline(limit_ms: int) -> float | None:
    return None if limit_ms == 0 else time.monotonic() + limit_ms / 1000


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


async def _sleep(seconds: float) -> None:
    """单独包一层，测试可 monkeypatch 掉真实等待。"""
    await asyncio.sleep(seconds)


def _empty(value: Any) -> bool:
    return value is None or value in ("", {}, [], ())


def _save(ctx: ExecutionContext, args: dict[str, Any], default: str, value: Any) -> str:
    name = str(args.get("save_as") or default)
    ctx.set_var(name, value)
    return name


def _interrupted(ctx: ExecutionContext) -> bool:
    return bool(getattr(ctx, "should_stop", False))


# ---------------------------------------------------------------- webhook


@register("trig_hook")
async def trig_hook(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """等外部请求进来（文件队列形式）。

    args:
      path       spool 目录（或单个文件名）
      kind      "json"（默认，能解析就解析）| "text"
      consume   默认 true，取走后删除文件（false = 只读不删，用于调试）
      save_as   默认 webhook_data
      limit_ms  默认 30000；0 = 不限时
    """
    path = str(ctx.resolve(args.get("path")) or "").strip()
    if not path:
        return Result.fail("trig_hook 缺 path（spool 目录或文件）")

    is_dir = os.path.isdir(path) or not os.path.splitext(path)[1]
    if is_dir:
        os.makedirs(path, exist_ok=True)

    consume = bool(args.get("consume", True))
    kind = str(args.get("kind") or "json")
    deadline = _deadline(_limit_ms(args))
    started = time.monotonic()

    while True:
        if _interrupted(ctx):
            return Result.fail("trig_hook 等待被中断")

        entry = _pick_spool_file(path, is_dir)
        if entry:
            try:
                with open(entry, "r", encoding="utf-8", errors="replace") as fh:
                    raw = fh.read()
            except OSError as exc:
                return Result.fail(f"trig_hook 读文件失败: {exc}")

            if kind == "json":
                try:
                    body: Any = json.loads(raw)
                except json.JSONDecodeError as exc:
                    return Result.fail(f"trig_hook 内容不是合法 JSON: {exc}；文件 {entry}")
            else:
                body = raw

            if consume:
                try:
                    os.remove(entry)
                except OSError as exc:
                    return Result.fail(f"trig_hook 取走后删除失败: {exc}")

            data = {
                "body": body,
                "file": entry,
                "waited_ms": round((time.monotonic() - started) * 1000),
            }
            name = _save(ctx, args, "webhook_data", data)
            return Result.done(f"收到 webhook（{os.path.basename(entry)}）→ {name}", data=data)

        if _expired(deadline):
            return Result.fail(
                f"trig_hook 超时：{_limit_ms(args)}ms 内 {path} 没有新事件"
            )
        await _sleep(_interval_s(args))


def _pick_spool_file(path: str, is_dir: bool) -> str | None:
    """取最老的一个待处理文件（FIFO，避免后到的先跑）。"""
    if not is_dir:
        return path if os.path.isfile(path) else None
    try:
        names = [
            n for n in os.listdir(path)
            if os.path.isfile(os.path.join(path, n)) and n.lower().endswith(_SPOOL_EXTS)
        ]
    except OSError:
        return None
    if not names:
        return None
    names.sort(key=lambda n: os.path.getmtime(os.path.join(path, n)))
    return os.path.join(path, names[0])


# ---------------------------------------------------------------- api


@register("trig_api")
async def trig_api(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """轮询一个接口，条件成立才继续。

    args:
      target   接口 URL
      kind     GET（默认）| POST
      headers / body
      path     响应里要看的键路径，如 "data.status"（缺省看整个响应）
      value    期望值（缺省：非空即成立）
      save_as  默认 api_response
      limit_ms / interval_ms
    """
    url = str(ctx.resolve(args.get("target")) or args.get("url") or "").strip()
    if not url:
        return Result.fail("trig_api 缺 target（接口 URL）")

    method = str(args.get("kind") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH"):
        return Result.fail(f"trig_api 不支持的 kind: {method}")

    headers = args.get("headers") if isinstance(args.get("headers"), dict) else None
    body = ctx.resolve(args.get("body"))
    want = ctx.resolve(args.get("value"))
    key_path = str(args.get("path") or "").strip()

    deadline = _deadline(_limit_ms(args))
    started = time.monotonic()
    attempts = 0
    last_err = ""

    while True:
        if _interrupted(ctx):
            return Result.fail("trig_api 轮询被中断")

        attempts += 1
        payload: Any = None
        try:
            payload = await _http_once(url, method, headers, body)
        except TriggerError as exc:
            last_err = str(exc)

        if last_err == "":
            actual = _dig(payload, key_path) if key_path else payload
            if _match(actual, want):
                data = {
                    "value": actual,
                    "response": payload,
                    "attempts": attempts,
                    "waited_ms": round((time.monotonic() - started) * 1000),
                }
                name = _save(ctx, args, "api_response", data)
                return Result.done(f"接口条件成立（第 {attempts} 次）→ {name}", data=data)

        if _expired(deadline):
            detail = f"；最后错误: {last_err}" if last_err else ""
            return Result.fail(
                f"trig_api 超时：{_limit_ms(args)}ms / {attempts} 次仍未满足条件"
                f"（path={key_path or '-'} value={want!r}）{detail}"
            )
        last_err = ""  # 单次失败不终止轮询，下一轮重试
        await _sleep(_interval_s(args))


async def _http_once(
    url: str, method: str, headers: dict[str, Any] | None, body: Any
) -> Any:
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=_TRANSPORT) as client:
            resp = await client.request(method, url, headers=headers, json=body)
    except httpx.RequestError as exc:
        raise TriggerError(f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 400:
        raise TriggerError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _dig(payload: Any, key_path: str) -> Any:
    """按 "a.b.0.c" 取值；取不到返回 None。"""
    cur = payload
    for part in key_path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _match(actual: Any, want: Any) -> bool:
    if want is None or want == "":
        return not _empty(actual)
    if isinstance(want, (dict, list)):
        return actual == want
    if isinstance(actual, bool):
        return actual == (str(want).lower() in ("true", "1", "yes", "是"))
    return str(actual) == str(want)


# ---------------------------------------------------------------- file


@register("trig_file")
async def trig_file(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """等文件出现 / 变化。

    args: target|path, kind=appear（默认）|change|exists, save_as=file_event, limit_ms
    """
    path = str(ctx.resolve(args.get("target") or args.get("path")) or "").strip()
    if not path:
        return Result.fail("trig_file 缺 path（要监控的文件）")

    kind = str(args.get("kind") or "appear")
    if kind not in ("appear", "change", "exists"):
        return Result.fail(f"trig_file 不支持的 kind: {kind}（appear/change/exists）")

    baseline = _stat(path)
    if kind == "change" and baseline is None:
        return Result.fail(f"trig_file kind=change 要求文件已存在: {path}")

    deadline = _deadline(_limit_ms(args))
    started = time.monotonic()

    while True:
        if _interrupted(ctx):
            return Result.fail("trig_file 等待被中断")

        current = _stat(path)
        hit = False
        if kind in ("appear", "exists"):
            hit = current is not None
        elif current is not None and current != baseline:
            # 拷贝中的文件大小还在涨 —— 等稳定（同一状态连续两次观测一致）
            await _sleep(_interval_s(args))
            stable = _stat(path)
            hit = stable == current

        if hit:
            data = {
                "path": path,
                "kind": kind,
                "size": (current or {}).get("size"),
                "mtime": (current or {}).get("mtime"),
                "waited_ms": round((time.monotonic() - started) * 1000),
            }
            name = _save(ctx, args, "file_event", data)
            return Result.done(f"文件事件 {kind}: {path} → {name}", data=data)

        if _expired(deadline):
            return Result.fail(f"trig_file 超时：{_limit_ms(args)}ms 内 {kind} 未发生 → {path}")
        await _sleep(_interval_s(args))


def _stat(path: str) -> dict[str, Any] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"size": st.st_size, "mtime": round(st.st_mtime, 3)}


# ---------------------------------------------------------------- time


@register("trig_time")
async def trig_time(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """流程内等到某个时刻（只等，不负责启动流程）。

    args:
      target    "2026-11-03 18:00:00" / "18:00"（今天或明天） / "18:00:30"
      kind      "datetime"（默认）| "delay"
      delay_ms  kind=delay 时的等待时长
      limit_ms  最长等待，默认 3600000（1 小时）；到点未到时间 → fail
    """
    kind = str(args.get("kind") or "datetime")
    started = time.monotonic()

    if kind == "delay":
        try:
            delay_ms = int(ctx.resolve(args.get("delay_ms")) or 0)
        except (TypeError, ValueError):
            return Result.fail("trig_time.delay_ms 必须是整数")
        if delay_ms <= 0:
            return Result.fail("trig_time kind=delay 需要正数 delay_ms")
        limit = _limit_ms(args, delay_ms + 1000)
        if limit and delay_ms > limit:
            return Result.fail(f"trig_time: delay_ms={delay_ms} 超过 limit_ms={limit}")
        await _sleep(delay_ms / 1000)
        data = {"kind": kind, "delay_ms": delay_ms, "waited_ms": round((time.monotonic() - started) * 1000)}
        name = _save(ctx, args, "time_event", data)
        return Result.done(f"已等待 {delay_ms}ms → {name}", data=data)

    raw = str(ctx.resolve(args.get("target")) or "").strip()
    if not raw:
        return Result.fail("trig_time 缺 target（目标时刻）")
    when = _parse_when(raw)
    if when is None:
        return Result.fail(
            f"trig_time 认不出时间格式: {raw!r}（支持 2026-11-03 18:00:00 / 18:00 / 18:00:30）"
        )

    limit = _limit_ms(args, 3_600_000)
    deadline = _deadline(limit)

    while True:
        if _interrupted(ctx):
            return Result.fail("trig_time 等待被中断")
        remain = (when - datetime.now()).total_seconds()
        if remain <= 0:
            data = {
                "kind": kind,
                "target": raw,
                "at": when.isoformat(sep=" "),
                "waited_ms": round((time.monotonic() - started) * 1000),
            }
            name = _save(ctx, args, "time_event", data)
            return Result.done(f"已到 {when:%Y-%m-%d %H:%M:%S} → {name}", data=data)
        if _expired(deadline):
            return Result.fail(
                f"trig_time 超时：{limit}ms 内没等到 {raw}（还差 {remain:.0f}s）"
            )
        await _sleep(min(remain, max(_interval_s(args), 0.5)))


def _parse_when(raw: str) -> datetime | None:
    """纯时间（HH:MM[:SS]）取最近一次未来时刻；否则按完整时间解析。"""
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        when = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        if when <= now:  # 今天已过 → 明天同一时刻
            when += timedelta(days=1)
        return when
    return None


# ---------------------------------------------------------------- hotkey


@register("trig_hotkey")
async def trig_hotkey(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """等热键（本机场景）。args: target="ctrl+alt+f1", save_as=hotkey_event, limit_ms

    依赖 keyboard 库（pip install keyboard，Windows 可能需要管理员权限）。
    """
    combo = str(ctx.resolve(args.get("target") or args.get("hotkey")) or "").strip()
    if not combo:
        return Result.fail("trig_hotkey 缺 target（如 ctrl+alt+f1）")

    keys = _parse_hotkey(combo)
    if not keys:
        return Result.fail(f"trig_hotkey 解析不了热键: {combo!r}")

    try:
        kb = _keyboard()
    except TriggerError as exc:
        return Result.fail(str(exc))

    deadline = _deadline(_limit_ms(args, 60_000))
    started = time.monotonic()

    while True:
        if _interrupted(ctx):
            return Result.fail("trig_hotkey 等待被中断")
        try:
            pressed = all(bool(kb.is_pressed(k)) for k in keys)
        except Exception as exc:  # noqa: BLE001 - keyboard 在权限不足时会抛
            return Result.fail(f"trig_hotkey 读键盘状态失败: {type(exc).__name__}: {exc}")

        if pressed:
            data = {
                "hotkey": combo,
                "keys": keys,
                "waited_ms": round((time.monotonic() - started) * 1000),
            }
            name = _save(ctx, args, "hotkey_event", data)
            # 等键松开，避免同一次按键触发多轮循环
            await _release(kb, keys)
            return Result.done(f"热键 {combo} 已触发 → {name}", data=data)

        if _expired(deadline):
            return Result.fail(f"trig_hotkey 超时：{_limit_ms(args, 60_000)}ms 内没等到 {combo}")
        await _sleep(_interval_s(args))


def _parse_hotkey(combo: str) -> list[str]:
    alias = {
        "control": "ctrl",
        "cmd": "windows",
        "win": "windows",
        "super": "windows",
        "option": "alt",
        "escape": "esc",
    }
    keys = []
    for part in combo.lower().replace(" ", "").split("+"):
        if not part:
            continue
        keys.append(alias.get(part, part))
    return keys


async def _release(kb: Any, keys: list[str]) -> None:
    """短暂等键松开（最多 3 秒），不做无上限等待。"""
    for _ in range(15):
        try:
            if not any(bool(kb.is_pressed(k)) for k in keys):
                return
        except Exception:  # noqa: BLE001
            return
        await _sleep(0.2)


def _keyboard() -> Any:
    """延迟 import：keyboard 不是必装依赖，缺了不该让整包导入失败。"""
    try:
        import keyboard  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise TriggerError(
            "缺少 keyboard 库：pip install keyboard（Windows 下读全局按键可能需要管理员权限）。"
            f"\n  实际错误: {type(exc).__name__}: {exc}"
        ) from exc
    return keyboard
