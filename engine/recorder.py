"""操作录制：把 Agent 的一串操作自动转成可重跑的流程 JSON。

解决什么问题：
  以前建流程是「操作一遍 → 记住每个 target → 手写 20 个节点 JSON」，
  那 20 个节点是纯体力活，还容易写错（今天那条 20 节点的流程就是这么敲出来的）。

怎么工作：
  record_start() → 之后每个**成功的写操作**（点击/输入/打开页…）都被记一条
  record_stop()  → 按顺序铺成 nodes + 线性 edges，直接存成 flows/xxx.json

刻意**不录**的节点：
  web_scan / web_fields / web_elem_info / element_check / web_verify
  —— 这些是「探索辅助」，跑流程时不需要；录进去只会让流程变慢、变脏。
  web_pick 也不录 —— 元素进库是外部状态，流程里用 `@el:名字` 引用即可。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 录制期的变量占位语法：`{{名字}}`（双花括号）。
#: 为什么不用引擎的 `{名字}`：引擎流程里单花括号就是变量引用，
#: 录制期如果直接用会和"字面量花括号"混淆；双括号在录制期处理掉，
#: 生成流程时再降成单括号交给引擎。
_PLACEHOLDER = re.compile(r"\{\{\s*([\w\u4e00-\u9fff]+)\s*\}\}")

#: 会被录进流程的节点类型（写操作 + 页面生命周期）
RECORDABLE = {
    "web_open",
    "web_attach",
    "web_click",
    "web_type",
    "web_fill_many",
    "web_type_label",
    "web_pick_option",
    "web_toggle",
    "web_upload",
    "web_hover",
    "web_drag",
    "press_key",
    "web_scroll",
    "web_wait",
    "sleep",
    "web_shot",
    "web_back",
    "web_forward",
    "web_reload",
    "web_close",
    "web_js",
    "web_frame_in",
    "web_frame_out",
}

#: 录制时丢掉的参数（噪音 / 每次不同 / 只影响本次调试）
DROP_ARGS = {"save_as", "hints", "limit_ms", "force", "verify_text", "hold_ms", "mode"}

#: 这些节点后面**必定**发生页面跳转/重载，必须自动补等待，
#: 否则录出来的流程是"attach 完立刻点"，跑第二遍时页面还没加载好就失败。
NAV_NODES = {"web_open", "web_attach", "web_back", "web_forward", "web_reload"}
NAV_WAIT_MS = 1800


class Recorder:
    """单例式录像机：进程内只有一份状态。"""

    def __init__(self) -> None:
        self.active = False
        self.entries: list[dict[str, Any]] = []
        self.skipped = 0
        self.vars: dict[str, Any] = {}
        self.context_node: dict[str, Any] = {}

    # ---------------------------------------------------------- 控制

    def start(
        self,
        variables: dict[str, Any] | None = None,
        context_node: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.active = True
        self.entries = []
        self.skipped = 0
        self.vars = dict(variables or {})
        # 录制开始时的页面现场（attach 一般发生在 record_start 之前，
        # 但录出来的流程必须从"接管页面"开始，否则第一条操作就没页面可用）
        self.context_node = dict(context_node or {})
        msg = "开始录制（之后的操作会自动记录）"
        if self.vars:
            msg += f"；已声明 {len(self.vars)} 个变量：{', '.join(self.vars)}"
        if self.context_node:
            msg += "；已记录当前页面（生成流程时会自动放在最前面）"
        return {"ok": True, "message": msg, "variables": dict(self.vars)}

    # ---------------------------------------------------------- 变量

    def resolve(self, value: Any) -> Any:
        """把 `{{变量}}` 换成 vars 里的实际值（**只在执行时用**，录制保留模板）。"""
        if not self.vars:
            return value
        if isinstance(value, str):
            def _rep(m: re.Match[str]) -> str:
                k = m.group(1)
                return str(self.vars.get(k, m.group(0)))

            return _PLACEHOLDER.sub(_rep, value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def to_engine_template(self, value: Any) -> Any:
        """`{{变量}}` → 引擎的 `{变量}`（生成流程时才转）。

        **只转 vars 里声明过的名字**。用户填的内容里本来就可能有双花括号
        （Vue/Handlebars 模板、话术里的 `{{客户姓名}}`），那些必须原样保留，
        否则回放时会被当成未定义变量而报错。
        """
        declared = set(self.vars) if self.vars else set()

        def _sub(m: re.Match[str]) -> str:
            k = m.group(1)
            return "{" + k + "}" if k in declared else m.group(0)

        if isinstance(value, str):
            return _PLACEHOLDER.sub(_sub, value)
        if isinstance(value, dict):
            return {k: self.to_engine_template(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.to_engine_template(v) for v in value]
        return value

    # ---------------------------------------------------------- 记录

    def add(self, node_type: str, args: dict[str, Any], message: str = "") -> None:
        if not self.active:
            return
        if node_type not in RECORDABLE:
            self.skipped += 1
            return
        clean = {
            k: v
            for k, v in (args or {}).items()
            if k not in DROP_ARGS and v not in (None, "", [], {})
        }
        self.entries.append({"type": node_type, "args": clean, "msg": (message or "")[:40]})

    # ---------------------------------------------------------- 收尾

    def build_flow(self, name: str, title: str = "") -> dict[str, Any]:
        # 先展开成"节点计划"，顺手补等待
        plan: list[dict[str, Any]] = []
        # 前置：录制开始时的页面现场（如果第一段操作本身不是页面类节点）
        first_types = {e["type"] for e in self.entries[:2]}
        if self.context_node and not (first_types & NAV_NODES):
            plan.append(
                {
                    "type": self.context_node.get("type", "web_attach"),
                    "args": self.context_node.get("args", {}),
                    "msg": "接管页面（录制开始时自动记录）",
                }
            )
        for e in self.entries:
            plan.append(
                {
                    "type": e["type"],
                    # 存进流程 JSON 时把 {{x}} 降成引擎的 {x}
                    "args": self.to_engine_template(e["args"]),
                    "msg": e.get("msg", ""),
                }
            )
            # 导航类节点后面自动补一个 sleep（除非下一条已经是 sleep/web_wait）
            if e["type"] in NAV_NODES:
                plan.append(
                    {
                        "type": "sleep",
                        "args": {"ms": NAV_WAIT_MS},
                        "msg": f"等页面加载（{NAV_WAIT_MS}ms，自动补）",
                        "_auto": True,
                    }
                )
        # 去掉重复/多余的 sleep（连续两个 sleep 合并）
        merged: list[dict[str, Any]] = []
        for p in plan:
            if (
                p["type"] == "sleep"
                and merged
                and merged[-1]["type"] == "sleep"
            ):
                continue
            merged.append(p)

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for i, e in enumerate(merged, 1):
            nid = f"n{i}"
            label = e.get("msg") or e["type"]
            nodes.append(
                {
                    "id": nid,
                    "type": e["type"],
                    "position": {"x": 0, "y": (i - 1) * 90},
                    "args": e["args"],
                    "label": label,
                    "off": False,
                    "budget_ms": 0,
                }
            )
            if i > 1:
                edges.append({"id": f"e{i - 1}", "from": f"n{i - 1}", "to": nid})
        return {
            "name": title or name,
            "version": 1,
            "nodes": nodes,
            "edges": edges,
            "vars": [{"name": k, "value": v} for k, v in self.vars.items()],
            "elements": {},
        }

    def stop(self, save_to: Any = None, name: str = "", title: str = "") -> dict[str, Any]:
        """停止录制，返回流程 dict；给了 save_to 就顺便落盘。"""
        self.active = False
        flow = self.build_flow(name, title)
        out: dict[str, Any] = {
            "ok": True,
            "steps": len(flow["nodes"]),
            "skipped": self.skipped,
            "flow": flow,
        }
        if save_to is not None and flow["nodes"]:
            try:
                p = save_to
                p.write_text(json.dumps(flow, ensure_ascii=False, indent=2), encoding="utf-8")
                out["path"] = str(p)
                out.pop("flow", None)  # 落了盘就不回传整份 JSON，省上下文
            except Exception as exc:  # noqa: BLE001
                out["save_error"] = f"{type(exc).__name__}: {exc}"
        self.entries = []
        return out


#: 进程级单例（MCP 服务整个生命周期共用）
recorder = Recorder()
