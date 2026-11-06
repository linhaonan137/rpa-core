# 引擎问题台账（KNOWN-ISSUES）

> 目的：把"已知但暂时不改"的问题**显式记账**，每条都带"⚠️ 什么时候必须修"。
> 避免以后踩到时重新排查一遍，也避免"知道有问题但没人记得"。
> 关联：[`ENGINE-AUDIT.md`](ENGINE-AUDIT.md)（逐条证据）、[`TAOBAO-PUBLISH.md`](TAOBAO-PUBLISH.md)（流程实操）。

## 1. 模块覆盖矩阵（已完成彻底筛查）

代码量 ≈ **14,300 行**（不含 tests）。**全部审过**。

| 模块 | 文件（行数）| 方式 |
|---|---|---|
| 执行内核 | runner 475 / schema 151 / graph 198 / context 232 / registry 98 | 子代理逐行 |
| 元素库 | capture 392 / matcher 199 / resolve 485 / store 256 | 子代理逐行 |
| 环境·页面 | web.py 的 open/tab/close/frame/ensure_page | 子代理 + 实测 |
| 环境·浏览器 | api/mcp.py attach / browser_lock / paths | 子代理 + 实测 |
| 中间件·鼠标·队列·DB | middleware 854 / real_mouse 310 / taskqueue 212 / db 259 | 子代理逐行 |
| 节点·web | web.py 全部节点（2123）| 子代理逐行 |
| 节点·数据文件网络媒体 | data 897 / files 649 / net 135 / media 354 | 子代理逐行 + 实测 |
| 节点·流程业务 | system 293 / picker 123 / desktop 299 / ai 343 / trigger 411 | 子代理逐行 |
| 自愈 / 录制 / 调度 / 打包 | heal 1190 / recorder 237 / scheduler 153 / stealth 91 / api/package 133 / api/scheduler 203 | 子代理逐行 |

## 2. 已修（27 项）

| 批次 | 内容 |
|---|---|
| 环境一批（8）| 干净起点（不复用 `pages[0]`）、页面淘汰只关自有页、`web_close(all)` 不关 Ant、shutdown 不 close 接管 context、`web_tab` 默认取最新、`_SHARED_BROWSER` 加锁、禁静默 launch、MCP `kind=new` 语义 |
| 环境二批（3）| 跨进程浏览器文件锁、`_save_failure` 取本次页、`_attach_cdp` 标 reused 不写脏端点 |
| 审计一批（4）| K1 异常逃逸不再静默成功、K7 装载期校验节点类型、E4 元素库合并写、D1 DB busy_timeout + 落库失败不改业务结果 |
| 审计二批（7）| E1 强锚点优先、E2 删 tie 静默分支、K3 continue 生效、K2 off 禁止分支、K5 then=skip、K8 expect 三条假绿、E5 frame 链 + P1 打包释放元素库 |
| 深度筛查（5）| B3 `_settle` 解析失败不再抛（曾导致"点击已发生却报失败 → 重试重复点击"）、B2 `go_back/forward` 无历史不再报成功、B4 `web_fill_many` 全失败不再报成功、A2 CSV 落盘双 CR、C1 foreach 注入当前项（`{_item}`/`item_as`）|
| **第三轮（9）**<br>端到端调用链 + 测试盲区视角 | **A1** `limit_ms` 在元素库定位里是死参数（`@el:` 只有 ~1s 窗口，冷启动必假失败）→ 改成按 `limit_ms` 轮询<br>**A2** body 非空但解析失败会静默用流程默认值跑完并报 ok（n8n 参数没进来就发测试商品）→ 现在 400<br>**A3** `_attach_shared` 在 `try` 外，浏览器不可达 → 裸 500 且无审计记录 → 现在 503 + 落库<br>**A4** `pages_before` 在锁外快照 → 会关掉等锁期间别人新开的页 → 快照移进锁内 + 只关 `_OWNED_PAGES`<br>**A5** `web_elem_info(attr=count)` 不等待、0 也成功（而它是 `draft_hits` 业务闸门）→ 加 `wait_for(attached)`<br>**B1** `_SubGraph.start` 兜底取 set 任意元素 → **嵌套循环随机跑 4 或 6 遍**（实测 6 次进程结果跳动）→ 取 loop 出口后继<br>**B2** `ctx._loop_items` 单值不是栈 → 内层循环 `{_item}` 取错行、外层残留 `None` → 进出栈恢复<br>**B3** `on_fail.then=skip` 在 `mode=retry` 下仍被忽略（K5 只修了 back 那一半）→ 统一判定<br>**B4** `exit="error"` 是永远走不到的假契约 → 从 `Exit` 移除并注明改用 `on_fail` |

## 2.1 元素库能力补强（2026-11-06 15:50）

针对"同构元素分不开、名字被横幅污染、拾取静默取第一个"三个反例：

| 能力 | 实现 | 验证 |
|---|---|---|
| **层 0 容器锚点** | 采集时记 `anchor{label, container, target, index}`；定位时先按 label 筛表单项容器，再在容器内取目标。同构下拉靠字段名区分 | 真页面拾取尺码框：`layer=anchor, candidates=1` |
| **采集语义约束** | label 候选宽度 ≤ 控件 2.2×、左边界对齐 ±32px、必须与控件共享 `form/field/item/wrapper` 容器；不达标标 `name_source=geometry`，定位时**跳过 role+name 层** | 单测 |
| **歧义拒绝** | `web_pick` 匹配 >1 直接失败并返回 `ambiguous=True`（要取第一个必须显式 `allow_first=true`）；MCP 侧不再拿歧义结果往下降级试别的写法 | 真页面：14 个候选被拒绝 |
| **回读校验** | 拾取先 `save=False`，用 `resolve_fingerprint` 回读；定位不到/不唯一就不入库。条目写 `meta.quality{layer,candidates,verified_at}` | 真页面 + 单测 |

**交互配方（第 4 步骨架，已可用）**：`hints.interaction` 存"怎么点"，`web_el_action` 节点读数据自动跑，流程里不再每个字段写一串节点。

```json
"interaction": {
  "open": "click",                   // click | hover | none
  "choose": "first-option",          // first-option | {"index":n} | {"text":"..."} | null
  "option_selector": "div.options-item",
  "close": "click-blank",            // click-blank | click-finish | escape | none
  "close_target": "text=商品属性",
  "wait_ms": 450
}
```

流程里写 `{"type":"web_el_action","args":{"key":"元素名"}}` 即可；规则也可在 `web_pick(interaction={...})` 时直接带上。

**待填规则表**（用户给一行，我写一条数据）：

| 控件 | open | choose | close | close_target |
|---|---|---|---|---|
| 多选下拉（面料等）| click | first-option | click-blank | `text=商品属性` |
| 主图勾选框 | hover | — | click-blank | `text=基础上传要求` |
| 尺码弹层 | click | `{"text":"175/88A"}` | click-finish | — |
| 颜色面板 | click | — | escape | — |

## 3. 延后（⚠️ 到最后期限必须先修）

### 3.1 接 n8n / 批量上架之前

| 编号 | 问题 | 现在不修的理由 | ⚠️ 必须修的时机 |
|---|---|---|---|
| A1 | 文本写文件非原子（`open("w")` 先截断，编码失败/断电即毁原文件）| 当前流程不写业务文件 | 开始用 `fs_write` / CSV 落盘跑批量时 |
| A3 | Excel 读：合并单元格 + `data_only=True` 静默返回 `None` | 当前清单没用 Excel | 用 Excel 做商品清单时（合并单元格必踩）|
| A4 | Excel 写：整本 load/save 丢图表、非原子覆盖整本工作簿 | 同上 | 用 `data_sheet_write` 改既有 xlsx 时 |
| D1 | 录制器明文写密码/手机号进 `flows/*.json`（已 git 跟踪）| 目前不靠手动录制 | **任何一次录制含敏感字段之前** |
| Q1/Q2 | 队列 `_trim` 删活记录（幽灵执行）、无持久化/无超时 | 单进程串行不触发 | **n8n 并发触发之前** |

### 3.2 启用对应功能之前

| 编号 | 问题 | ⚠️ 必须修的时机 |
|---|---|---|
| R1/R2 | 真实鼠标不校验前台窗口 / 无视口约束 / 不看 `SendInput` 返回值 | 第一次用 `mode=real` 之前 |
| B1 | `web_click` human 分支忽略 `kind`、real 分支吞 `follow_new_tab`、`hold_ms` 未 resolve | 同上 |
| B5 | `web_download` 的 `limit_ms` 无效、吞掉点击异常、404 页当成功 | 第一次用 `web_download` 之前 |
| A5 | `net_http` 无重试、`timeout` 非总时长、响应全量进内存、GBK 乱码 | 用它调第三方接口之前 |
| A6 | 媒体下载 `kill` 后可能挂住；m3u8 把旧文件当本轮产物 | 用 `media_*` 之前 |
| C2 | `ui_text` 不校验剪贴板归属（失败时返回上一次复制的任意内容）| 用桌面节点之前 |
| C3 | `desktop._find` 用 `PaneControl` 定位 → 按 Name 找不到任何按钮/输入框 | 用 `ui_focus/ui_text/ui_table` 之前 |
| C4 | `if` 比较无类型归一（`"false"` 判真、`>` 遇日期直接崩）| 用 `if` 做金额/日期/开关判断之前 |
| K4 | `on_fail=back` 回退会重跑兄弟分支 | 用 `mode=back` 之前 |
| K6 | `start_at` 续跑不保存变量/页面状态 | 用断点续跑或自愈重跑之前 |
| K9 | `resolve` 把 `{return}` 当变量误伤 | 用 `sys_py` / `web_exec` 跑带花括号代码之前 |
| D2 | 打包整目录 `flows`（含 `_meta` / `.versions` / `_schedule.json`）| 交付打包之前 |
| D3 | `heal` 盲写 `node.args["target"]`、LLM 散文当选择器 | 接入自愈之前 |
| D4 | 定时任务全量回写覆盖外部增删、`daily_at` 按分钟精确匹配会漏跑 | 用内置 `rpa schedule`（若不用 n8n）之前 |

### 3.3 明确不改

| 编号 | 问题 | 理由 |
|---|---|---|
| E3 | geometry 兜底层存左上角、比中心点 | 只是第八层兜底，前七层够用；改版导致定位失败率上升时再动 |
| E7 | matcher 与 resolve 两套打分器 | 能工作，统一属重构级 |
| E8 | `is_stale` 是累计不是连续、生产不记 hit | 不用 `element_drop(stale_only)` 就没影响 |
| K10 | registry 同模块重复注册静默覆盖 | 要人为写重复注册才踩 |
| M1/M2 | middleware 一半函数生产无人调用；`focus_latest` 取 `pages[-1]` 无归属校验 | 死代码，不影响运行 |
| P2 | `_MAX_PAGES_PER_CONTEXT` 无 LRU | 只关自有页 + 上限 3，够用 |

## 4. 使用方式

- 排障前先搜本表：同一问题不重复排查。
- 踩到 §3.1/§3.2 里的"时机"时，**先修再继续**，别绕。
- 新增已知问题：先记这里，标注"为什么现在不改 + 何时必须修"。
- 每次改动后跑 `pytest tests -q` + 至少一次端到端回放（当前基线：`qn_publish_draft` 27~30s 全绿）。
