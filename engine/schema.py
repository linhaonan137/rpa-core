"""流程 JSON 契约 —— 所有节点与 Agent 以此为准，不得改动字段名。

设计要点（四处关键取舍）：
  1. 边用 from / to / exit，不用 source / target / sourceHandle
  2. 节点参数一层 args，不再套 data.config
  3. 变量键名 vars，不用 variables
  4. 出口名 exit，普通边为 None

元数据约定：字段名以下划线开头的（_note / _heal / _meta）是注释性数据，
引擎在读取业务参数时一律跳过，不参与执行。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------- 枚举

# 出口：普通边为 None；分支/循环/错误各有自己的名字
Exit = Literal["true", "false", "loop", "done", "error"] | None

# 失败策略：retry 原地重试 / back 回退到某节点 / skip 跳过 / stop 中断
OnFailMode = Literal["retry", "back", "skip", "stop"]

WaitState = Literal["visible", "attached", "hidden", "exist"]


# ---------------------------------------------------------------- 子结构


class Position(BaseModel):
    x: float = 0.0
    y: float = 0.0


class OnFail(BaseModel):
    """失败时的处理。mode=back 时 rollback_to 必填。"""

    mode: OnFailMode = "retry"
    attempts: int = Field(default=2, ge=0, le=10)
    backoff_ms: int = Field(default=1000, ge=0, le=60_000)
    rollback_to: str | None = None  # mode=back 时的回退节点 id
    then: OnFailMode | None = None  # 次数耗尽后的动作，默认 stop

    @model_validator(mode="after")
    def _check_back(self) -> "OnFail":
        if self.mode == "back" and not self.rollback_to:
            raise ValueError("on_fail.mode=back 时必须给 rollback_to")
        return self


class Expect(BaseModel):
    """事后断言：动作成功了，但结果不一定对。"""

    target: str | None = None  # 元素引用 @el:name 或 css
    state: WaitState | None = None  # 元素应为什么状态
    text: str | None = None  # 页面应包含的文本
    url: str | None = None  # URL 应包含的片段
    limit_ms: int = Field(default=10_000, ge=0)

    @model_validator(mode="after")
    def _at_least_one(self) -> "Expect":
        if not any((self.target, self.text, self.url)):
            raise ValueError("expect 至少要给 target / text / url 之一")
        return self


# ---------------------------------------------------------------- 主体


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    position: Position = Field(default_factory=Position)
    args: dict[str, Any] = Field(default_factory=dict)

    label: str | None = None  # 日志显示名，缺省用 type
    off: bool = False  # 禁用：直接算成功跳过
    on_fail: OnFail | None = None
    expect: Expect | None = None
    budget_ms: int = Field(default=0, ge=0)  # 0 = 用该类型默认值

    # 元数据（引擎不读，只做展示与审计）
    note: str | None = Field(default=None, alias="_note")
    heal: dict[str, Any] | None = Field(default=None, alias="_heal")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @property
    def display(self) -> str:
        return self.label or self.type


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: str
    exit: Exit = None  # 普通边为 None


class Var(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: Any = None


class Flow(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    version: int = 1
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    vars: list[Var] = Field(default_factory=list)
    elements: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")

    # ------------------------------------------------------------ 校验

    @model_validator(mode="after")
    def _check_graph(self) -> "Flow":
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"节点 id 重复: {dup}")

        known = set(ids)
        for e in self.edges:
            if e.from_ not in known:
                raise ValueError(f"边 {e.id} 的 from 指向不存在的节点: {e.from_}")
            if e.to not in known:
                raise ValueError(f"边 {e.id} 的 to 指向不存在的节点: {e.to}")

        # 出口名只有分支/循环类节点才合法
        branch_types = {"if", "loop", "foreach", "map_each", "while"}
        for e in self.edges:
            if e.exit in ("true", "false", "loop", "done"):
                src = next(n for n in self.nodes if n.id == e.from_)
                if src.type not in branch_types:
                    raise ValueError(
                        f"节点 {src.id}({src.type}) 不是分支/循环类，"
                        f"却用了 exit={e.exit}"
                    )
        return self

    # ------------------------------------------------------------ 工具

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"无此节点: {node_id}")

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


# ---------------------------------------------------------------- 便捷构造

#: node() 里这些键算节点级字段，其余进 args
_NODE_FIELDS = {
    "on_fail", "expect", "off", "label", "budget_ms", "position", "note", "heal", "meta",
}


def node(id: str, type: str, **kw: Any) -> Node:  # noqa: A002
    """测试里快速造节点。

    node("n1", "open", url="u")                  → args={"url":"u"}
    node("n1", "fail", on_fail=OnFail(...))      → 字段 on_fail
    node("n1", "noop", note="说明")               → 字段 note（序列化为 _note）
    """
    args: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for k, v in kw.items():
        if k in _NODE_FIELDS:
            extra[k] = v
        else:
            args[k] = v
    return Node(id=id, type=type, args=args, **extra)


def edge(from_: str, to: str, exit: Exit = None, id: str | None = None) -> Edge:
    return Edge(id=id or f"{from_}-{to}", **{"from": from_}, to=to, exit=exit)
