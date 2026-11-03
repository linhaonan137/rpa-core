"""节点注册表与统一返回结构。

两条硬规则：
  1. 同一节点类型被两个不同模块注册 → 启动即失败（防"改了死代码副本不生效"）
  2. 节点执行不许抛异常穿透，失败一律返回 Result(ok=False)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Result:
    """节点统一返回值。"""

    ok: bool = True
    next: str | None = None  # 走哪个出口（None = 默认边）
    data: Any = None  # 出参，可被下游引用
    error: str | None = None
    message: str = ""
    ms: float = 0.0  # 耗时（毫秒）
    skipped: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def done(cls, message: str = "", data: Any = None, next: str | None = None) -> "Result":
        return cls(ok=True, message=message, data=data, next=next)

    @classmethod
    def fail(cls, error: str, message: str = "", data: Any = None) -> "Result":
        return cls(ok=False, error=error, message=message or error, data=data)

    @classmethod
    def skip(cls, message: str = "") -> "Result":
        return cls(ok=True, skipped=True, message=message)


# 节点函数签名：async def run(args: dict, ctx: ExecutionContext) -> Result
Executor = Callable[[dict[str, Any], Any], Awaitable[Result]]


class DuplicateNodeType(RuntimeError):
    """同一 type 被重复注册（不同模块）。"""


class Registry:
    def __init__(self) -> None:
        self._map: dict[str, Executor] = {}
        self._src: dict[str, str] = {}

    def add(self, type_: str, fn: Executor, src: str = "", raw: tuple[str, ...] | set[str] = ()) -> None:
        src = src or getattr(fn, "__module__", "?")
        old = self._map.get(type_)
        if old is not None:
            old_src = self._src.get(type_, "?")
            if old_src != src:
                raise DuplicateNodeType(
                    f"节点类型重复注册: {type_}\n  已注册来自: {old_src}\n  再次注册来自: {src}"
                )
            # 同模块重复注册（热重载/重复 import）→ 后写覆盖
        self._map[type_] = fn
        self._src[type_] = src
        if raw:
            _RAW_FIELDS[type_] = set(raw)

    def get(self, type_: str) -> Executor:
        try:
            return self._map[type_]
        except KeyError:
            raise KeyError(
                f"未注册的节点类型: {type_}（已注册 {len(self._map)} 个）"
            ) from None

    def has(self, type_: str) -> bool:
        return type_ in self._map

    def types(self) -> list[str]:
        return sorted(self._map)

    def source_of(self, type_: str) -> str:
        return self._src.get(type_, "?")

    def __len__(self) -> int:
        return len(self._map)


registry = Registry()

#: 哪些节点类型有"不做变量替换"的字段（代码/模板/正则）
_RAW_FIELDS: dict[str, set[str]] = {}


def register(type_: str, *, raw: tuple[str, ...] = ()) -> Callable[[Executor], Executor]:
    """用法：

    @register("open")
    async def web_open(args, ctx) -> Result: ...

    @register("web_js", raw=("code",))
    async def web_js(args, ctx) -> Result: ...
        # code 字段不参与 {var} 替换，避免 JS 里的 {} 被当变量

    为什么需要 raw：引擎默认对整份 args 做变量替换，但代码块/模板/正则里的
    花括号（`{a:1}`、`{姓名}`、`\\d{3}`）不是变量。声明 raw 后这些字段原样传入。
    """

    def deco(fn: Executor) -> Executor:
        registry.add(type_, fn, src=getattr(fn, "__module__", "?"))
        if raw:
            _RAW_FIELDS[type_] = set(raw)
        return fn

    return deco


def raw_fields(type_: str) -> set[str]:
    """该节点类型不做变量替换的字段名。"""
    return _RAW_FIELDS.get(type_, set())


# ---------------------------------------------------------------- 计时


class timer:
    """with timer() as t: ... → t.ms"""

    def __init__(self) -> None:
        self.ms: float = 0.0

    def __enter__(self) -> "timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ms = (time.perf_counter() - self._t0) * 1000
