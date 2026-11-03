"""节点包：按需加载 + 全量加载。

为什么不写成 `from . import web, data, ...`：
  多个 Agent 并行开发时都会新增文件，写死 import 列表会互相冲突、
  且任何一个文件有语法错误就整个包不可用。

用法：
  load("web")     只加载一个模块（测试用，互不干扰）
  load_all()      加载全部（生产入口用）
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

_failed: dict[str, str] = {}

#: 打包环境的兜底清单（PyInstaller 扫不到目录时用）
_EXPLICIT = ["web", "data", "files", "system", "ai", "media", "trigger", "desktop", "picker", "net"]


def load(name: str) -> Any:
    """加载单个节点模块。"""
    return importlib.import_module(f"{__name__}.{name}")


def load_all() -> dict[str, str]:
    """加载目录下全部模块，返回失败的 {模块名: 错误}。

    PyInstaller onefile 打包后 pkgutil.iter_modules 扫不到目录，
    所以用显式清单兜底 —— 新增节点文件时要同步加进 _EXPLICIT。
    """
    names: list[str] = []
    try:
        names = [m.name for m in pkgutil.iter_modules(__path__)]
    except Exception:  # noqa: BLE001 - 打包环境没有目录可扫
        pass
    for n in _EXPLICIT:
        if n not in names:
            names.append(n)

    for m in sorted(set(names)):
        if m.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{m}")
        except Exception as exc:  # noqa: BLE001 - 单个模块坏不该拖垮整包
            _failed[m] = f"{type(exc).__name__}: {exc}"
    return dict(_failed)


def failures() -> dict[str, str]:
    return dict(_failed)


# 控制类节点是引擎自举必需的，导入包时直接加载
from . import system  # noqa: E402,F401

__all__ = ["load", "load_all", "failures", "system"]
