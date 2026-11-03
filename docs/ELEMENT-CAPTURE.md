# 元素档案：字段定义与抓取实现

> 用途：E01（schema）与 E04（拾取器）的实现依据。
> 一句话：**一个元素存六个维度的身份，按稳定性排序逐级降级定位。**
> 环境：Playwright **1.64.1**（`ariaSnapshot()` 可用；`page.accessibility` 已废弃，不要用）。

## 一、六个维度

| 层 | 字段 | 稳定性 | 失效条件 | 谁能填 |
|---|---|---|---|---|
| 1 语义 | `role` `name` | ★★★★★ | 改文案 | MCP / 拾取器 / 自愈 |
| 2 属性 | `attrs`（id / data-testid / name / type） | ★★★★ | 前端重写 | **拾取器** |
| 3 辅助 | `attrs.placeholder` / `attrs.aria-label` / `attrs.title` | ★★★ | 改辅助属性 | **拾取器** |
| 4 结构 | `path` `frame` | ★★ | 改布局 / 改 iframe | **拾取器** |
| 5 邻居 | `siblings` | ★★ | 改周围文案 | 拾取器 / 自愈 |
| 6 几何 | `geometry` | ★ | 加广告位 / 改窗口 | 拾取器 / 自愈 |

## 二、完整 schema

```jsonc
{
  "key": "submit_order",              // 元素库主键，节点里写 @el:submit_order
  "role": "button",                   // 第 1 层（可空，但 role 和 name 至少一个非空）
  "name": "提交订单",                  // 第 1 层
  "attrs": {                          // 第 2、3 层：原样保存，不做筛选
    "id": "submit-btn",
    "data-testid": "order-submit",
    "name": null,
    "type": "submit",
    "placeholder": null,
    "aria-label": "submit order",
    "title": null
  },
  "path": [                           // 第 4 层：从外到内
    "form#order-form:nth-child(3)",
    "div.actions:nth-child(2)",
    "button:nth-child(1)"
  ],
  "frame": [],                        // 第 4 层：iframe 链，空数组=主文档
  "siblings": ["取消", "保存草稿"],     // 第 5 层：同父节点的邻居文本（最多 5 个）
  "geometry": {                       // 第 6 层：**文档坐标**（已加滚动量）
    "x": 820, "y": 640, "w": 120, "h": 36,
    "scrollX": 0, "scrollY": 120,
    "viewportW": 1440, "viewportH": 900
  },
  "_heal": null,                      // 自愈痕迹：{"ts","from","why","by"}
  "_meta": {                          // 元数据，引擎忽略
    "created_at": "2026-11-03T04:20:00",
    "source": "picker",               // picker | mcp | heal
    "hits": 0, "misses": 0            // 命中/失效计数，用来判断这条要不要淘汰
  }
}
```

**三条硬规则**：

1. **字段是超集，缺的写 `null`，不要省键**。否则读的时候要写一堆 `if key in dict`。
2. **`role` 和 `name` 至少一个非空**，都空说明这条档案无效，校验器直接拒。
3. **`geometry` 存的是文档坐标**（视口坐标 + 滚动量），不是 `getBoundingClientRect()` 的原始值。

## 三、抓取：一个 evaluate 拿全六层

```javascript
// JS_CAPTURE —— 传入元素句柄，返回六层信息
(el) => {
  const MAX_DEPTH = 12;

  // 第 4 层：从外到内的 CSS 路径
  const path = [];
  let n = el;
  while (n && n.nodeType === 1 && path.length < MAX_DEPTH) {
    const tag = n.tagName.toLowerCase();
    const parent = n.parentElement;
    const idx = parent ? [...parent.children].indexOf(n) + 1 : 1;
    path.unshift(n.id ? `${tag}#${n.id}:nth-child(${idx})`
                      : `${tag}:nth-child(${idx})`);
    n = parent;
  }

  // 第 6 层：几何（转文档坐标）
  const r = el.getBoundingClientRect();
  const sx = window.scrollX || 0, sy = window.scrollY || 0;

  // 第 5 层：邻居文本（排除自己，最多 5 个，各截 30 字）
  const sibs = [...(el.parentElement ? el.parentElement.children : [])]
    .filter(c => c !== el)
    .slice(0, 5)
    .map(c => (c.innerText || c.value || '').trim().slice(0, 30))
    .filter(Boolean);

  // 第 1 层：role 优先取显式值，否则按标签推断
  const tagRoleMap = {
    BUTTON: 'button', A: 'link', INPUT: 'textbox',
    SELECT: 'combobox', TEXTAREA: 'textbox', IMG: 'img'
  };
  const role = el.getAttribute('role')
            || tagRoleMap[el.tagName]
            || null;

  // 第 1 层：name 优先 aria-label，其次文本，input 取 value
  const name = (el.getAttribute('aria-label')
             || (el.tagName === 'INPUT' ? el.value : el.innerText)
             || el.getAttribute('placeholder')
             || el.getAttribute('title')
             || '').trim().slice(0, 80);

  // 第 2、3 层：属性全量保存
  const attrs = {};
  for (const a of el.attributes) attrs[a.name] = a.value;

  return {
    role, name, attrs,
    path,
    frame: window.__frameChain || [],       // 由外层注入，见下文
    siblings: sibs,
    geometry: {
      x: Math.round(r.x + sx), y: Math.round(r.y + sy),
      w: Math.round(r.width),  h: Math.round(r.height),
      scrollX: sx, scrollY: sy,
      viewportW: window.innerWidth, viewportH: window.innerHeight
    }
  };
}
```

**成本**：一次 `locator.evaluate()` 往返，比调 6 次 API 快得多。

## 四、iframe 怎么处理

`frame` 链**必须在进入 iframe 前记好**，因为 iframe 内部的 `window` 拿不到外层信息。

```python
# 进 iframe 前把链注入进去
async def capture_in_frame(page, frame_chain: list[dict], locator):
    if frame_chain:
        await locator.evaluate(
            "() => { window.__frameChain = %s }" % json.dumps(frame_chain)
        )
    return await locator.evaluate(JS_CAPTURE)
```

`frame_chain` 长这样：

```json
[{"selector": "iframe#payFrame"}, {"selector": "iframe[name=inner]"}]
```

回放时按链逐层 `frame_locator()` 进去，再按 `path` 找元素。

⚠️ **跨域 iframe 拿不到内容** —— `evaluate` 会被同源策略拦。这种情况只能记 `frame` 链 + 用 `frameLocator` 定位，抓不到属性。要在档案里标 `"crossOrigin": true`，定位时直接跳过第 2~5 层。

## 五、Python 包装

```python
from playwright.async_api import Page, Locator
import json

JS_CAPTURE = open("js_capture.js", encoding="utf-8").read()   # 上面那段

async def capture(page: Page, locator: Locator, key: str,
                  frame_chain: list[dict] | None = None) -> dict:
    if frame_chain:
        await locator.evaluate(
            "(chain) => { window.__frameChain = chain }", frame_chain
        )
    raw = await locator.evaluate(JS_CAPTURE)
    entry = {
        "key": key,
        "role": raw["role"],
        "name": raw["name"],
        "attrs": raw["attrs"],
        "path": raw["path"],
        "frame": frame_chain or [],
        "siblings": raw["siblings"],
        "geometry": raw["geometry"],
        "_heal": None,
        "_meta": {"source": "picker", "hits": 0, "misses": 0},
    }
    validate(entry)
    return entry


def validate(entry: dict) -> None:
    if not entry.get("role") and not entry.get("name"):
        raise ValueError(f"{entry['key']}: role 和 name 不能同时为空")
    for k in ("attrs", "path", "frame", "siblings", "geometry"):
        if k not in entry:
            raise ValueError(f"{entry['key']}: 缺字段 {k}（缺值要写 null，不能省键）")
```

## 六、三个来源怎么填

| 来源 | 能填的层 | 说明 |
|---|---|---|
| **Playwright MCP** | 1（+ 部分 3） | `locator.ariaSnapshot()` 只给 role/name。`ref: e123` **绝不入库**（会话内临时） |
| **拾取器**（本文件） | 1~6 | 一次 evaluate 拿全，是主力 |
| **自愈沉淀** | 补过期层 | 跑成功时对比实际值，更新 `attrs` / `geometry`，写 `_heal` |

**统一口径**：不管谁写，都走 `validate()`，字段集合必须一致。

## 七、定位时的降级顺序

```python
ORDER = ["role+name", "attrs", "path", "siblings", "geometry"]

# 1. get_by_role(role, name=name)
# 2. [data-testid] / #id / [name=] / [placeholder=] / [aria-label=]
# 3. frame_locator(...).locator(path 拼成的 CSS)
# 4. 父容器内按邻居文本缩小范围
# 5. 文档坐标 + 视口提示（最后手段，命中也要记 misses+1）
```

**退到第 5 层还找不到 → 报人，不许继续猜。** 这是 false heal 的第一道防线。

## 八、一个真实条目

```json
{
  "key": "invoice_submit",
  "role": "button",
  "name": "查验",
  "attrs": {"id": "checkBtn", "class": "btn btn-primary", "type": "button"},
  "path": ["div.container:nth-child(2)", "form:nth-child(1)", "div:nth-child(5)", "button:nth-child(2)"],
  "frame": [],
  "siblings": ["重置"],
  "geometry": {"x": 960, "y": 512, "w": 88, "h": 32,
               "scrollX": 0, "scrollY": 0, "viewportW": 1440, "viewportH": 900},
  "_heal": null,
  "_meta": {"created_at": "2026-11-03T04:21:00", "source": "picker", "hits": 0, "misses": 0}
}
```

节点里写：

```json
{"type": "click_element", "data": {"selector": "@el:invoice_submit"}}
```

元素库里改一条，所有引用 `@el:invoice_submit` 的节点同时生效。
