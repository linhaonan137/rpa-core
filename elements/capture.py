"""元素拾取器（E04）：在页面里抓一个元素的六维身份，写进元素库。

这是元素库冷启动的唯一入口 —— 没有它，元素库只能手写 JSON。

用法（引擎节点 / MCP / 调试脚本共用）：

    entry = await capture(page, "#submit", key="submit_order")
    store.add(entry)

六层一次 evaluate 拿全（成本：一次往返）：
  1 语义   role（显式 > 标签推断）/ name（aria-label > 文本 > value）
  2 属性   全量 attrs
  3 辅助   并在 attrs 里（placeholder / aria-label / title）
  4 结构   path（父链 nth-child，最多 12 层）+ frame 链
  5 邻居   同父节点的前后兄弟文本（最多 5 个）
  6 几何   文档坐标（视口坐标 + 滚动量）—— 这是 getBoundingClientRect 的原值
"""

from __future__ import annotations

from typing import Any

from .store import ElementEntry, ElementError, ElementStore, Fingerprint, Geometry

__all__ = ["JS_CAPTURE", "capture", "validate_raw", "raw_to_entry"]


# ---------------------------------------------------------------- 抓取脚本

JS_CAPTURE = r"""
(el) => {
  const MAX_DEPTH = 12;

  // ---------------------------------------------------------- 第 0 层：表单 label
  // 表单控件自身没有名字时，找**正上方同一列**的标签文本。
  // 阿里 next-design 的 Form.Item 是「label 在上、控件在下，垂直间距 ~24px」，
  // 淘宝发品页 25 个下拉框 placeholder 全是"请选择/请输入"、无 id 无 name，
  // 不这样找完全无法区分（实测间距 24px）。
  const labelOf = (e) => {
    if (!/^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)) return "";
    const r0 = e.getBoundingClientRect();
    let best = "", bestDy = 999;
    for (const c of document.querySelectorAll("label, dt, th, span, div")) {
      if (c === e || c.contains(e)) continue;
      const cr = c.getBoundingClientRect();
      if (cr.width < 8 || cr.height < 6) continue;
      const dy = r0.y - cr.y;
      if (dy < 2 || dy > 34) continue;
      if (cr.x > r0.x + r0.width || cr.x + cr.width < r0.x) continue;
      // 只要"自己带文本"的元素，避免抓到整块容器
      const direct = Array.from(c.childNodes)
        .filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join("").trim();
      if (!direct || direct.length < 2 || direct.length > 16) continue;
      if (/^[\*＊]|^(重要|必填|选填)$/.test(direct)) continue;
      if (dy < bestDy) { best = direct; bestDy = dy; }
    }
    return best;
  };

  // ---------------------------------------------------------- 第 1 层：可访问名
  // 简化版 accname：aria-labelledby > aria-label > label[for] > 包裹 label >
  //                 placeholder/title/alt > 有值的 value > 文本内容
  // 为什么不用 el.value 兜底：空输入框 value 是空串，会导致 name 为空，
  // 生成 role=textbox[name=""] 匹配到全页所有输入框。
  const accName = (e) => {
    const lb = e.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map(id => {
        const n = document.getElementById(id);
        return n ? (n.innerText || n.textContent || "").trim() : "";
      }).filter(Boolean).join(" ").trim();
      if (t) return t;
    }
    const al = (e.getAttribute("aria-label") || "").trim();
    if (al) return al;
    if (e.id) {
      try {
        const lab = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
        if (lab) { const t = (lab.innerText || "").trim(); if (t) return t; }
      } catch (_) {}
    }
    const wrap = e.closest ? e.closest("label") : null;
    if (wrap && wrap !== e) { const t = (wrap.innerText || "").trim(); if (t) return t; }
    // 表单控件优先用「上方 label」：placeholder 往往 20 个框都是"请选择"，
    // 毫无区分度；上方 label 才是真名字（袖长类型 / 上市年份季节 / 功能…）
    const lf = labelOf(e);
    if (lf) return lf;
    for (const a of ["placeholder", "title", "alt"]) {
      const v = (e.getAttribute(a) || "").trim();
      if (v) return v;
    }
    if (e.tagName === "INPUT" && e.value && e.value.trim()) return e.value.trim();
    return (e.innerText || "").trim();
  };

  // ---------------------------------------------------------- 第 1 层：role
  // 按 input 的 type 细分（原来 INPUT 一律 textbox，单选框会冒充输入框）
  const accRole = (e) => {
    const explicit = e.getAttribute("role");
    if (explicit) return explicit;
    const tag = e.tagName;
    const type = (e.getAttribute("type") || "").toLowerCase();
    if (tag === "INPUT") {
      if (type === "hidden") return null;
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "submit" || type === "button" || type === "reset" || type === "image") return "button";
      if (type === "search") return "searchbox";
      if (type === "range") return "slider";
      if (type === "file") return "button";
      return "textbox";
    }
    if (tag === "BUTTON") return "button";
    if (tag === "A") return e.getAttribute("href") !== null ? "link" : null;
    if (tag === "SELECT") return e.multiple ? "listbox" : "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "IMG") return "img";
    const map = {
      H1: "heading", H2: "heading", H3: "heading", H4: "heading", H5: "heading", H6: "heading",
      NAV: "navigation", MAIN: "main", ASIDE: "complementary", FORM: "form",
      TABLE: "table", UL: "list", OL: "list", LI: "listitem", P: "paragraph", DIALOG: "dialog",
    };
    return map[tag] || null;
  };

  const esc = (s) => { try { return CSS.escape(s); } catch (_) { return s; } };

  // ---------------------------------------------------------- 第 4 层：稳定 path
  // 优先 id / data-testid / name 锚点，其次同标签唯一就走纯 tag，
  // 最后才 nth-of-type。原来的全 nth-child 会因为兄弟增删整体失效。
  const stablePath = (e) => {
    const parts = [];
    let n = e, d = 0;
    while (n && n.nodeType === 1 && d < MAX_DEPTH) {
      if (n.id) { parts.unshift("#" + esc(n.id)); break; }
      const testid = n.getAttribute("data-testid") || n.getAttribute("data-test")
                  || n.getAttribute("data-qa") || n.getAttribute("data-cy");
      if (testid) { parts.unshift(`${n.tagName.toLowerCase()}[data-testid="${testid}"]`); break; }
      const nm = n.getAttribute("name");
      if (nm && /^(INPUT|SELECT|TEXTAREA|A|FORM|IFRAME|META)$/.test(n.tagName)) {
        parts.unshift(`${n.tagName.toLowerCase()}[name="${nm}"]`); break;
      }
      const tag = n.tagName.toLowerCase();
      const parent = n.parentElement;
      const sameTag = parent ? Array.from(parent.children).filter(c => c.tagName === n.tagName) : [];
      if (sameTag.length <= 1) {
        parts.unshift(tag);
      } else {
        parts.unshift(`${tag}:nth-of-type(${sameTag.indexOf(n) + 1})`);
      }
      n = parent; d++;
    }
    return parts;
  };

  // ---------------------------------------------------------- 粒度修正
  // 淘宝后台这类页面：text 匹配到的是内层 span，真正可点的是外层 a[role=button]。
  // 不往上找的话 role 是 null，语义层（role+name）用不上，白白降级到 text/path。
  if (!accRole(el)) {
    let p = el.parentElement, d = 0;
    while (p && d < 4) {
      const rr = accRole(p);
      if (rr && ["button", "link", "menuitem", "tab", "option", "checkbox", "radio"].includes(rr)) {
        el = p;
        break;
      }
      p = p.parentElement;
      d++;
    }
  }

  // ---------------------------------------------------------- 第 6 层：几何
  const r = el.getBoundingClientRect();
  const sx = window.scrollX || 0, sy = window.scrollY || 0;

  // ---------------------------------------------------------- 第 5 层：邻居文本
  const sibs = Array.prototype.slice
    .call(el.parentElement ? el.parentElement.children : [])
    .filter(c => c !== el)
    .slice(0, 5)
    .map(c => (c.innerText || c.value || c.getAttribute("aria-label") || "").trim().slice(0, 30))
    .filter(Boolean);

  const role = accRole(el);
  const name = accName(el).slice(0, 80);
  // ---------------------------------------------------------- iframe 链（自动探测）
  // 元素在 iframe 里时，从 window.frameElement 往上爬就能拿到链路。
  // 跨域 iframe 访问 frameElement 会抛 SecurityError —— 那时只能靠调用方传 frame_chain。
  const autoFrame = () => {
    const chain = [];
    try {
      let w = window, guard = 0;
      while (w && w.frameElement && guard++ < 10) {
        const fe = w.frameElement;
        let sel = "iframe";
        if (fe.id) {
          sel = "#" + (window.CSS && CSS.escape ? CSS.escape(fe.id) : fe.id);
        } else if (fe.getAttribute("name")) {
          sel = 'iframe[name="' + fe.getAttribute("name") + '"]';
        } else if (fe.getAttribute("src")) {
          const s = fe.getAttribute("src").split("?")[0];
          const tail = s.split("/").filter(Boolean).pop() || s;
          sel = 'iframe[src*="' + tail + '"]';
        }
        chain.unshift({ selector: sel });
        w = w.parent;
      }
    } catch (e) { /* 跨域，放弃 */ }
    return chain;
  };

  // ---------------------------------------------------------- 第 2、3 层：属性
  const attrs = {};
  for (const a of el.attributes) attrs[a.name] = a.value;

  return {
    tag: el.tagName.toLowerCase(),
    role: role, name: name, attrs: attrs,
    label: labelOf(el),   // 单独留一份：定位时优先用 label 找控件（比 path 稳）
    path: stablePath(el),
    frame: (window.__rpa_frame_chain && window.__rpa_frame_chain.length)
             ? window.__rpa_frame_chain : autoFrame(),
    siblings: sibs,
    geometry: {
      x: Math.round(r.x + sx), y: Math.round(r.y + sy),
      w: Math.round(r.width), h: Math.round(r.height),
      scrollX: sx, scrollY: sy,
      viewportW: window.innerWidth, viewportH: window.innerHeight,
    },
  };
}
"""


# ---------------------------------------------------------------- 校验


def validate_raw(raw: dict[str, Any]) -> None:
    """抓回来的原始数据必须能落库，否则当场报错（不静默产出坏档案）。"""
    if not isinstance(raw, dict):
        raise ElementError(f"抓取结果不是对象: {type(raw).__name__}")
    if not raw.get("role") and not raw.get("name"):
        raise ElementError("role 和 name 至少一个非空（元素可能不可见或是空壳）")
    if not raw.get("path"):
        raise ElementError("path 为空：元素可能已脱离文档树")


def _tag_from_path(path: list[str]) -> str:
    """从 path 尾部反推 tag（兼容没有 tag 字段的旧抓取结果）。"""
    if not path:
        return ""
    return path[-1].split(":")[0].split("#")[0].split("[")[0]


def raw_to_entry(raw: dict[str, Any], key: str, *, source: str = "picker") -> ElementEntry:
    """把抓取结果转成 ElementEntry。字段是超集：缺的写 None/空，不省键。"""
    validate_raw(raw)
    geo = raw.get("geometry") or {}
    fp = Fingerprint(
        role=raw.get("role"),
        name=raw.get("name"),
        tag=str(raw.get("tag") or _tag_from_path(raw.get("path") or [])),
        attrs={k: str(v) for k, v in (raw.get("attrs") or {}).items()},
        path=list(raw.get("path") or []),
        siblings=list(raw.get("siblings") or []),
        frame=list(raw.get("frame") or []),
        geometry=Geometry(
            x=int(geo.get("x", 0)), y=int(geo.get("y", 0)),
            w=int(geo.get("w", 0)), h=int(geo.get("h", 0)),
            scroll_x=int(geo.get("scrollX", 0)), scroll_y=int(geo.get("scrollY", 0)),
            viewport_w=int(geo.get("viewportW", 0)), viewport_h=int(geo.get("viewportH", 0)),
        ),
    )
    import time as _time

    hints: dict[str, Any] = {}
    lbl = str(raw.get("label") or "").strip()
    if lbl:
        # 表单 label 单独留一份：定位时用「label 文本 + 位置」找控件，
        # 比 path 稳得多（path 会随页面结构变，label 是业务语义）
        hints["label"] = lbl

    return ElementEntry(
        key=key,
        role=fp.role,
        name=fp.name,
        hints=hints,
        fingerprint=fp,
        meta={"created_at": _time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source},
    )


# ---------------------------------------------------------------- 入口


async def _frame_chain_of(page: Any, frame: Any) -> list[dict[str, str]]:
    """从 Frame 对象反查 iframe 选择器链（用 Playwright 的 frame_element）。

    为什么不用 JS 的 window.frameElement：file:// 协议下每个文档是独立源，
    子 frame 访问 frameElement 会拿到 null（实测）；跨域 iframe 同理。
    Playwright 的 frame_element() 走 CDP，不受同源限制。
    """
    chain: list[dict[str, str]] = []
    cur = frame
    guard = 0
    while cur is not None and cur is not page.main_frame and guard < 8:
        guard += 1
        try:
            fe = await cur.frame_element()
            info = await fe.evaluate(
                """el => ({
                    id: el.id || null,
                    name: el.getAttribute('name') || null,
                    src: el.getAttribute('src') || null,
                })"""
            )
        except Exception:  # noqa: BLE001 - 拿不到就放弃这一层
            break
        sel = "iframe"
        if info.get("id"):
            sel = "#" + str(info["id"])
        elif info.get("name"):
            sel = f'iframe[name="{info["name"]}"]'
        elif info.get("src"):
            tail = str(info["src"]).split("?")[0].rstrip("/").split("/")[-1]
            if tail:
                sel = f'iframe[src*="{tail}"]'
        chain.insert(0, {"selector": sel})
        try:
            cur = cur.parent_frame
        except Exception:  # noqa: BLE001
            break
    return chain


async def capture(
    page: Any,
    selector: str | None,
    key: str,
    *,
    store: ElementStore | None = None,
    save: bool = True,
    frame_chain: list[dict] | None = None,
    source: str = "picker",
    limit_ms: int = 10_000,
    locator: Any = None,
) -> ElementEntry:
    """抓一个元素写进元素库。

    selector：任意 Playwright 选择器（css / text= / xpath=）。
    locator：直接给一个已定位好的 locator（走 _locate 的 @el:/role= 写法时用）。
    frame_chain：元素在 iframe 里时由调用方传链路，如 [{"selector": "iframe#pay"}]。
    """
    if not key or not key.replace("_", "").replace("-", "").isalnum():
        raise ElementError(f"非法元素 key: {key!r}（只允许字母数字下划线中划线）")

    if locator is not None:
        raw = await locator.evaluate(JS_CAPTURE, timeout=limit_ms)
        entry = raw_to_entry(raw, key, source=source)
        if frame_chain:
            entry.fingerprint.frame = list(frame_chain)
        if store is not None and save:
            store.add(entry, overwrite=True)
            store.save()
        return entry

    if not selector:
        raise ElementError("capture 需要 selector 或 locator")

    # 先在主 frame 找；找不到就遍历子 frame（iframe 里的元素也能拾取）
    # —— 以前只在 page 上 locator，iframe 内的元素永远超时
    loc = page.locator(selector).first
    raw: dict[str, Any] | None = None
    last_err: Exception | None = None

    async def _try(target_loc: Any, timeout_ms: int) -> dict[str, Any]:
        # 快路径：先 count 判断存不存在（毫秒级），不存在就别白白等超时。
        # 淘宝千牛实测：不预判的话每个候选都要等满 3 秒，一次拾取 5 秒。
        try:
            if await target_loc.count() == 0:
                import asyncio as _aio

                await _aio.sleep(0.3)  # SPA 慢渲染，给一次机会
                if await target_loc.count() == 0:
                    raise ElementError("__not_found__")
        except ElementError:
            raise
        except Exception:  # noqa: BLE001 - count 失败就当能试
            pass

        # 顺序很重要：先把 frame 链注入到该 frame 的 window，再抓取
        # （JS_CAPTURE 会读 window.__rpa_frame_chain）
        if frame_chain:
            await target_loc.evaluate("(c) => { window.__rpa_frame_chain = c }", frame_chain)
        return await target_loc.evaluate(JS_CAPTURE, timeout=timeout_ms)

    try:
        raw = await _try(loc, min(limit_ms, 3000))
    except Exception as exc:  # noqa: BLE001
        last_err = exc

    if raw is None:
        for f in page.frames:
            if f is page.main_frame:
                continue
            try:
                floc = f.locator(selector).first
                raw = await _try(floc, 2000)
                loc = floc
                # frame 链用 Playwright API 反查（JS 的 frameElement 在同源受限场景拿不到）
                if not (raw.get("frame") or []):
                    raw["frame"] = await _frame_chain_of(page, f)
                break
            except Exception:  # noqa: BLE001 - 这个 frame 没有就下一个
                continue

    if raw is None:
        raise ElementError(
            f"找不到元素: {selector}（主 frame 与 {max(len(page.frames) - 1, 0)} 个子 frame 都没找到）"
            + (f"；最后一次错误: {type(last_err).__name__}" if last_err else "")
        )

    entry = raw_to_entry(raw, key, source=source)
    if frame_chain:
        entry.fingerprint.frame = list(frame_chain)
    if store is not None and save:
        store.add(entry, overwrite=True)
        store.save()
    return entry
