"""执行上下文：变量作用域栈、页面句柄、循环栈、日志。

三条关键的静默失败防护：
  1. **点号 `{a.b}` 直接报错**——朴素实现的正则常缺 `$` 锚点，会把 `.b` 丢掉并返回整个 `a`
  2. **找不到变量报错**——原样保留 `{var}` 的实现，导致它被当字符串塞进选择器
  3. **整段引用保留类型**——`{"count": "{n}"}` 得到 int 5 而不是字符串 "5"

变量作用域是**栈**（S23）：
  - 全局作用域 [0] 存流程 vars
  - 子流程 / 循环 push 一层局部作用域，退出时 pop
  - 读：从最近一层往前找；写：默认写最近一层
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

# ---------------------------------------------------------------- 异常


class ContextError(Exception):
    """上下文类错误基类。"""


class UndefinedVar(ContextError):
    """变量不存在。注意：这是错误，不是"原样保留"。"""


class BadSyntax(ContextError):
    """变量表达式语法错误（典型：用了点号）。"""


class BadAccess(ContextError):
    """下标越界 / 键不存在 / 类型不支持。"""


# ---------------------------------------------------------------- 正则

# 一个占位符：{...}，花括号内不含嵌套；支持 \{ 转义
_TOKEN = re.compile(r"(?<!\\)\{([^{}]*)\}")
# 表达式：base 后跟 0..n 个 [xxx]
_EXPR = re.compile(r"^([A-Za-z_\u4e00-\u9fa5][\w\u4e00-\u9fa5]*)((?:\[[^\]]*\])*)$")
_BRACKET = re.compile(r"\[([^\]]*)\]")


# ---------------------------------------------------------------- 引擎级变量名

# 循环里由引擎注入的保留变量，用户 set_var 不能覆盖（防混淆）
RESERVED = {"_index", "_item", "_key", "_value", "_loop_depth"}


class ExecutionContext:
    """一次执行的全局状态。节点通过它读写变量、拿页面、打日志。"""

    def __init__(
        self,
        page: Any = None,
        run_id: str = "",
        logger: Callable[[str, str, dict], None] | None = None,
        elements: dict[str, Any] | None = None,
    ) -> None:
        self.page = page
        self.run_id = run_id
        self._logger = logger
        self.elements: dict[str, Any] = elements or {}

        self._scopes: list[dict[str, Any]] = [{}]  # [0] 全局
        self.loop_stack: list[dict[str, Any]] = []
        self.frames: list[str] = []  # iframe 链（选择器）

        # 运行期开关
        self.should_stop = False
        self.should_break = False
        self.should_continue = False

        # 失败时的现场（供自愈读取）
        self.last_error: dict[str, Any] | None = None

    # ------------------------------------------------------------ 作用域

    def push_scope(self) -> None:
        self._scopes.append({})

    def pop_scope(self) -> None:
        if len(self._scopes) <= 1:
            raise ContextError("作用域栈已空，不能 pop")
        self._scopes.pop()

    @property
    def depth(self) -> int:
        return len(self._scopes) - 1

    # ------------------------------------------------------------ 变量

    def set_var(self, name: str, value: Any, *, scope: str = "local") -> None:
        """scope=local 写当前层；scope=global 写全局层。"""
        if not name:
            raise ContextError("变量名不能为空")
        if name in RESERVED:
            raise ContextError(f"{name} 是引擎保留变量，不能覆盖")
        target = self._scopes[0] if scope == "global" else self._scopes[-1]
        target[name] = value

    def get_var(self, name: str) -> Any:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        raise UndefinedVar(f"变量未定义: {name}")

    def has_var(self, name: str) -> bool:
        return any(name in s for s in self._scopes)

    def all_vars(self) -> dict[str, Any]:
        """合并视图（外层在前，内层覆盖）。用于快照与日志。"""
        merged: dict[str, Any] = {}
        for scope in self._scopes:
            merged.update(scope)
        return merged

    # ------------------------------------------------------------ 解析

    def resolve(self, value: Any) -> Any:
        """把带 {占位符} 的值替换成实际值。

        - str：整段是单个占位符时**保留原类型**；否则插值成字符串
        - dict / list / tuple：递归
        - 其他类型原样返回
        """
        if isinstance(value, str):
            return self._resolve_str(value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            out = [self.resolve(v) for v in value]
            return tuple(out) if isinstance(value, tuple) else out
        return value

    def _resolve_str(self, text: str) -> Any:
        if "{" not in text:
            return text.replace("\\{", "{").replace("\\}", "}")

        # 整段就是一个占位符 → 保留类型
        whole = re.fullmatch(r"(?<!\\)\{([^{}]*)\}", text)
        if whole:
            expr = whole.group(1).strip()
            if not _EXPR.match(expr):
                self._raise_or_keep(expr)
                return text  # 是代码片段（如 {a:1}），原样返回
            return self._eval(expr)

        # 混合文本 → 逐个替换成字符串
        def _sub(m: re.Match[str]) -> str:
            expr = m.group(1).strip()
            if not _EXPR.match(expr):
                self._raise_or_keep(expr)
                return m.group(0)  # 原样保留，不当变量
            v = self._eval(expr)
            return v if isinstance(v, str) else _to_text(v)

        out = _TOKEN.sub(_sub, text)
        return out.replace("\\{", "{").replace("\\}", "}")

    @staticmethod
    def _raise_or_keep(expr: str) -> None:
        """不像变量表达式的 {xxx}：空值和点号报错，其余当代码片段原样保留。

        这样 `() => ({a:1})` 里的 {a:1} 不会被误当变量，
        而 `{}` / `{rows.标题}` 仍然给明确错误（检查点 X10）。
        """
        if not expr:
            raise BadSyntax("空占位符 {}")
        bare = expr.split(".")[0]
        if "." in expr and not bare.endswith("]"):
            raise BadSyntax(
                f"不支持的写法 {{{expr}}}：点号取值无效，请写成 {{base[key]}}"
            )

    def _eval(self, expr: str) -> Any:
        """求值一个表达式，如 `n` / `row[标题]` / `rows[0][name]`。"""
        if not expr:
            raise BadSyntax("空占位符 {}")

        m = _EXPR.match(expr)
        if not m:
            # 点号是最常见的错法，单独给提示
            if "." in expr and "[" not in expr.split(".")[0]:
                raise BadSyntax(
                    f"不支持的写法 {{{expr}}}：点号取值无效，请写成 {{base[key]}}"
                )
            raise BadSyntax(f"无法解析的占位符: {{{expr}}}")

        base, accessors = m.group(1), m.group(2)
        cur = self.get_var(base)  # 未定义 → UndefinedVar

        for raw in _BRACKET.findall(accessors):
            key = raw.strip()
            cur = self._access(cur, key, expr)
        return cur

    @staticmethod
    def _access(cur: Any, key: str, expr: str) -> Any:
        # 先脱引号（支持 [k] / ["k"] / ['k']）
        if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
            key = key[1:-1]

        if isinstance(cur, (list, tuple)):
            try:
                idx = int(key)
            except ValueError:
                raise BadAccess(f"{{{expr}}}: 列表要用整数下标，得到 {key!r}") from None
            try:
                return cur[idx]
            except IndexError:
                raise BadAccess(f"{{{expr}}}: 下标越界 {idx}（长度 {len(cur)}）") from None

        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            # 兼容 {"1": ...} 与 {1: ...}
            try:
                num = int(key)
                if num in cur:
                    return cur[num]
            except ValueError:
                pass
            raise BadAccess(f"{{{expr}}}: 键不存在 {key!r}")

        raise BadAccess(f"{{{expr}}}: {type(cur).__name__} 不支持下标取值")

    # ------------------------------------------------------------ 循环

    def push_loop(self, state: dict[str, Any]) -> None:
        self.loop_stack.append(state)
        self._set_reserved("_loop_depth", len(self.loop_stack))

    def pop_loop(self) -> None:
        if not self.loop_stack:
            raise ContextError("不在循环中，不能 pop_loop")
        self.loop_stack.pop()
        self._set_reserved("_loop_depth", len(self.loop_stack))

    def _set_reserved(self, name: str, value: Any) -> None:
        """引擎自己写保留变量，绕过 RESERVED 检查。"""
        self._scopes[-1][name] = value

    @property
    def in_loop(self) -> bool:
        return bool(self.loop_stack)

    # ------------------------------------------------------------ 日志

    def log(self, level: str, message: str, **kv: Any) -> None:
        if self._logger:
            self._logger(level, message, {"run_id": self.run_id, **kv})

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        """失败现场用：变量 + 循环栈 + 页面上限信息。"""
        info: dict[str, Any] = {
            "run_id": self.run_id,
            "vars": {k: _safe(v) for k, v in self.all_vars().items()},
            "loop_depth": len(self.loop_stack),
            "loop": self.loop_stack[-1] if self.loop_stack else None,
            "url": None,
        }
        try:
            if self.page is not None:
                info["url"] = self.page.url
        except Exception:  # noqa: BLE001 - 快照不能因取 url 失败而中断
            pass
        return info


# ---------------------------------------------------------------- 工具


def _to_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _safe(v: Any, limit: int = 500) -> Any:
    """快照里截断超长值，避免日志爆掉。"""
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + f"...({len(v)} chars)"
    if isinstance(v, (list, tuple)) and len(v) > 20:
        return [*list(v)[:20], f"...({len(v)} items)"]
    return v
