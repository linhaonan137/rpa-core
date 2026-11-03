"""六维指纹定位器：把元素库里存的六个维度**真的用上**。

之前的问题：元素库存了 role/name/attrs/path/siblings/geometry 六维，
但定位时只拼了个 `role=X[name="Y"]` 就完事 —— 其余 5 维存着没人用。
一旦 role 推断错、name 为空、或页面上有多个同名元素，就只能报错或点错。

这里的策略（按稳定性从高到低逐层降级）：
  1 语义   role + name        最稳，改文案才失效
  2 属性   id / data-testid / name / type      次稳
  3 结构   path（稳定化的父链）                一般
  4 邻居   siblings 文本 → 先锁父容器再找子元素  弱
  5 几何   文档坐标最近                       最弱，兜底

命中多个时用**加权打分**挑：语义(40) > 属性(30) > 结构(15) > 邻居(10) > 几何(5)。
最高分并列且没有明显优势 → 报歧义，把候选列出来，绝不静默取第一个。
"""

from __future__ import annotations

from typing import Any

__all__ = ["resolve_fingerprint", "AmbiguousElement", "SCORE"]

#: 各维度命中时的加分。语义层权重最高，几何只作兜底。
SCORE = {
    "role": 20,
    "name": 20,
    "attrs_id": 30,
    "attrs_testid": 28,
    "attrs_name": 18,
    "attrs_type": 8,
    "path": 15,
    "sibling": 10,
    "geometry": 5,
}


class AmbiguousElement(Exception):
    """匹配到多个元素且无法可靠区分。"""

    def __init__(self, key: str, candidates: list[dict[str, Any]]) -> None:
        self.key = key
        self.candidates = candidates
        lines = [f"@el:{key} 匹配到 {len(candidates)} 个元素，无法确定是哪一个："]
        for c in candidates[:5]:
            lines.append(
                f"  · {c.get('tag','?')}「{(c.get('name') or c.get('text') or '')[:40]}」"
                f" score={c.get('score', 0)}"
            )
        lines.append("请用更精确的定位（css / nth / 更具体的 name），或重新拾取该元素。")
        super().__init__("\n".join(lines))


def _attr_selectors(attrs: dict[str, str]) -> list[tuple[str, str, int]]:
    """按可靠性排出属性选择器（属性名, css 片段, 权重）。"""
    out: list[tuple[str, str, int]] = []
    for k in ("data-testid", "data-test", "data-qa", "data-cy"):
        if attrs.get(k):
            out.append((k, f'[{k}="{_esc(attrs[k])}"]', SCORE["attrs_testid"]))
    if attrs.get("id") and not attrs["id"].startswith("__"):
        out.append(("id", f'#{_esc(attrs["id"])}', SCORE["attrs_id"]))
    if attrs.get("name"):
        out.append(("name", f'[name="{_esc(attrs["name"])}"]', SCORE["attrs_name"]))
    if attrs.get("type"):
        out.append(("type", f'[type="{_esc(attrs["type"])}"]', SCORE["attrs_type"]))
    return out


def _esc(s: str) -> str:
    return str(s).replace('"', '\\"')


#: 用 label 文本 + 垂直/水平位置找表单控件，返回它在 querySelectorAll(tag) 里的索引。
#: 注意：索引要和 page.locator(tag) 的顺序一致（同一份 DOM，未穿透 shadow）。
_JS_FIND_BY_LABEL = r"""
([labelText, tagName]) => {
  const labs = [];
  for (const c of document.querySelectorAll("label, dt, th, span, div")) {
    const direct = Array.from(c.childNodes)
      .filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join("").trim();
    if (direct === labelText) labs.push(c);
  }
  if (!labs.length) return -1;
  const targets = Array.from(document.querySelectorAll(tagName));
  for (const lab of labs) {
    const lr = lab.getBoundingClientRect();
    let best = -1, bestDy = 999;
    targets.forEach((inp, i) => {
      const r = inp.getBoundingClientRect();
      if (r.width < 10 || r.height < 8) return;
      const dy = r.y - lr.y;
      if (dy < 2 || dy > 40) return;
      if (r.x > lr.x + lr.width || r.x + r.width < lr.x) return;
      if (dy < bestDy) { bestDy = dy; best = i; }
    });
    if (best >= 0) return best;
  }
  return -1;
}
"""


def _simplify_name(n: str) -> str:
    """去掉符号/引号/多余空白，只留字词。

    为什么需要：我们存的 name 是 innerText（`Next →`、`“It is our choices...”`），
    而 Playwright 的 get_by_role(name=...) 按 accname 规范算出来的是 `Next`
    （箭头和引号被规范化掉）—— 直接用原文匹配会 0 命中，白白降级到 path。
    """
    import re

    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(n or ""))
    return " ".join(s.split()).strip()


async def _count(loc: Any) -> int:
    try:
        return await loc.count()
    except Exception:  # noqa: BLE001
        return 0


async def _describe(loc: Any, idx: int = 0) -> dict[str, Any]:
    """取候选的简短描述（用于报错时给人看）。"""
    try:
        el = loc.nth(idx)
        return {
            "tag": (await el.evaluate("e => e.tagName.toLowerCase()")) or "?",
            "name": (await el.get_attribute("aria-label")) or "",
            "text": ((await el.inner_text()) or "").strip()[:80],
        }
    except Exception:  # noqa: BLE001
        return {"tag": "?", "name": "", "text": ""}


def _text_overlap(a: str, b: str) -> bool:
    """两段文本是否有实质重叠（用于校验 path 命中的是不是同一个元素）。

    path 命中只说明"这个位置有元素"，不说明"是原来那个元素"。
    实测：文案从「提交订单」改成「确认下单」，path 照样命中 —— 会静默点错。
    """
    x = _simplify_name(a)
    y = _simplify_name(b)
    if not x or not y:
        return True  # 没文本可比就放行（纯图标按钮之类）
    if x in y or y in x:
        return True
    # 字符集重叠率
    sx, sy = set(x), set(y)
    return len(sx & sy) / max(len(sx), 1) >= 0.5


async def resolve_fingerprint(
    page: Any,
    entry: dict[str, Any],
    *,
    key: str = "",
    limit_ms: int = 10_000,
    allow_multi: bool = False,
    _attempt: int = 0,
) -> tuple[Any, dict[str, Any]]:
    """用六维指纹定位一个元素，返回 (locator, 诊断信息)。

    entry 里可用的键（都是元素库条目里现成的）：
      role / name / css / attrs / path / siblings / geometry / tag
    """
    diag: dict[str, Any] = {"layer": None, "tried": [], "ambiguous": False}
    role = (entry.get("role") or "").strip()
    name = (entry.get("name") or "").strip()
    attrs = entry.get("attrs") or {}
    tag = (entry.get("tag") or "").strip()

    # ---------------------------------------------- 层 1：role + name
    if role and name:
        try:
            # 精确优先：Playwright 的 name 匹配默认是子串，
            # 「发布商品」会同时命中「发布商品时没有所需类目怎么办？」，
            # 也会把响应式布局里重复的菜单副本算进来。
            loc = page.get_by_role(role, name=name, exact=True)
            n = await _count(loc)
            diag["tried"].append({"layer": "role+name(exact)", "count": n, "sel": f'role={role}[name={name!r}]'})
            if n == 0:
                loc = page.get_by_role(role, name=name, exact=False)
                n = await _count(loc)
                diag["tried"].append({"layer": "role+name", "count": n, "sel": f'role={role}[name={name!r}]'})
            if n == 0:
                # 原文没中：用规范化后的名字再试（符号/引号会让 accname 对不上）
                simple = _simplify_name(name)[:40]
                if simple and simple != name:
                    loc = page.get_by_role(role, name=simple, exact=False)
                    n = await _count(loc)
                    diag["tried"].append(
                        {"layer": "role+name(simplified)", "count": n, "sel": f'role={role}[name={simple!r}]'}
                    )
                    if n == 1:
                        diag["layer"] = "role+name(simplified)"
                        return loc, diag
            if n == 1:
                diag["layer"] = "role+name"
                return loc, diag
            if n > 1:
                scored = await _score_candidates(loc, n, entry)
                if allow_multi:
                    diag["layer"] = "role+name"
                    diag["ambiguous"] = True
                    return loc, diag
                # 只有一个可见 → 直接用它（响应式布局常留隐藏副本）
                vis_only = [c for c in scored if c.get("visible")]
                if len(vis_only) == 1:
                    diag["layer"] = "role+name(visible)"
                    diag["score"] = vis_only[0]["score"]
                    return loc.nth(vis_only[0]["index"]), diag
                # 多个候选位置完全重合 → React SSR 水合残留的两份 DOM，
                # 点哪个效果一样，取第一个（实测淘宝「发布商品」就是这种）
                pool = vis_only or scored
                if len(pool) > 1:
                    pts = {(round(c.get("x", 0) / 4), round(c.get("y", 0) / 4)) for c in pool}
                    if len(pts) == 1:
                        diag["layer"] = "role+name(overlap)"
                        diag["score"] = pool[0]["score"]
                        return loc.nth(pool[0]["index"]), diag
                    # 位置有细微偏移但 tag 和文本都一样 → 还是重复 DOM（水合动画中间态）
                    t0, t1 = pool[0], pool[1]
                    if (
                        t0.get("score") == t1.get("score")
                        and t0.get("tag") == t1.get("tag")
                        and (t0.get("text") or "")[:20] == (t1.get("text") or "")[:20]
                    ):
                        diag["layer"] = "role+name(tie)"
                        diag["score"] = t0["score"]
                        return loc.nth(t0["index"]), diag
                best = scored[0]
                if len(scored) == 1 or best["score"] - scored[1]["score"] >= 10:
                    diag["layer"] = "role+name(scored)"
                    diag["score"] = best["score"]
                    return loc.nth(best["index"]), diag
                raise AmbiguousElement(key or name, scored)
        except AmbiguousElement:
            raise
        except Exception as exc:  # noqa: BLE001
            diag["tried"].append({"layer": "role+name", "error": f"{type(exc).__name__}"})

    # ---------------------------------------------- 层 1.2：表单 label 关联
    # 表单控件的 label 在正上方 ~24px（next-design Form.Item 的固定结构）。
    # 用「label 文本 + 位置」找控件，比 path 稳得多：
    # label 是业务语义（"袖长类型"），path 会随页面结构变。
    # 淘宝发品页 25 个下拉框 placeholder 全是"请选择"，这一层是唯一的语义依据。
    label = str(entry.get("label") or "").strip()
    tag_name = str(entry.get("tag") or "").strip().lower()
    if label and tag_name in ("input", "select", "textarea"):
        try:
            idx = await page.evaluate(_JS_FIND_BY_LABEL, [label, tag_name])
            if isinstance(idx, int) and idx >= 0:
                diag["tried"].append(
                    {"layer": "label", "count": 1, "sel": f'{tag_name} @ label {label!r} [#{idx}]'}
                )
                diag["layer"] = "label"
                return page.locator(tag_name).nth(idx), diag
            diag["tried"].append(
                {"layer": "label", "count": 0, "sel": f'{tag_name} @ label {label!r}'}
            )
        except Exception as exc:  # noqa: BLE001
            diag["tried"].append({"layer": "label", "error": f"{type(exc).__name__}"})

    # ---------------------------------------------- 层 1.5：无 role 的纯文本元素
    # span / small / div 这类没有语义 role，只能靠内容定位。
    # 它们的 name 就是自身文本，get_by_text 比 path 稳得多（改结构不影响）。
    if not role and name and len(name) >= 2:
        try:
            snip = _simplify_name(name)[:40] or name[:40]
            loc = page.get_by_text(snip, exact=False)
            n = await _count(loc)
            diag["tried"].append({"layer": "text", "count": n, "sel": f"text={snip!r}"})
            if n == 1:
                diag["layer"] = "text"
                return loc, diag
            if n > 1:
                scored = await _score_candidates(loc, n, entry)
                if scored and (len(scored) == 1 or scored[0]["score"] - scored[1]["score"] >= 10):
                    diag["layer"] = "text(scored)"
                    diag["score"] = scored[0]["score"]
                    return loc.nth(scored[0]["index"]), diag
        except Exception as exc:  # noqa: BLE001
            diag["tried"].append({"layer": "text", "error": f"{type(exc).__name__}"})

    # ---------------------------------------------- 层 2：属性
    base = tag or "*"
    for attr_name, css, weight in _attr_selectors(attrs):
        try:
            loc = page.locator(base + css)
            n = await _count(loc)
            diag["tried"].append({"layer": f"attr:{attr_name}", "count": n, "sel": base + css})
            if n == 1:
                diag["layer"] = f"attr:{attr_name}"
                return loc, diag
            if n > 1:
                # 多个同属性：用 role/name 再筛一遍
                if role or name:
                    narrower = await _narrow(loc, role, name)
                    if narrower is not None:
                        diag["layer"] = f"attr:{attr_name}+semantic"
                        return narrower, diag
                diag["tried"][-1]["note"] = "多个候选，继续降级"
        except Exception as exc:  # noqa: BLE001
            diag["tried"].append({"layer": f"attr:{attr_name}", "error": f"{type(exc).__name__}"})

    # ---------------------------------------------- 层 3：显式 css
    css = (entry.get("css") or "").strip()
    if css:
        try:
            loc = page.locator(css)
            n = await _count(loc)
            diag["tried"].append({"layer": "css", "count": n, "sel": css})
            if n == 1:
                diag["layer"] = "css"
                return loc, diag
        except Exception as exc:  # noqa: BLE001
            diag["tried"].append({"layer": "css", "error": f"{type(exc).__name__}"})

    # ---------------------------------------------- 层 4：path
    path = entry.get("path") or []
    if path:
        sel = " > ".join(path)
        try:
            loc = page.locator(sel)
            n = await _count(loc)
            diag["tried"].append({"layer": "path", "count": n, "sel": sel[:120]})
            if n == 1:
                # path 命中 ≠ 是原来那个元素：文案变了但结构没变时，会在这里静默点错。
                # 有 name 就做一次内容校验，对不上宁可失败也不点错。
                verified = True
                if name and len(_simplify_name(name)) >= 2:
                    try:
                        got = ((await loc.nth(0).inner_text()) or "").strip()[:80]
                        verified = _text_overlap(name, got)
                        if not verified:
                            diag["tried"][-1]["note"] = (
                                f"内容不符（原 {name[:14]!r} / 现 {got[:14]!r}），拒绝该命中"
                            )
                    except Exception:  # noqa: BLE001 - 读不到文本就放行
                        verified = True
                if verified:
                    diag["layer"] = "path"
                    diag["unverified"] = True  # 已退化层，体检时应重拾
                    return loc, diag
        except Exception as exc:  # noqa: BLE001
            diag["tried"].append({"layer": "path", "error": f"{type(exc).__name__}"})

    # ---------------------------------------------- 层 5：邻居文本
    sibs = [str(s) for s in (entry.get("siblings") or []) if str(s).strip()]
    if sibs:
        for sib_text in sibs[:3]:
            try:
                # 先找包含这段邻居文本的容器，再在容器里按 tag 找
                container = page.locator("*", has_text=sib_text).last
                if await _count(container) == 0:
                    continue
                cand = container.locator(base)
                n = await _count(cand)
                diag["tried"].append({"layer": f"sibling:{sib_text[:16]}", "count": n})
                if n == 1:
                    diag["layer"] = f"sibling:{sib_text[:16]}"
                    return cand, diag
            except Exception:  # noqa: BLE001
                continue

    # ---------------------------------------------- 层 6：几何兜底
    geo = entry.get("geometry") or {}
    if geo.get("x") or geo.get("y"):
        try:
            loc = await _nearest_by_geometry(page, base, int(geo.get("x", 0)), int(geo.get("y", 0)))
            if loc is not None:
                diag["tried"].append({"layer": "geometry", "count": 1})
                diag["layer"] = "geometry"
                return loc, diag
        except Exception as exc:  # noqa: BLE001
            diag["tried"].append({"layer": "geometry", "error": f"{type(exc).__name__}"})

    # 全部失败：页面可能还在渲染，等一会儿重试一次再判死刑
    # （实测：web_open until=dom 返回后页面内容还没画完，六层全找不到）
    if _attempt < 1:
        import asyncio

        await asyncio.sleep(1.0)
        return await resolve_fingerprint(
            page, entry, key=key, limit_ms=limit_ms, allow_multi=allow_multi, _attempt=_attempt + 1
        )

    # 重试也失败：把诊断带出去，方便判断是"页面变了"还是"指纹质量差"
    diag["layer"] = None
    return None, diag


async def _narrow(loc: Any, role: str, name: str) -> Any | None:
    """在多个候选里用 role/name 再筛一次（属性命中多个时的收敛）。"""
    try:
        n = await loc.count()
        for i in range(min(n, 30)):
            el = loc.nth(i)
            got_role = await el.evaluate(
                "e => e.getAttribute('role') || ({BUTTON:'button',A:'link'}[e.tagName] || null)"
            )
            got_name = (
                (await el.get_attribute("aria-label"))
                or ((await el.inner_text()) or "").strip()
            )
            if role and got_role and got_role != role:
                continue
            if name and name not in (got_name or ""):
                continue
            return el
    except Exception:  # noqa: BLE001
        return None
    return None


async def _score_candidates(loc: Any, n: int, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """对多个候选打分（用元素库里存的指纹逐维比对）。

    隐藏元素额外降权：淘宝这类后台的响应式布局会保留一份不可见的菜单副本，
    两个「发布商品」指纹完全一样，只能靠可见性区分。
    """
    want_attrs = entry.get("attrs") or {}
    want_path = entry.get("path") or []
    want_sibs = [str(s) for s in (entry.get("siblings") or [])]
    want_geo = entry.get("geometry") or {}
    role = (entry.get("role") or "").strip()
    name = (entry.get("name") or "").strip()

    out: list[dict[str, Any]] = []
    for i in range(min(n, 30)):
        try:
            el = loc.nth(i)
            score = 0
            detail: dict[str, int] = {}

            got = await el.evaluate(
                """e => {
                    const attrs = {};
                    for (const a of e.attributes) attrs[a.name] = a.value;
                    const path = [];
                    let n = e, d = 0;
                    while (n && n.nodeType === 1 && d < 12) {
                        const p = n.parentElement;
                        const same = p ? Array.from(p.children).filter(c => c.tagName === n.tagName) : [];
                        path.unshift(same.length <= 1 ? n.tagName.toLowerCase()
                                   : n.tagName.toLowerCase() + ":nth-of-type(" + (same.indexOf(n) + 1) + ")");
                        n = p; d++;
                    }
                    const r = e.getBoundingClientRect();
                    return {
                        role: e.getAttribute('role') || ({BUTTON:'button',A:'link',INPUT:'textbox',SELECT:'combobox'}[e.tagName] || null),
                        name: (e.getAttribute('aria-label') || e.innerText || '').trim().slice(0, 80),
                        attrs: attrs, path: path,
                        x: Math.round(r.x + (window.scrollX||0)), y: Math.round(r.y + (window.scrollY||0)),
                    };
                }"""
            )

            if role and got.get("role") == role:
                score += SCORE["role"]; detail["role"] = SCORE["role"]
            if name and name[:20] in (got.get("name") or ""):
                score += SCORE["name"]; detail["name"] = SCORE["name"]

            gattrs = got.get("attrs") or {}
            for k, w in (("id", SCORE["attrs_id"]), ("data-testid", SCORE["attrs_testid"]),
                         ("name", SCORE["attrs_name"]), ("type", SCORE["attrs_type"])):
                if want_attrs.get(k) and gattrs.get(k) == want_attrs[k]:
                    score += w; detail[f"attr:{k}"] = w

            gpath = got.get("path") or []
            if want_path and gpath[-3:] == want_path[-3:]:
                score += SCORE["path"]; detail["path"] = SCORE["path"]

            gx, gy = int(got.get("x", 0)), int(got.get("y", 0))
            if want_geo and (abs(gx - int(want_geo.get("x", 0))) <= 30
                             and abs(gy - int(want_geo.get("y", 0))) <= 30):
                score += SCORE["geometry"]; detail["geometry"] = SCORE["geometry"]

            text = ((await el.inner_text()) or "").strip()[:60]
            try:
                vis = await el.is_visible()
            except Exception:  # noqa: BLE001
                vis = True
            if vis:
                score += 10
                detail["visible"] = 10
            out.append({"index": i, "score": score, "detail": detail, "text": text,
                        "visible": vis, "x": gx, "y": gy,
                        "tag": (await el.evaluate("e => e.tagName.toLowerCase()")) or "?"})
        except Exception:  # noqa: BLE001 - 单个候选读失败不影响整体
            continue

    out.sort(key=lambda c: c["score"], reverse=True)
    return out


async def _nearest_by_geometry(page: Any, base: str, x: int, y: int) -> Any | None:
    """坐标最近的元素（文档坐标，容差 40px）。"""
    try:
        idx = await page.evaluate(
            """([base, x, y]) => {
                const els = Array.from(document.querySelectorAll(base || '*'));
                let best = -1, bestD = 1e18;
                els.forEach((e, i) => {
                    const r = e.getBoundingClientRect();
                    const cx = r.x + (window.scrollX || 0) + r.width / 2;
                    const cy = r.y + (window.scrollY || 0) + r.height / 2;
                    const d = Math.hypot(cx - x, cy - y);
                    if (d < bestD) { bestD = d; best = i; }
                });
                return bestD <= 40 ? best : -1;
            }""",
            [base, x, y],
        )
        if isinstance(idx, int) and idx >= 0:
            return page.locator(base or "*").nth(idx)
    except Exception:  # noqa: BLE001
        return None
    return None
