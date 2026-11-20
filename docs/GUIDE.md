# 写流程指南：等待策略 + 站点实操

> 写流程前读这一页。分两部分：**通用等待规则**（任何站点都适用）和**淘宝发品的实测要点**。

---

# 第一部分：等待与稳定性

## 核心一句话

**能等元素就等元素；元素出现后加 0.3~1 秒随机抖动，再操作。**

```json
{"type": "web_wait", "args": {"target": "a:has-text('发布商品')", "jitter": [400, 1100]}}
```

只有"目标还不存在于任何页面"（如等新标签页创建）才用 `sleep`，且必须用区间：

```json
{"type": "sleep", "args": {"ms": [1200, 2200]}}
```

## 为什么不用固定 sleep

| 问题 | 说明 |
|---|---|
| 慢了浪费 | 页面 0.7 秒就绪，固定等 3 秒白丢 2.3 秒 |
| 快了崩 | 元素没出来就点；**时快时慢最难复现** |
| 节奏机械 | 每次精确在同一毫秒动作，是最明显的机器特征 |

实测（千牛发品选类目）：固定 sleep 20.1s → 等元素+抖动 10.6s。

## 三种等待对象

| 等什么 | 参数 |
|---|---|
| 元素到某状态 | `target` + `kind`（visible / attached / hidden / detached）|
| 页面出现文字 | `text` |
| 地址变化 | `url` |

`limit_ms` 是**最长**等待，是保险不是实耗。

## jitter 建议值

| 场景 | 区间 |
|---|---|
| 普通点击/输入后 | `[300, 900]` |
| 切换页面/提交后 | `[500, 1500]` |
| 敏感操作（下单、发布） | `[800, 2000]` |

**支持 jitter 的节点**：`web_open` / `web_tab` / `web_click` / `web_js`。漏一个就是一处 0ms 反应。

`web_type` 不支持 jitter，它用 `delay_ms` 逐字输入，本身就有节奏。

## 等哪个元素

等"下一步要操作的那个"：
- ❌ 等 `body`（永远可见，等于没等）
- ❌ 等"加载中"消失（动画消失 ≠ 内容到了）
- ✅ 等下一步要点的按钮

## 十条硬规矩

| # | 规矩 | 要点 |
|---|---|---|
| 1 | 元素库先行 | 先 `element_pick` 入库，节点写 `@el:名字`；硬 CSS 改版即废 |
| 2 | React 受控输入用真实键盘 | `web_type(kind="type")`；`fill()` 改不了 React state |
| 3 | 下拉三步判据 | 点开 → 点选项 → 读回填值；**弹层开 ≠ 选上了** |
| 4 | 切标签先清历史标签 | `web_tab` 按 title 匹配会撞旧标签 |
| 5 | 敏感点击用真实鼠标 | `mode=real`；浏览类用默认 `cdp` |
| 6 | 用 `expect` 断言业务成功 | 节点 ok 只代表脚本跑完 |
| 7 | 易碎节点配 `on_fail` | 但**不可逆操作（提交/支付）永不可盲目 retry** |
| 8 | 输出必须 `save_as` | `{n2}` 不是合法引用 |
| 9 | 浮层遮挡两个解法 | `force=true` → 再不行 `web_js` 直接 click |
| 10 | iframe 边界 | `web_scan` 只扫主文档；进出用 `web_switch(kind=iframe/main)` |

## 可逆 vs 不可逆

- 可逆（导航、填表、开关浮层）→ `on_fail=retry`
- 外部承诺（提交订单、发布商品）→ **绝不盲目重试**，先做幂等检查

## 排查顺序

1. `read_failure(run_id)` 看失败节点 + 截图，先确认 `page_url` 对不对
2. 选择器（`web_has`）→ frame → 视口（滚动）→ 浮层（`force`/JS）→ 渲染（等文案）
3. `element_verify()` 看 `degraded` 列表，重拾元素

## 无人值守前置

- 专用执行机，别和日常工作站混用
- 固定分辨率/缩放/窗口布局
- 阻止休眠锁屏；有头浏览器不能丢
- 登录态浏览器常驻（`RPA_CDP` + `out/_last_cdp.txt` 自动找回）

---

# 第二部分：淘宝发品实测

> 千牛/淘宝商品发布页的记录，不是通用教程。关联流程 `flows/qn_publish_draft.json`。

## 链路与现状

工作台 → 商品 → 发布商品 → 选类目（3 级）→ 填表页 → 保存草稿。

`qn_publish_draft` 30/30 全过，连跑 3 次 31~34s 全绿。**25 个必填只填了 5 个**（标题/导购标题/颜色/尺码/价格）。

## 逐节点要点

| 节点 | 注意点 | 做对了长什么样 |
|---|---|---|
| `web_open` | `until=dom` 够用，`jitter=[500,1500]` | URL = 工作台 |
| 点「发布商品」 | `locator.click()` 报 `intercepts pointer events` → 改 JS `a.click()` | 新标签出现 |
| `sleep` | **唯一**该用处：新页面还没创建 | 标签数 +1 |
| `web_tab` | 按 title 切，**先清历史标签** | URL = category.htm |
| 类目项点击 | 元素是 `p/span/li` **不是 a/button**，用 `textContent` 精确匹配 + `width>0` 过滤隐藏项 | 选中项高亮 |
| 品牌下拉 | 选项是 `div.options-item`；别用 `text=004`（多匹配） | `span.next-select-values` 显示 004 |
| `web_type` 填标题 | **必须 `kind=type`** | 计数器变 `28/60` |
| 导购标题 | **限 10~15 汉字**，超了报错且保存被拦 | 无错误提示 |
| 颜色 | 两级：先「白色系」再「白色」；`text=白色` 会歧义 | 主色框 = `白色` |
| 保存草稿 | 按钮可能 disabled：先查再点；**错误面板为空**才能存 | 草稿箱数量 +1 |

## Fusion 下拉（18 个 `placeholder=请选择`）

**是下拉不是输入框，别往里 type。**

```
点开 input[placeholder='请选择']
 → 点 div.options-item      ← 真正选项；li/[class*=option] 会数到容器（虚高 17 vs 实际 14）
 → 读 .next-select-values   ← 选中值在这；input.value 是搜索框，选中后仍空
 → 多选不自动关 → web_key(Escape)
```

曾误报"5 个下拉验证通过"，实际只点开没选值。

## iframe

全页 0 个 shadow DOM，但 **4 个跨域 iframe**。

| 工具 | 能进 iframe |
|---|---|
| `web_click` / `web_fill` / `web_upload` / `web_read(text/html/list)` | 🟢 |
| `web_read(kind=eval)` | 🟢 走 `content_frame().evaluate` |
| `web_scan` | 🔴 只扫主文档 |

主图上传路径（已验证）：

```
点 div.image-empty → 进 iframe#mainImagesGroup → 点「本地上传」
→ web_fill(kind=upload, value=图片路径) → 点「完成」
```

## 页面现状

```
8455 DOM 节点 / 0 shadow DOM / 4 跨域 iframe
83 个 input：58 可见 / 25 不可见 / 20 readonly / 7 disabled
18 个 placeholder=请选择、25 个必填标记、5 个上传区
```

## 排查速查

| 现象 | 按顺序查 |
|---|---|
| 找不到元素 | 选择器 → frame → 视口 → 浮层 → 没渲染 |
| 填了没生效 | React 受控：换 `web_type(kind=type)`，看计数器 |
| 下拉选不中 | 点的是容器 → 改 `div.options-item`；看 `.next-select-values` |
| 保存没反应 | 按钮 disabled？导购标题超 15 字？错误面板为空？|
| 切错页面 | 清历史 publish/category 标签 |
| 回放跳登录 | 先 `web_open(kind=attach)` 接管已登录浏览器 |
| 重复草稿 | 先跑 `flows/draft_check.json` 查 `draft_hits` |

## 接自动化前必须补

| 缺口 | 状态 |
|---|---|
| 字段覆盖 100% | 🔴 25 必填只填 5 个 |
| 幂等去重 | 🟢 `draft_check.json` |
| 事后断言 | 🟢 `expect{url=publish.htm}` + 查 `draft_hits` |
| 登录态常驻 | 🟡 需重启生效 |
| 连跑稳定性 | 🟢 3 次全绿 |

**业务结果回传**：`POST /run/{flow}` 返回 `vars`（含 `draft_hits`），据此判断"草稿真进库了"，不只看 `ok`。
