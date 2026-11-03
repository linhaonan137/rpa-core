"""元素匹配打分器 + 三道门。

给一个"目标档案"和页面上的若干候选，算出每个候选的相似度，然后决定：
  · commit  可以修
  · report  报人（不够像 / 有歧义 / 试跑失败）

权重设计（参考 Healenium 的属性加权 + Automation Sandbox 的可解释评分）：

  role  0.25   ARIA 语义最稳，改样式不影响
  name  0.20   可见文本相似度
  attrs 0.15   稳定属性交集（id / data-testid / name / type）
  path  0.15   父链公共前缀比（LCS 的简化版）
  tag   0.10   标签相同
  nb    0.10   邻居文本交集
  geo   0.05   几何位置接近（最后的兜底）

三道门（缺一条就会 false heal）：
  ① top.score >= MIN_SCORE（默认 0.60）
  ② top.score - runner_up.score >= MIN_GAP（默认 0.10，防"两个都像"选错）
  ③ 试跑动作真的成功（由调用方提供 verify 回调，本模块只给结论）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .store import Fingerprint

#: 各维度权重，和恒为 1.0
WEIGHTS: dict[str, float] = {
    "role": 0.25,
    "name": 0.20,
    "attrs": 0.15,
    "path": 0.15,
    "tag": 0.10,
    "nb": 0.10,
    "geo": 0.05,
}

MIN_SCORE = 0.60
MIN_GAP = 0.10
MAX_GEO_DIST = 800.0  # 距离超过这个像素数，几何项归零


@dataclass
class Candidate:
    """页面上的一个候选元素。"""

    role: str | None = None
    name: str | None = None
    tag: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    path: list[str] = field(default_factory=list)
    siblings: list[str] = field(default_factory=list)
    geometry: dict[str, int] = field(default_factory=dict)
    ref: str = ""  # 页面内临时引用（不持久化）


@dataclass
class Scored:
    candidate: Candidate
    score: float
    parts: dict[str, float]  # 逐项贡献，用于解释"为什么选它"


@dataclass
class Decision:
    action: str  # "commit" | "report"
    reason: str
    best: Scored | None = None
    runner_up: Scored | None = None
    ranked: list[Scored] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.action == "commit"


# ---------------------------------------------------------------- 相似度基元


def text_sim(a: str | None, b: str | None) -> float:
    """0~1 的文本相似度：完全相同 1.0，包含 0.8，否则按字符交集比例。"""
    if not a or not b:
        return 0.0
    a, b = a.strip(), b.strip()
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def jaccard(a: Sequence[str] | dict[str, Any], b: Sequence[str] | dict[str, Any]) -> float:
    """集合/字典键的交并比。"""
    sa = set(a.keys() if isinstance(a, dict) else a)
    sb = set(b.keys() if isinstance(b, dict) else b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def prefix_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """两个路径从根开始的公共前缀比例（等价于 LCS 的最长公共前缀近似）。"""
    if not a or not b:
        return 0.0
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n / max(len(a), len(b))


def geo_sim(a: dict[str, int], b: dict[str, int]) -> float:
    """几何接近度：按中心点距离衰减。"""
    if not a or not b:
        return 0.0
    ax, ay = a.get("x", 0) + a.get("w", 0) / 2, a.get("y", 0) + a.get("h", 0) / 2
    bx, by = b.get("x", 0) + b.get("w", 0) / 2, b.get("y", 0) + b.get("h", 0) / 2
    dist = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
    return max(0.0, 1.0 - dist / MAX_GEO_DIST)


# ---------------------------------------------------------------- 打分


def score(cand: Candidate, fp: Fingerprint) -> Scored:
    """给一个候选打分，返回总分与逐项贡献（可解释）。"""
    parts: dict[str, float] = {}

    parts["role"] = WEIGHTS["role"] * (1.0 if cand.role and cand.role == fp_role(fp) else 0.0)
    parts["name"] = WEIGHTS["name"] * text_sim(cand.name, fp.attrs.get("_name") or fp_name(fp))
    parts["attrs"] = WEIGHTS["attrs"] * jaccard(_stable_attrs(cand.attrs), _stable_attrs(fp.attrs))
    parts["path"] = WEIGHTS["path"] * prefix_ratio(cand.path, fp.path)
    parts["tag"] = WEIGHTS["tag"] * (1.0 if cand.tag and cand.tag.lower() == fp.tag.lower() else 0.0)
    parts["nb"] = WEIGHTS["nb"] * jaccard(cand.siblings, fp.siblings)
    parts["geo"] = WEIGHTS["geo"] * geo_sim(cand.geometry, _geo_dict(fp))

    total = round(sum(parts.values()), 4)
    return Scored(candidate=cand, score=total, parts={k: round(v, 4) for k, v in parts.items()})


@dataclass
class Target:
    """打分目标：档案 + 期望的 role/name（档案里可能只藏了 hints）。"""

    fingerprint: Fingerprint
    role: str | None = None
    name: str | None = None


def fp_role(fp: Fingerprint) -> str | None:
    return fp.role or fp.attrs.get("role")


def fp_name(fp: Fingerprint) -> str | None:
    return fp.name or fp.attrs.get("_text")


_STABLE_ATTRS = ("id", "name", "data-testid", "type", "placeholder", "aria-label", "title")


def _stable_attrs(attrs: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in attrs.items() if k in _STABLE_ATTRS}


def _geo_dict(fp: Fingerprint) -> dict[str, int]:
    g = fp.geometry
    return {"x": g.x, "y": g.y, "w": g.w, "h": g.h}


# ---------------------------------------------------------------- 排名与决策


def rank(candidates: Sequence[Candidate], fp: Fingerprint, role: str | None = None, name: str | None = None) -> list[Scored]:
    """按分数从高到低排序。role/name 覆盖档案里的空值。"""
    if role and not fp.role:
        fp.role = role
    if name and not fp.name:
        fp.name = name
    scored = [score(c, fp) for c in candidates]
    return sorted(scored, key=lambda s: s.score, reverse=True)


def decide(
    ranked: Sequence[Scored],
    *,
    min_score: float = MIN_SCORE,
    min_gap: float = MIN_GAP,
) -> Decision:
    """三道门的前两道（第三道试跑由调用方做）。

    门① 分数够高    门② 与第二名的差距够大
    """
    if not ranked:
        return Decision("report", "没有候选")

    best = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None

    if best.score < min_score:
        return Decision(
            "report",
            f"最高分 {best.score:.3f} 低于阈值 {min_score}",
            best=best, runner_up=runner, ranked=list(ranked),
        )

    if runner is not None and (best.score - runner.score) < min_gap:
        return Decision(
            "report",
            f"歧义：top1={best.score:.3f} 与 top2={runner.score:.3f} 差距小于 {min_gap}",
            best=best, runner_up=runner, ranked=list(ranked),
        )

    return Decision(
        "commit",
        f"可信：{best.score:.3f}"
        + (f"，领先第二名 {best.score - runner.score:.3f}" if runner else ""),
        best=best, runner_up=runner, ranked=list(ranked),
    )


def match(
    candidates: Sequence[Candidate],
    fp: Fingerprint,
    *,
    role: str | None = None,
    name: str | None = None,
    min_score: float = MIN_SCORE,
    min_gap: float = MIN_GAP,
    verify: Callable[[Candidate], bool] | None = None,
) -> Decision:
    """完整三道门：打分 → 阈值 → 歧义 → 试跑。

    verify 是第三道门：拿候选真的试一次动作，返回是否成功。
    没有 verify 时跳过第三道门（调用方要自己承担 false heal 风险）。
    """
    ranked = rank(candidates, fp, role=role, name=name)
    d = decide(ranked, min_score=min_score, min_gap=min_gap)

    if d.action == "commit" and verify is not None and d.best is not None:
        try:
            ok = bool(verify(d.best.candidate))
        except Exception as exc:  # noqa: BLE001
            return Decision("report", f"试跑异常: {exc}", best=d.best, runner_up=d.runner_up, ranked=list(ranked))
        if not ok:
            return Decision(
                "report", "试跑动作失败（不动元素库）",
                best=d.best, runner_up=d.runner_up, ranked=list(ranked),
            )
    return d


def explain(s: Scored) -> str:
    """人类可读的解释，写进 changes.jsonl。"""
    parts = " ".join(f"{k}={v:.2f}" for k, v in s.parts.items() if v > 0)
    return f"score={s.score:.3f} [{parts}]"
