"""下载器节点：yt-dlp 四件套 + 加密流（N_m3u8DL-RE）。

两条硬规则：
  1. **二进制缺失必须 fail**——以前最贵的 bug 是 yt-dlp 不在 PATH，
     subprocess 抛 FileNotFoundError 被宽 except 吞掉，节点显示"成功"，
     下游拿着不存在的文件路径继续跑。
  2. **不猜输出文件名**——yt-dlp 的 `-o` 模板能带 `%(title)s`，
     真实文件名只有 yt-dlp 自己知道，所以只回 stdout 摘要 + 目录，
     不伪造一个"应该叫什么"的路径。

参数命名（与其他节点一致）：
  path     输出目录（缺省当前目录）
  save_as  输出文件名（缺省用标题）；media_info 里是结果变量名
  var      结果变量名（下载类默认 media_result，避免和文件名混用）
  kind     音轨格式 / 容器格式等"变体"，各节点自行解释
  limit_ms 超时（毫秒）
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any

from engine.context import ExecutionContext
from engine.registry import Result, register

# ---------------------------------------------------------------- 常量

DEFAULT_TIMEOUT_MS = 1_800_000  # 30 分钟；播放列表用 2 小时
DEFAULT_ENV_YTDLP = "RPA_YTDLP"
DEFAULT_ENV_M3U8DL = "RPA_M3U8DL"

_STDERR_TAIL = 400


class ToolMissing(RuntimeError):
    """外部二进制找不到 —— 必须是 fail，不能静默跳过。"""


# ---------------------------------------------------------------- 工具定位


def _resolve_tool(explicit: str, env_key: str, default: str, install_hint: str) -> str:
    """按 args → env → 默认名 的顺序找二进制，再走 PATH。

    candidate 带路径时要求文件真实存在；裸名字走 shutil.which。
    """
    candidate = explicit.strip() or os.environ.get(env_key, "").strip() or default

    if os.path.dirname(candidate):  # 带路径
        if not os.path.isfile(candidate):
            raise ToolMissing(f"找不到 {candidate}（来源: args 或 {env_key}）。{install_hint}")
        return candidate

    found = shutil.which(candidate)
    if not found:
        raise ToolMissing(
            f"系统里没有 {candidate}。{install_hint}\n"
            f"  也可用环境变量 {env_key} 指向 exe 的绝对路径。"
        )
    return found


def _tool_from_args(args: dict[str, Any], key: str, env_key: str, default: str, hint: str) -> str:
    return _resolve_tool(str(args.get(key) or ""), env_key, default, hint)


def _ytdlp(args: dict[str, Any]) -> str:
    return _tool_from_args(
        args,
        "ytdlp",
        DEFAULT_ENV_YTDLP,
        "yt-dlp",
        "安装：pip install yt-dlp（或下载 yt-dlp.exe 放到 PATH）。",
    )


def _m3u8dl(args: dict[str, Any]) -> str:
    return _tool_from_args(
        args,
        "m3u8dl",
        DEFAULT_ENV_M3U8DL,
        "N_m3u8DL-RE",
        "安装：从 github.com/nilaoda/N_m3u8DL-RE 下载可执行文件并放进 PATH。",
    )


# ---------------------------------------------------------------- 执行


async def _run(argv: list[str], timeout_ms: int) -> tuple[int, str, str]:
    """跑子进程，返回 (returncode, stdout, stderr)。

    argv[0] 不存在 → ToolMissing（不尝试"当成成功"）。
    """
    timeout_s = (timeout_ms or DEFAULT_TIMEOUT_MS) / 1000
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ToolMissing(f"二进制无法执行: {argv[0]}（{exc}）") from exc
    except OSError as exc:
        raise ToolMissing(f"二进制启动失败: {argv[0]}（{exc}）") from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TimeoutError(f"下载超时 {timeout_s:.0f}s：{argv[0]}") from None

    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


def _tail(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _STDERR_TAIL:
        return text
    return "..." + text[-_STDERR_TAIL:]


def _failed(rc: int, out: str, err: str, node: str) -> Result:
    detail = _tail(err) or _tail(out) or "(无输出)"
    return Result.fail(f"{node} 失败：退出码 {rc}；{detail}")


def _out_dir(args: dict[str, Any]) -> str:
    from engine.paths import OUT_DIR, resolve_path

    raw = str(args.get("path") or "").strip()
    p = resolve_path(raw) if raw else OUT_DIR
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _template(args: dict[str, Any], default: str = "%(title)s [%(id)s].%(ext)s") -> str:
    """`-o` 完整模板。save_as 给了就精确指定，否则用默认模板。"""
    save_as = str(args.get("save_as") or "").strip()
    base = _out_dir(args)
    if save_as:
        return os.path.join(base, save_as)
    return os.path.join(base, default)


def _info_out(
    ctx: ExecutionContext, args: dict[str, Any], data: Any, default: str = "media_result"
) -> str:
    """结果同时放进 data 和变量。

    注意下载类节点的 `save_as` 已经是**文件名**，所以变量名单独用 `var`，
    不然会出现"变量名叫 a.mp4"这种脏数据。
    """
    name = str(args.get("var") or default)
    ctx.set_var(name, data)
    return name


def _timeout(args: dict[str, Any], default: int = DEFAULT_TIMEOUT_MS) -> int:
    raw = args.get("limit_ms")
    if raw in (None, "", 0):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 节点


@register("media_get")
async def media_get(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """单视频下载。args: {url, path?, save_as=文件名, var=变量, quality=best, kind=容器, limit_ms?}"""
    url = str(ctx.resolve(args.get("url")) or "").strip()
    if not url:
        return Result.fail("media_get 缺 url")

    try:
        exe = _ytdlp(args)
    except ToolMissing as exc:
        return Result.fail(str(exc))

    quality = str(args.get("quality") or "best")
    argv = [
        exe,
        "--no-playlist",
        "--newline",
        "-f",
        quality if quality != "best" else "bv*+ba/b",
        "--merge-output-format",
        str(args.get("kind") or "mp4"),
        "-o",
        _template(args),
        url,
    ]

    try:
        rc, out, err = await _run(argv, _timeout(args))
    except ToolMissing as exc:
        return Result.fail(str(exc))
    except TimeoutError as exc:
        return Result.fail(str(exc))

    if rc != 0:
        return _failed(rc, out, err, "media_get")

    info = {"url": url, "dir": _out_dir(args), "quality": quality, "stdout": _tail(out)}
    name = _info_out(ctx, args, info)
    return Result.done(f"下载完成 → {info['dir']}（存 {name}）", data=info)


@register("media_audio")
async def media_audio(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """只抽音轨。args: {url, path?, save_as=文件名, var=变量, kind=mp3, limit_ms?}"""
    url = str(ctx.resolve(args.get("url")) or "").strip()
    if not url:
        return Result.fail("media_audio 缺 url")

    try:
        exe = _ytdlp(args)
    except ToolMissing as exc:
        return Result.fail(str(exc))

    fmt = str(args.get("kind") or args.get("audio_format") or "mp3")
    argv = [
        exe,
        "--no-playlist",
        "--newline",
        "-x",
        "--audio-format",
        fmt,
        "--audio-quality",
        str(args.get("quality") or "0"),
        "-o",
        _template(args),
        url,
    ]

    try:
        rc, out, err = await _run(argv, _timeout(args))
    except ToolMissing as exc:
        return Result.fail(str(exc))
    except TimeoutError as exc:
        return Result.fail(str(exc))

    if rc != 0:
        return _failed(rc, out, err, "media_audio")

    info = {"url": url, "dir": _out_dir(args), "format": fmt, "stdout": _tail(out)}
    name = _info_out(ctx, args, info)
    return Result.done(f"音轨已抽出（{fmt}）→ {info['dir']}（存 {name}）", data=info)


@register("media_playlist")
async def media_playlist(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """播放列表批量下载。args: {url, path?, save_as?, count=上限, skip_existing=true, limit_ms?}

    skip_existing 用 yt-dlp 的 archive 文件实现，断点续传靠它。
    """
    url = str(ctx.resolve(args.get("url")) or "").strip()
    if not url:
        return Result.fail("media_playlist 缺 url")

    try:
        exe = _ytdlp(args)
    except ToolMissing as exc:
        return Result.fail(str(exc))

    out_dir = _out_dir(args)
    argv = [exe, "--yes-playlist", "--newline"]

    if bool(args.get("skip_existing", True)):
        argv += ["--download-archive", os.path.join(out_dir, ".download-archive.txt")]

    try:
        max_items = int(args.get("count") or args.get("max_items") or 0)
    except (TypeError, ValueError):
        return Result.fail("media_playlist.count 必须是整数")
    if max_items > 0:
        argv += ["--max-downloads", str(max_items)]

    if bool(args.get("audio_only", False)):
        argv += ["-x", "--audio-format", str(args.get("kind") or "mp3")]

    argv += ["-o", _template(args, "%(playlist_title)s/%(playlist_index)s - %(title)s.%(ext)s")]
    argv.append(url)

    try:
        rc, out, err = await _run(argv, _timeout(args, 7_200_000))
    except ToolMissing as exc:
        return Result.fail(str(exc))
    except TimeoutError as exc:
        return Result.fail(str(exc))

    if rc != 0:
        return _failed(rc, out, err, "media_playlist")

    info = {
        "url": url,
        "dir": out_dir,
        "max_items": max_items,
        "skip_existing": bool(args.get("skip_existing", True)),
        "stdout": _tail(out),
    }
    name = _info_out(ctx, args, info)
    return Result.done(f"播放列表下载完成 → {out_dir}（存 {name}）", data=info)


@register("media_info")
async def media_info(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """只取元数据，不下载。args: {url, save_as=media_info, kind=单个字段名?}

    kind 给了就只把该字段写进变量（常用：kind=title 直接拿标题）。
    """
    url = str(ctx.resolve(args.get("url")) or "").strip()
    if not url:
        return Result.fail("media_info 缺 url")

    try:
        exe = _ytdlp(args)
    except ToolMissing as exc:
        return Result.fail(str(exc))

    argv = [exe, "--no-playlist", "--no-warnings", "--dump-single-json", url]

    try:
        rc, out, err = await _run(argv, _timeout(args, 120_000))
    except ToolMissing as exc:
        return Result.fail(str(exc))
    except TimeoutError as exc:
        return Result.fail(str(exc))

    if rc != 0:
        return _failed(rc, out, err, "media_info")

    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        return Result.fail(f"media_info 解析 yt-dlp 输出失败: {exc}；首 200 字: {out[:200]!r}")

    # 只留可用元数据，丢掉 formats 等大块（几十 KB，塞进变量没意义）
    keys = (
        "id", "title", "duration", "view_count", "like_count", "comment_count",
        "upload_date", "uploader", "channel", "tags", "thumbnail", "is_live",
        "webpage_url", "description", "extractor",
    )
    data = {k: raw.get(k) for k in keys if k in raw}
    data["resolution"] = raw.get("resolution") or (
        f"{raw.get('width')}x{raw.get('height')}" if raw.get("width") else None
    )
    data["fps"] = raw.get("fps")
    data["filesize_approx"] = raw.get("filesize_approx")

    field = str(args.get("kind") or "").strip()
    if field:
        if field not in data:
            return Result.fail(f"media_info 没有字段 {field}；可用: {sorted(data)}")
        name = str(args.get("save_as") or field)
        ctx.set_var(name, data[field])
        return Result.done(f"{field} 已存 {name}", data=data[field])

    name = str(args.get("var") or args.get("save_as") or "media_info")
    ctx.set_var(name, data)
    return Result.done(f"元数据已存 {name}（{data.get('title')}）", data=data)


@register("media_m3u8")
async def media_m3u8(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """加密流下载（N_m3u8DL-RE）。

    args: {url, path?, save_as?, kind=容器格式, decryption_key, referer,
           user_agent, thread_count=8, limit_ms?}
    """
    url = str(ctx.resolve(args.get("url")) or "").strip()
    if not url:
        return Result.fail("media_m3u8 缺 url")

    try:
        exe = _m3u8dl(args)
    except ToolMissing as exc:
        return Result.fail(str(exc))

    out_dir = _out_dir(args)
    save_name = str(args.get("save_as") or "").strip()
    argv = [exe, url, "--save-dir", out_dir, "--auto-select"]

    if save_name:
        argv += ["--save-name", os.path.splitext(save_name)[0]]
    if kind := str(args.get("kind") or "").strip():
        argv += ["--mux-after-done", f"format={kind}"]
    if key := str(ctx.resolve(args.get("decryption_key")) or "").strip():
        argv += ["--key", key]
    if referer := str(args.get("referer") or "").strip():
        argv += ["--header", f"Referer: {referer}"]
    if ua := str(args.get("user_agent") or "").strip():
        argv += ["--header", f"User-Agent: {ua}"]

    extra_headers = args.get("headers")
    if isinstance(extra_headers, dict):
        for k, v in extra_headers.items():
            argv += ["--header", f"{k}: {v}"]

    try:
        threads = int(args.get("thread_count") or 8)
    except (TypeError, ValueError):
        return Result.fail("media_m3u8.thread_count 必须是整数")
    argv += ["--thread-count", str(threads)]

    try:
        rc, out, err = await _run(argv, _timeout(args))
    except ToolMissing as exc:
        return Result.fail(str(exc))
    except TimeoutError as exc:
        return Result.fail(str(exc))

    if rc != 0:
        return _failed(rc, out, err, "media_m3u8")

    # 只统计目录里"刚生成的媒体文件"，用来给下游一个可验证的落盘证据
    produced = sorted(
        f for f in os.listdir(out_dir)
        if os.path.isfile(os.path.join(out_dir, f))
        and os.path.splitext(f)[1].lower() in {".mp4", ".mkv", ".ts", ".m4a", ".flv", ".mp3"}
    )
    info = {
        "url": url,
        "dir": out_dir,
        "files": produced,
        "decrypted": bool(str(args.get("decryption_key") or "").strip()),
        "stdout": _tail(out),
    }
    name = _info_out(ctx, args, info, "m3u8_result")
    if not produced:
        # 退出码 0 但目录里没有媒体文件 → 多半是分片全在缓存里没合并
        return Result.fail(
            f"media_m3u8 退出码 0 但 {out_dir} 没有生成媒体文件（存 {name}）"
        )
    return Result.done(f"加密流下载完成：{produced[-1]}（存 {name}）", data=info)
