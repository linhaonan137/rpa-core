"""统一入口。

  python -m main run flows/demo.json         跑一个流程文件
  python -m main run flows/demo.json -v k=v  带变量
  python -m main serve --port 8100           起 HTTP 服务
  python -m main mcp                         起 MCP 服务（stdio）
  python -m main nodes                       列出已注册节点
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import nodes

# PyInstaller 只认静态 import（它看不到 nodes.load_all() 里的 importlib.import_module），
# 所以这里逐个写死，否则打包后 nodes 子模块全部丢失（实测：No module named 'nodes.web'）
import nodes.ai  # noqa: F401
import nodes.data  # noqa: F401
import nodes.desktop  # noqa: F401
import nodes.files  # noqa: F401
import nodes.media  # noqa: F401
import nodes.net  # noqa: F401
import nodes.picker  # noqa: F401
import nodes.system  # noqa: F401
import nodes.trigger  # noqa: F401
import nodes.web  # noqa: F401

from engine.context import ExecutionContext
from engine.paths import DB_PATH, FLOWS_DIR
from engine.registry import registry
from engine.runner import RunOptions, Runner
from engine.schema import Flow


def _load_flow(path: str) -> Flow:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"流程文件不存在: {path}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    return Flow(**raw)


def _parse_vars(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"变量格式应为 k=v: {item}")
        k, v = item.split("=", 1)
        # 尝试按 JSON 解析（数字/布尔/列表）
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


async def _run(args: argparse.Namespace) -> int:
    nodes.load_all()
    flow = _load_flow(args.file)

    ctx = ExecutionContext(run_id=f"cli-{Path(args.file).stem}")
    for k, v in _parse_vars(args.var).items():
        ctx.set_var(k, v, scope="global")
    for var in flow.vars:
        if not ctx.has_var(var.name):
            ctx.set_var(var.name, var.value, scope="global")

    opts = RunOptions(
        start_at=args.start_at or None,
        step=args.step,
        on_pause=(lambda nid, when: print(f"  ⏸  {nid} [{when}]")) if args.step else None,
    )
    result = await Runner(flow, ctx, opts).execute()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for n in result["nodes"]:
            # 用 ASCII 标记：GBK 控制台编不了 ✓/✗（EXE 实测崩过）
            mark = "[OK]" if n["ok"] else ("[--]" if n["skipped"] else "[X] ")
            extra = f"  {n['error']}" if n["error"] else ""
            print(f"  {mark} {n['node_id']:<14} {n['type']:<18} {n['ms']:>8.1f}ms{extra}")
        print(f"\n{'PASS' if result['ok'] else 'FAIL'}  {result['steps']} 步  {result['ms']}ms")
        if result["failed"]:
            print(f"失败节点: {', '.join(result['failed'])}")

    # 落执行记录
    if not args.no_db:
        try:
            from store.db import DB

            db = DB(args.db)
            db.save_run(
                ctx.run_id, flow.name, bool(result["ok"]), result["nodes"],
                stopped=bool(result.get("stopped")),
                steps=int(result.get("steps", 0)), ms=float(result.get("ms", 0)),
            )
            db.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  (执行记录未落盘: {exc})", file=sys.stderr)

    return 0 if result["ok"] else 1


def _setup_stdio() -> None:
    """Windows 控制台默认 GBK（cp936），打印 ✓/✗/→ 这类符号会 UnicodeEncodeError。

    源码环境常因为终端设了 UTF-8 不暴露，打包成 EXE 后必炸（实测）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 非交互流或老版本不支持
            pass


def _no_args_banner(released: list[str]) -> int:
    """双击 EXE（无参数）时的提示：不让人看到一个 argparse 报错就退出。"""
    from engine.paths import describe

    info = describe()
    lines = [
        "RPA 内核",
        f"  项目根    {info['PROJECT_ROOT']}",
        f"  流程目录  {info['FLOWS_DIR']}",
    ]
    if info["frozen"] == "True":
        lines.append(f"  打包模式  是（解压目录 {info['MEIPASS']}）")
    if released:
        lines.append(f"  已释放    {', '.join(released)} → 项目根（可直接编辑）")
    lines += [
        "",
        "可用命令（在命令行里跑）：",
        "  rpa run flows\\demo.json        跑一个流程",
        "  rpa run flows\\demo.json --step 单步执行",
        "  rpa serve --port 8100          起 HTTP 服务（body 注入变量）",
        "  rpa mcp                        起 MCP 服务（给 Agent 用）",
        "  rpa nodes                      列出已注册节点",
        "  rpa schedule --list            查看定时任务",
        "  rpa build                      重新打包 EXE",
    ]
    print("\n".join(lines))
    try:
        input("\n按回车退出...")
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    from engine.paths import bootstrap_frozen, ensure_dirs

    _setup_stdio()
    # 打包后首次运行：把包内 flows 释放到 exe 旁边（用户可编辑）
    released = bootstrap_frozen()
    ensure_dirs()

    args_in = list(sys.argv[1:] if argv is None else argv)
    if not args_in:
        return _no_args_banner(released)

    ap = argparse.ArgumentParser(prog="rpa", description="RPA 内核")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑一个流程文件")
    r.add_argument("file")
    r.add_argument("-v", "--var", action="append", default=[], help="变量 k=v，可多次")
    r.add_argument("--start-at", default="", help="从某节点开始，跳过上游")
    r.add_argument("--step", action="store_true", help="单步：每个节点前暂停")
    r.add_argument("--json", action="store_true", help="输出完整 JSON")
    r.add_argument("--no-db", action="store_true", help="不落执行记录")
    r.add_argument("--db", default=None, help=f"数据库文件（默认 {DB_PATH}）")

    s = sub.add_parser("serve", help="起 HTTP 服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8100)
    s.add_argument("--flows", default=None, help=f"流程目录（默认 {FLOWS_DIR}）")
    s.add_argument("--db", default=None, help=f"数据库文件（默认 {DB_PATH}）")

    sub.add_parser("mcp", help="起 MCP 服务（stdio）")
    sub.add_parser("nodes", help="列出已注册节点")

    sc = sub.add_parser("schedule", help="定时执行流程（无人值守）")
    sc.add_argument("--flows", default=None, help=f"流程目录（默认 {FLOWS_DIR}）")
    sc.add_argument("--db", default=None, help=f"数据库文件（默认 {DB_PATH}）")
    sc.add_argument("--add", nargs=2, metavar=("FLOW", "EVERY_MIN"), help="新增/更新一个定时任务")
    sc.add_argument("--daily", nargs=2, metavar=("FLOW", "HH:MM"), help="每天定点执行")
    sc.add_argument("--remove", metavar="FLOW", help="删除定时任务")
    sc.add_argument("--list", action="store_true", help="列出定时任务")
    sc.add_argument("--interval", type=int, default=60, help="巡检间隔秒数")

    bp = sub.add_parser("build", help="打包成 EXE")
    bp.add_argument("--name", default="rpa")
    bp.add_argument("--dist", default=None, help="输出目录（相对路径基于项目根）")
    bp.add_argument("--no-flows", action="store_true", help="不把 flows 目录打进包里")

    args = ap.parse_args(args_in)

    if args.cmd == "run":
        return asyncio.run(_run(args))

    if args.cmd == "serve":
        from api.http import serve

        serve(host=args.host, port=args.port, flow_dir=args.flows, db_path=args.db)
        return 0

    if args.cmd == "mcp":
        from api.mcp import main as mcp_main

        mcp_main()
        return 0

    if args.cmd == "schedule":
        from api.scheduler import FlowScheduler, Job

        sch = FlowScheduler(flow_dir=args.flows, db_path=args.db)

        if args.add:
            flow, every = args.add
            sch.add(Job(flow=flow, every_minutes=int(every)))
            print(f"已添加：{flow} 每 {every} 分钟")
        if args.daily:
            flow, at = args.daily
            sch.add(Job(flow=flow, daily_at=at))
            print(f"已添加：{flow} 每天 {at}")
        if args.remove:
            sch.remove(args.remove)
            print(f"已删除：{args.remove}")
        if args.list or not (args.add or args.daily or args.remove):
            jobs = sch.jobs()
            if not jobs:
                print("（没有定时任务。用 --add 流程名 分钟数 添加）")
            for j in jobs:
                when = f"每 {j.every_minutes} 分钟" if j.every_minutes else f"每天 {j.daily_at}"
                print(f"  {j.flow:<24} {when:<18} {'启用' if j.enabled else '停用'}  上次: {j.last_run or '-'}")
            if args.list:
                return 0

        if args.add or args.daily or args.remove:
            return 0

        print(f"开始巡检（每 {args.interval}s 一次，Ctrl+C 停止）")
        import asyncio as _aio

        nodes.load_all()
        try:
            _aio.run(sch.serve_forever(interval_sec=args.interval))
        except KeyboardInterrupt:
            print("已停止")
        return 0

    if args.cmd == "build":
        from api.package import build_exe

        print(f"打包中（约 1 分钟）...")
        r = build_exe(args.name, args.dist, include_flows=not args.no_flows)
        if r.get("ok"):
            print(f"✅ {r['exe']}  {r.get('size_mb', '?')} MB")
            return 0
        print(f"✗ 打包失败: {r.get('error')}")
        if r.get("output"):
            print(r["output"][-800:])
        return 1

    if args.cmd == "nodes":
        nodes.load_all()
        failed = nodes.failures()
        for t in registry.types():
            print(f"  {t:<22} {registry.source_of(t)}")
        print(f"\n共 {len(registry)} 个节点")
        if failed:
            print(f"\n加载失败: {failed}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
