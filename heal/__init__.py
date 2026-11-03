"""自愈：五层分层修复（L0 引擎 / L1 元素库 / L1.5 诊断 / L2 流程 / L3 报人）。

入口只有一个：`heal_once`。分层判定在 rules，候选与三道门在 candidates，
写元素库与快照/流水在 patch，失败现场在 context。
"""

from .agent import HealResult, build_l2_prompt, heal_once
from .candidates import collect, pick
from .context import FailureContext, build_failure, find_element_ref
from .patch import (
    apply_to_element,
    log_change,
    restore_element,
    snapshot_element,
)
from .rules import classify, diagnose, retry_with_reload

__all__ = [
    "HealResult",
    "heal_once",
    "build_l2_prompt",
    "FailureContext",
    "build_failure",
    "find_element_ref",
    "collect",
    "pick",
    "classify",
    "diagnose",
    "retry_with_reload",
    "apply_to_element",
    "restore_element",
    "snapshot_element",
    "log_change",
]
