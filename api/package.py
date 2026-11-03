"""打包 EXE：调 PyInstaller 把 main.py 打成可执行文件。

  python -c "from api.package import build_exe; print(build_exe())"

流程：
  1. 先探测 pyinstaller 是否可用（不可用直接 fail，提示 pip install）
  2. 拼命令：--onefile --name <exe_name> --distpath <workdir> main.py
     include_flows 时用 --add-data 把 flows 目录带进去（Windows 分隔符是分号）
  3. subprocess 执行，capture 输出，600s 超时
  4. 成功返回 {ok, exe, size_mb}；失败返回 {ok:False, error, output(尾部2000字符)}

真正调 PyInstaller 的部分抽成 _run_pyinstaller(cmd)，测试 mock 它，不真打包。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from engine.paths import PROJECT_ROOT

_TIMEOUT_SEC = 600


# ---------------------------------------------------------------- 探测

def _pyinstaller_available() -> tuple[bool, str]:
    """检查 pyinstaller 可用性。返回 (是否可用, 说明)。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return False, "python 环境里找不到 PyInstaller 模块"
    except subprocess.TimeoutExpired:
        return False, "pyinstaller --version 超时"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        return False, detail[-500:] if detail else f"退出码 {r.returncode}"
    return True, (r.stdout or "").strip()


# ---------------------------------------------------------------- 真正执行

def _run_pyinstaller(cmd: list[str]) -> tuple[int, str]:
    """跑 PyInstaller 命令，返回 (退出码, 合并输出)。测试 mock 这个。

    cwd 固定在项目根：命令里的 ``main.py`` / ``flows`` 都是相对项目根的写法，
    这样从任何目录调用都不会找错文件。
    """
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT_SEC,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        out = _as_text(exc.stdout) + _as_text(exc.stderr)
        return 124, f"打包超时（{_TIMEOUT_SEC}s）\n{out}"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _as_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


# ---------------------------------------------------------------- 入口

def build_exe(
    exe_name: str = "rpa",
    workdir: str | Path | None = None,
    *,
    onefile: bool = True,
    include_flows: bool = True,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """用 PyInstaller 打 main.py，成功返回 {ok, exe, size_mb}。"""
    ok, detail = _pyinstaller_available()
    if not ok:
        return {"ok": False, "error": f"需要 pip install pyinstaller（{detail}）"}

    # 相对路径一律基于项目根，而不是进程 cwd
    wd = Path(workdir) if workdir is not None else PROJECT_ROOT / "dist"
    if not wd.is_absolute():
        wd = PROJECT_ROOT / wd
    workdir = str(wd)

    cmd: list[str] = [sys.executable, "-m", "PyInstaller"]
    if onefile:
        cmd.append("--onefile")
    cmd += ["--name", exe_name, "--distpath", workdir]
    if include_flows:
        sep = os.pathsep  # Windows 是分号
        cmd.append(f"--add-data=flows{sep}flows")
    # playwright 的 driver（node + cli.js）是运行时下载的二进制，静态分析找不到，
    # 不 collect 的话 EXE 里 web_open 会失败（只带 python 包没用）
    cmd += ["--collect-all", "playwright"]
    cmd += list(extra_args or [])
    cmd.append("main.py")

    rc, output = _run_pyinstaller(cmd)
    if rc != 0:
        return {
            "ok": False,
            "error": f"PyInstaller 失败（退出码 {rc}）",
            "output": output[-2000:],
        }

    exe = Path(workdir) / (f"{exe_name}.exe" if os.name == "nt" else exe_name)
    if not exe.exists():
        return {
            "ok": False,
            "error": f"PyInstaller 报成功但找不到产物: {exe}",
            "output": output[-2000:],
        }
    return {
        "ok": True,
        "exe": str(exe),
        "size_mb": round(exe.stat().st_size / 1024 / 1024, 1),
    }


# ---------------------------------------------------------------- spec

def build_spec_text() -> str:
    """返回推荐的 .spec 文件内容（手动 `pyinstaller rpa.spec` 用）。"""
    sep = os.pathsep
    return f"""\
# -*- mode: python ; coding: utf-8 -*-
# 用法：pyinstaller rpa.spec
# 推荐用 spec 而不是长命令行：可进版本库、参数好审、CI 可复现。

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('flows', 'flows')],   # 把流程目录带进包（sep={sep!r} 等价于命令行 --add-data）
    hiddenimports=[],
    hookspath=[],
    excludes=[],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='rpa',
    console=True,        # CLI 工具保留控制台；做 GUI 时改 False
    upx=False,
)
"""
