"""全项目路径的唯一来源。

禁止在别处写 ``"flows"`` 这种依赖 cwd 的相对路径——所有默认路径都必须
从这里取，保证 MCP 服务、PyInstaller EXE、从任意 cwd 调用时行为一致。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _root() -> Path:
    """项目根（``engine/`` 的上一级）。打包成 EXE 时指向 exe 所在目录。"""
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


#: 项目根目录（绝对路径）。
PROJECT_ROOT: Path = _root()


def _p(env: str, default: str) -> Path:
    """环境变量优先（并绝对化），否则在 ``PROJECT_ROOT`` 下拼默认值。"""
    v = os.getenv(env)
    if v:
        return Path(v).expanduser().resolve()
    return (PROJECT_ROOT / default).resolve()


#: 流程定义目录。
FLOWS_DIR: Path = _p("RPA_FLOWS", "flows")
#: SQLite 数据库文件。
DB_PATH: Path = _p("RPA_DB", "rpa.db")
#: 元素库 JSON 文件。
ELEMENTS_PATH: Path = _p("RPA_ELEMENTS", "elements.json")
#: 运行产物输出目录。
OUT_DIR: Path = _p("RPA_OUT", "out")
#: 流程元数据（版本快照等）子目录名。
META_DIR_NAME = "_meta"
#: 元素库/流程快照子目录名。
VERSIONS_DIR_NAME = ".versions"


def ensure_dirs() -> None:
    """确保运行必需目录存在（幂等）。"""
    for d in (FLOWS_DIR, FLOWS_DIR / META_DIR_NAME, OUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def resolve_path(p: str | Path) -> Path:
    """把用户给的路径变绝对：绝对的保持，相对的基于 PROJECT_ROOT（不跟随 cwd）。

    所有接收路径参数的节点都必须走这里 —— 否则从不同 cwd 调用（MCP / 双击 EXE）
    产物会落到意想不到的地方（实测：MCP 截图落到了客户端的工作目录）。
    """
    pp = Path(str(p)).expanduser()
    return pp.resolve() if pp.is_absolute() else (PROJECT_ROOT / pp).resolve()


class PathNotAllowed(ValueError):
    """路径不合法（绝对路径 / 越界）。"""


def relative_path(p: str, *, base: Path | None = None) -> Path:
    """**对外接口专用**：只接受相对路径，绝对路径和 `..` 越界一律拒绝。

    为什么：MCP 是给 Agent 用的，它不该能读 `C:\\Windows\\System32\\...`。
    流程内部（节点之间）仍用 resolve_path，保持灵活。
    """
    raw = str(p or "").strip()
    if not raw:
        raise PathNotAllowed("路径不能为空")
    pp = Path(raw).expanduser()
    if pp.is_absolute() or pp.drive:
        raise PathNotAllowed(
            f"只接受相对路径（基于项目根，如 out/a.png、flows/b.json），收到绝对路径: {raw}"
        )

    root = (base or PROJECT_ROOT).resolve()
    resolved = (root / pp).resolve()
    # 防止 out/../../etc 这种反斜杠越界
    if resolved != root and root not in resolved.parents:
        raise PathNotAllowed(f"路径越界（不允许跳出项目根）: {raw}")
    return resolved


# ---------------------------------------------------------------- 打包（frozen）


def meipass() -> Path | None:
    """PyInstaller onefile 的解压目录（运行时才有）。"""
    v = getattr(sys, "_MEIPASS", None)
    return Path(v) if v else None


def bootstrap_frozen() -> list[str]:
    """打包后首次运行：把包内只读资源释放到 exe 旁边，让用户能改。

    为什么需要：`--add-data` 打进去的 flows 运行时解压在 `_MEIPASS`（临时目录，
    退出即删）。而用户要能看到并编辑流程，所以复制到 exe 同级。

    只在目标不存在时复制 —— 用户改过的流程不会被覆盖。
    返回实际释放的资源名列表（空列表表示不需要或已完成）。
    """
    if not getattr(sys, "frozen", False):
        return []
    src = meipass()
    if src is None:
        return []

    import shutil

    released: list[str] = []
    for name in ("flows",):
        s = src / name
        d = PROJECT_ROOT / name
        if not s.is_dir():
            continue
        if d.exists() and any(d.iterdir()):
            continue  # 已有内容：不覆盖
        try:
            if d.exists():
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copytree(s, d)
            released.append(name)
        except Exception:  # noqa: BLE001 - 释放失败不该让程序起不来
            pass
    return released


def describe() -> dict[str, str]:
    """当前解析出的所有路径（排查用）。"""
    return {
        "frozen": str(getattr(sys, "frozen", False)),
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "MEIPASS": str(meipass() or "-"),
        "FLOWS_DIR": str(FLOWS_DIR),
        "DB_PATH": str(DB_PATH),
        "ELEMENTS_PATH": str(ELEMENTS_PATH),
        "OUT_DIR": str(OUT_DIR),
    }
