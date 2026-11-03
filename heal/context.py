"""失败上下文：从 Runner 的返回里把"现场"捞出来。

设计取舍：
  · **只提取事实，不做判断**——判层在 rules.py，这里不写 if 判断层
  · **候选存成 plain dict**——FailureContext 要能直接写进 changes.jsonl，
    不能带 Candidate/Scorer 这类内部对象
  · **永远不抛**——它跑在失败路径上，再抛就把原始错误盖掉了

字段与 PLAN 8.5 的失败报告一一对应：node / type / config(args) / error /
page_url / candidates。snapshot_path 是 H13 落盘的现场快照。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: 元素引用的前缀
EL_PREFIX = "@el:"


@dataclass
class FailureContext:
    """一次失败的完整现场。layer 由 rules.classify 填写。"""

    flow_name: str
    node_id: str
    node_type: str
    args: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    url: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    snapshot_path: str | None = None
    layer: str = "L1"  # L1 / L1.5 / L2 / L3

    # ---------------------------------------------------------- 便捷

    @property
    def element_key(self) -> str | None:
        """失败节点引用的元素库 key（`@el:submit` → `submit`）。"""
        return find_element_ref(self.args)

    @property
    def target(self) -> Any:
        return self.args.get("target")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 提取


def build_failure(
    run_result: dict[str, Any] | None,
    flow: Any,
    ctx: Any = None,
    page: Any = None,
) -> FailureContext | None:
    """从 Runner.execute() 的返回拼出失败上下文。

    返回 None 表示"没有失败"（run_result.ok 为真）或结果里找不到失败节点。
    不抛异常：取不到的字段一律给安全默认值。
    """
    result = run_result or {}
    if result.get("ok") is True:
        return None

    logs = [
        n
        for n in (result.get("nodes") or [])
        if isinstance(n, dict) and not n.get("ok")
    ]
    if not logs:
        return None

    log = logs[-1]
    node_id = str(log.get("node_id") or log.get("node") or "")
    if not node_id:
        return None

    node = _node_of(flow, node_id)
    node_type = str(log.get("type") or (getattr(node, "type", "") or ""))
    error = str(log.get("error") or "")
    # 事后断言失败时日志类型是 "expect"，节点类型要以流程为准
    if node_type == "expect" and node is not None:
        node_type = str(node.type)
        error = error or "postAssert 失败"
    if not error:
        error = "未知错误"

    return FailureContext(
        flow_name=str(getattr(flow, "name", "") or "?"),
        node_id=node_id,
        node_type=node_type,
        args=dict(getattr(node, "args", {}) or {}),
        error=error,
        url=_url_of(ctx, page),
        candidates=[],
        snapshot_path=None,
    )


def find_element_ref(value: Any) -> str | None:
    """递归扫 args，找出第一个 `@el:xxx` 引用的 key。"""
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith(EL_PREFIX):
            key = text[len(EL_PREFIX):].strip()
            return key or None
        return None
    if isinstance(value, dict):
        # target 优先，避免被 hints 里的次要引用抢走
        for k in ("target", "element", "el"):
            if k in value:
                got = find_element_ref(value[k])
                if got:
                    return got
        for v in value.values():
            got = find_element_ref(v)
            if got:
                return got
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            got = find_element_ref(item)
            if got:
                return got
    return None


# ---------------------------------------------------------------- 内部


def _node_of(flow: Any, node_id: str) -> Any:
    getter = getattr(flow, "node", None)
    if not callable(getter):
        return None
    try:
        return getter(node_id)
    except (KeyError, ValueError):
        return None


def _url_of(ctx: Any, page: Any) -> str | None:
    for source in (page, getattr(ctx, "page", None)):
        if source is None:
            continue
        try:
            url = getattr(source, "url", None)
        except Exception:  # noqa: BLE001 - 页面已关闭
            url = None
        if url:
            return str(url)

    snap = getattr(ctx, "snapshot", None)
    if callable(snap):
        try:
            url = (snap() or {}).get("url")
        except Exception:  # noqa: BLE001
            url = None
        if url:
            return str(url)
    return None
