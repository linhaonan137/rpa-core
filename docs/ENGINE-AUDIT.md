# 引擎模块审计清单（2026-11-06）

> 方式：3 个子代理按模块并行审查（执行内核 / 元素库 / 中间件·队列·存储）+ 主代理核对 API 层。
> 结论：**26 条问题，其中 P0 10 条**。P0 里有 4 条会直接导致"报成功但业务错"或"写坏数据"，已在本轮修复（见 §5）。

## 1. 汇总

| 模块 | 文件 | P0 | P1 | P2 |
|---|---|---|---|---|
| 执行内核 | runner / schema / graph / context / registry | 3 | 6 | 1 |
| 元素库 | elements/capture·matcher·resolve·store | 4 | 3 | 1 |
| 中间件·队列·存储 | middleware / real_mouse / taskqueue / db / paths | 3 | 3 | 2 |

## 2. P0：会算错业务结果 / 写坏数据

| # | 模块 | 问题 | 证据 | 影响 |
|---|---|---|---|---|
| K1 | 内核 | **节点内异常逃逸被静默吞掉**：`if`/`loop` 节点抛错时不写日志、不置 `should_stop`，`ok` 只由 logs 推导 → 整条流程报 `ok=True, failed=[]` | `runner.py:109-119`、`274-278`、`287-296` | `if` 条件里变量名写错，两条分支都不执行，流程报绿 |
| K2 | 内核 | **`off=true` 用在 if/loop 上会切断整条下游**，仍报成功（分支节点只有 true/false、loop/done 出口，`next=None` 匹配不到任何边）| `runner.py:170-172`、`registry.py:37-38` | 调试时"先把循环关掉"，流程绿但数据没写完 |
| K3 | 内核 | **`continue` 节点是死代码**：`ctx.should_continue` 全仓无读取点，循环里 continue 后本轮剩余步骤照跑 | `runner.py:332-334`、`context.py:76` | "今天已处理过就 continue" 变成再处理一遍 → 重复提交 |
| E1 | 元素库 | **label 层排在 id/testid 之前且命中不校验**：几何猜出的"上方标签"被优先采信，属性层永远轮不到 | `resolve.py:258-268`、`capture.py:43-55,86-87` | 库里的 `qn_price_input` name 就是公告横幅文案，价格可能填进别的框 |
| E2 | 元素库 | **同分 tie 分支静默取第一个**（文档写"并列报歧义"）：input 的 `inner_text` 恒空，18 个同构下拉全部同分同 tag 同文本 | `resolve.py:232-241` | `qn_brand_select` 会命中 18 个候选并静默选第一个 → 品牌选错字段 |
| E3 | 元素库 | **geometry 层原点语义不一致**：采集存左上角，定位比中心点，容差只按 tag 取 40px | `capture.py:224-226`、`resolve.py:517-522` | 120×36 的按钮离自己 63px > 40 → 层 6 对大元素恒空转；对小图标则命中相邻元素 |
| E4 | 元素库 | **整文件读-改-写 + 固定 `.tmp` 名 + 无锁**：`save()` 全量重写构造时的快照 | `store.py:163-171` | 两进程同时写 → 拾取结果被旧快照覆盖（**这就是"web_pick 报 ok 但库里没有"的主因**），极端情况下 `.tmp` 撞车写出半截 JSON |
| R1 | 真实鼠标 | **点前不校验前台窗口，置前失败也不拦**；找不到标题还降级成"任意 Chrome/Edge 窗口" | `real_mouse.py:354-361`、`web.py:1043-1056` | 点击打进终端/编辑器/别的 Chrome，节点仍返回 done |
| R2 | 真实鼠标 | **无视口约束、不看 `SendInput` 返回值**；`bounding_box()` 是视口相对且真实鼠标路径不会自动滚动 | `real_mouse.py:225-233,258-265` | 元素在首屏外时点到桌面/第二块屏；SendInput 失败也报成功 |
| D1 | 存储 | **落库失败两端语义反着**：MCP 静默吞（丢记录），HTTP 直接抛 → 500 → n8n 重试 → **重复提交**；且无 `busy_timeout` | `mcp.py:1756-1765`、`http.py:178-186`、`db.py:64-67` | 业务已成功后因为写库失败被判失败 |

## 3. P1：偶发失败 / 语义与配置不符

| # | 模块 | 问题 | 证据 |
|---|---|---|---|
| K4 | 内核 | `on_fail=back` 的"单分支"守卫用当前波次节点数判断，回退区间可能**重跑兄弟分支** | `runner.py:355-360`、`graph.py:161-168` |
| K5 | 内核 | `on_fail.then="skip"` 实际不生效，重试耗尽后直接停单 | `runner.py:366-370`、`214-225` |
| K6 | 内核 | `start_at` 续跑不保存运行时状态：变量、loop_stack、frame 链、页面全丢 | `runner.py:143-158` |
| K7 | 内核 | **装载期零校验**：节点类型名、args 键名都要跑到那一步才报 | `schema.py:79`、`runner.py:254` |
| K8 | 内核 | `expect` 三条假绿：无 page 直接通过、`state=hidden` 未实现、`target=@el:` 必失败 | `runner.py:391-411` |
| K9 | 内核 | `resolve` 把 `{return}`/`{item}` 当变量误伤；`sys_py` 漏了 `raw=("code",)` | `context.py:154-178`、`system.py:211` |
| E5 | 元素库 | frame 链只在 selector 分支反查，`role+name` / `target=` 分支一律存 `[]` | `capture.py:357-365,404-415` |
| E6 | 元素库 | accname 边界：labelledby 只解一层、无 label 回落 innerText；子串命中不复验 | `capture.py:66-93`、`resolve.py:193-210` |
| E7 | 元素库 | 两套互不相通的打分器；path 维度恒 0；`visible +10` 与身份分混加 | `matcher.py:165-169`、`resolve.py:482-497` |
| Q1 | 队列 | `_trim` 不看状态 → 记录被删但任务还在跑（幽灵执行 → 调用方重投 → 重复） | `taskqueue.py:234-240,88-91` |
| Q2 | 队列 | 队列无持久化、无任务级超时；进程退出静默丢任务 | `taskqueue.py:127` |
| P1 | 路径 | frozen 只释放 `flows`，不释放 `elements.json` → 打包后 `@el:` 全废 | `paths.py:115-118`、`rpa.spec:4` |

## 4. P2：维护性

| # | 模块 | 问题 |
|---|---|---|
| K10 | 内核 | registry 同模块重复注册静默覆盖；`_RAW_FIELDS` 空声明不清旧值 |
| E8 | 元素库 | `is_stale` 是累计不是连续；生产路径几乎不写 hit，`stale_only` 会误删 |
| M1 | 中间件 | `locate`/`wait_ready`/`with_retry`/`scroll_find`/`watch_new_tabs` **生产路径无人调用**，只有测试用；文件头"36 个节点只写业务语义"已不成立 |
| M2 | 中间件 | `focus_latest` 无条件取 `pages[-1]`，可能是用户/另一进程的页 |

## 5. 本轮已修（4 项）

| # | 修法 | 验证 |
|---|---|---|
| K1 | `execute` 的异常逃逸分支补 `NodeLog(ok=False)` + `should_stop`（原来节点内部抛错等于"这一波没发生过"，流程报绿）| 单测 + 主循环 ok 由 logs 推导 |
| K7 | `Runner._preflight()`：装载期校验节点类型，拼错给 `difflib` 候选，并提示"MCP 工具名 ≠ 节点名"；抛 `KeyError` 走既有"流程配置错误"分支 | 坏流程（`web_switch`）→ **HTTP 400**，前面的 `web_open` 都没执行 |
| E4 | `store.save()` 落盘前 reload 并按 key 合并（取 mtime 新者）、tmp 名带 pid | 两个实例交替写，3 条都在 |
| D1 | `DB` 显式 `busy_timeout=30000` + `synchronous=NORMAL`；HTTP 落库失败不再 500，改为 `db_saved:false` + warn 日志 | 正常流程 `db_saved=True` |

`pytest` 全量通过；新增 `tests/test_engine_audit.py` 6 个用例。

## 6. 待办（按收益排序）

| 优先 | 条目 | 理由 |
|---|---|---|
| 0 | R1 + R2（真实鼠标：前台窗口断言 / 视口 clamp / 检查 `SendInput` 返回值）| **P0**，但当前流程走 `mode=cdp` 没触发，故未在本轮修。一旦启用 `mode=real`，点偏会打进别的窗口 |
| 1 | E1 + E2（label 层降权到只做打分 + 删 tie 静默分支）| 直接影响 `@el:` 点错元素；元素库刚落地，现在修最划算 |
| 2 | K3（continue 死代码）+ K2（off 用于分支节点）| 会导致"重复执行"和"静默漏执行" |
| 3 | K8（expect 三条假绿）| 现在业务断言只有 3 处，假绿等于没有断言 |
| 4 | Q1 + Q2（`_trim` 删活记录 / 队列无持久化无超时）| 接 n8n 后并发会踩到 |
| 5 | E3（几何原点）、E5（frame 链丢失）、E7（两套打分器）| 定位精度，改版后才暴露 |
| 6 | P1（frozen 不释放 `elements.json`）| 交付打包时必现 |
