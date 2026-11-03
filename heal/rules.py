"""L1 规则层：先判层，再决定改什么。**这一层绝不调 LLM。**

判层顺序（顺序不能反，见 PLAN 6.2 的诊断表）：

  ① 登录页 / 验证页          → L3 报人（改选择器解决不了会话失效）
  ② 候选为空                 → L2（页面结构真变了）
  ③ 唯一高分候选             → L1（元素库过期，改库即可）
  ④ 多个高分 / 都不够像      → L1.5（先诊断，再回 L1 或上 L2）

阈值取 0.75：middleware.candidates 的分数里 role 0.35 / name 0.45 / css 0.20，
命中 role+name 就是 0.80 以上；只命中 name 是 0.45，不够。歧义门用 0.10 的
间距，和 elements.matcher.MIN_GAP 保持一致。
"""

from __future__ import annotations

from typing import Any

from .context import FailureContext

#: 判"高分候选"的阈值
HIGH_SCORE = 0.75
#: 判"还有点像"的阈值（低于它认为结构已变）
LOW_SCORE = 0.40
#: 与第二名的差距低于它 → 歧义
MIN_GAP = 0.10

#: URL 里出现这些片段就认为不是业务页
_AUTH_URL = (
    "/login",
    "/signin",
    "/sign-in",
    "/sign_in",
    "/passport",
    "/sso",
    "/captcha",
    "/verify",
    "/auth/",
    "login.html",
    "login.aspx",
    "accounts.google.com",
)

#: 只有"验证码"这类元素才足以判定为验证页（"登录"两个字误报太多，不算）
_AUTH_ELEMENT = (
    "验证码",
    "captcha",
    "recaptcha",
    "人机验证",
    "滑块验证",
    "扫码登录",
    "双重验证",
    "两步验证",
)

#: 错误文本里的会话失效信号
_AUTH_ERROR = (
    "登录已失效",
    "会话失效",
    "未登录",
    "需要登录",
    "登录超时",
    "not logged in",
    "session expired",
)


# ---------------------------------------------------------------- 判层


def classify(fc: FailureContext) -> str:
    """判定这一轮该走哪层，返回 'L1' | 'L1.5' | 'L2' | 'L3'。

    不抛异常：字段缺失时按"信息最少"的保守结论走（歧义 → L1.5，没有候选 → L2）。
    """
    if is_auth_page(fc):
        return "L3"

    candidates = list(fc.candidates or [])
    if not candidates:
        return "L2"

    scored = [(c, _score_of(c)) for c in candidates]
    known = [(c, s) for c, s in scored if s is not None]

    if not known:
        # 候选没带分数（外部塞进来的裸候选）：只能按数量粗判
        return "L1" if len(candidates) == 1 else "L1.5"

    known.sort(key=lambda p: p[1], reverse=True)
    top_score = known[0][1]
    high = [p for p in known if p[1] >= HIGH_SCORE]

    if len(high) >= 2:
        return "L1.5"

    if len(high) == 1:
        if len(known) > 1:
            second = known[1][1]
            if second >= HIGH_SCORE - MIN_GAP and (top_score - second) < MIN_GAP:
                return "L1.5"
        return "L1"

    return "L1.5"


def is_auth_page(fc: FailureContext) -> bool:
    """登录页 / 验证页 / 会话失效 → 只能报人。"""
    url = (fc.url or "").lower()
    if any(token in url for token in _AUTH_URL):
        return True

    error = (fc.error or "").lower()
    if any(token in error for token in _AUTH_ERROR):
        return True

    text = " ".join(
        f"{c.get('name') or ''} {c.get('role') or ''}".lower()
        for c in (fc.candidates or [])
        if isinstance(c, dict)
    )
    return any(token in text for token in _AUTH_ELEMENT)


# ---------------------------------------------------------------- L1.5 诊断


def diagnose(fc: FailureContext, decision: Any = None) -> tuple[str, str]:
    """L1.5 诊断：只出结论，不改任何东西。返回 (下一步层, 理由)。

    decision 是 elements.matcher.Decision（可选）。有它能区分
    "属性变了（回 L1）"和"结构大变（上 L2）"。
    """
    if is_auth_page(fc):
        return "L3", "页面是登录/验证页（会话失效或风控），改流程无用"

    if not (fc.candidates or []):
        return "L2", "页面上找不到任何候选：结构大变或缺了一步"

    best = getattr(decision, "best", None) if decision is not None else None
    if best is not None:
        score = float(getattr(best, "score", 0.0) or 0.0)
        if score >= HIGH_SCORE:
            reason = getattr(decision, "reason", "定位歧义")
            return "L1", f"元素还在，只是定位歧义（{reason}）：按最高分加限定后回 L1"
        if score < LOW_SCORE:
            return "L2", f"最像的候选仅 {score:.2f}，页面结构已变"

    return "L2", "候选都不够像：判定为页面结构变化"


# ---------------------------------------------------------------- L0


def retry_with_reload(args: dict[str, Any] | None) -> dict[str, Any]:
    """L0：重试前把 reload 打开（吃掉弹窗/遮罩类失败）。返回新 dict，不改入参。"""
    out: dict[str, Any] = dict(args or {})
    out["reload"] = True
    return out


# ---------------------------------------------------------------- 内部


def _score_of(cand: Any) -> float | None:
    if isinstance(cand, dict):
        raw = cand.get("score")
    else:
        raw = getattr(cand, "score", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
