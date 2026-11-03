# LESSONS — 踩过的坑：五类结论，按「再犯一次会疼多久」排序

> 📖 **参考文件**（不是历史废纸）：这些坑来自旧 Agent 项目，但**很多坑在自研内核里会重犯**。
> 新方向入口见 `README.md`；写调度器和自愈之前，值得读一遍。

每一行都是「现象 → 原因 → 正确做法 → 怎么防复发」，不再保留修复过程。

## 一、写坏生产（最贵的一类）

| 现象 | 原因 | 正确做法 | 怎么防复发 |
|---|---|---|---|
| `reset_workflow` 把**另一个流程**整份写进目标，还报成功、退出码 0、记了知识库 | `build/state/baseline.json` 是**进程级单例**，与 wid 无关；备份的输出键正好与验收口径吻合 → 跑一次就 `verdict_s=true` | 调 `reset/restore` 前比对「备份节点 id 集合」与「目标流程节点 id 集合」，不一致或读不到都拒绝（fail-closed） | 工具层 `_backup_mismatch()` + 写回层 `commit()` 节点/边指纹双保险 |
| `patch_nodes` 改了却被判「没改动」，拿改动前的 verdict 写回 | 只靠 `transform` 正则生效时 `changed=[]`，判定只看 `changed` | `effective` 纳入 `transformed>0` | 轨迹白名单加 `transformed`；`test_fix_round7_gates` |
| 录制把**基底副本**的 verdict 当成新流程的验收 | 轨迹里只有 `start_run(基底)` + `create_from_actions`，没跑过新流程 | verdict 必须来自 `create_from_actions` **之后**且 `args.wid` 等于上报的 `workflow_id`，否则 `null` + 退出码 1 | `core.runtime.verdict_after` |
| 模型照抄 user_input 里的原 wid，直接打到生产流程 | 只补默认值、不校验 | 写类 / verdict 类工具显式传的 wid 必须等于会话 wid，否则拒绝；读类放行 | `_GUARDED_TOOLS` + `_WID_TOOLS` |
| 写回时把别人手工改过的流程静默覆盖 | commit 无乐观锁 | 至少比对节点/边指纹，不一致在 PUT **之前**拒绝 | 边界：真正的乐观锁需要接口带 `updatedAt` |

## 二、假成功（比报错更危险）

| 现象 | 原因 | 正确做法 |
|---|---|---|
| `element_exists` 找不到元素也 `success=True` | 上游实现只返回「检查过了」 | 断言节点用 `assert_checkpoint`，别用 `element_exists` 判存在性 |
| `/reset` 在 PUT 失败时仍回 `ok:true` | 只判 `_error` 不判 `_http_error` | 两个都判 |
| 重试耗尽走 `skip` 的节点在 `run_detail` 里完全消失 | 该分支提前 `return`，走不到 `_notify_node_complete` | 在 return 前补记 |
| `create_from_actions` 自动删同名流程 | 生成器先查重再删 | 录制侧先 `list_flows` 查重，重名**拒绝**；查不到列表则 fail-closed |
| 知识库把被拒 / 回滚的补丁记成成功案例 | 只按 `ok` 记，不看 `effective` | 只取 `ok is not False and effective is not False` |

## 三、测试测不到（方法论）

| 教训 | 具体 |
|---|---|
| **属性层打桩测不到函数体** | 392 条全绿的同时 `do_dump` 返回 `None`、`_read_seq` 未定义 —— 三个主干函数零真实调用覆盖。mock 全绿不算数，改完必须跑真实函数体 |
| **自己修 bug 引入的回归比设计缺陷更危险** | 三个 P0 全是「修 bug 时引入」：`return` 缩进、编辑丢内容、`--quiet` 静默没加。字符串替换必须 `assert`，不能 `if` 静默跳过 |
| **CRLF 文件用 `\n` 替换会静默失败** | 项目文件多为 CRLF，含 `\n` 的锚点匹配不上。先读 bytes 判换行符，或改单行锚点 |
| **并行的 tool_calls 时序测不出来** | 单条 `AIMessage` 里 patch + run 并行时，`tool_end` 先后顺序会骗过闸门。用 `tool_start/tool_end` 配成区间、重叠即判 stale，再加一条「串行哨兵」测试（变并行就红） |
| **单元测试覆盖不了跨调用归属** | P0 基本都出在「哪次调用的结果算数」上，必须补时序 / 归属类测试 |

## 四、环境陷阱

| 现象 | 原因 | 正确做法 |
|---|---|---|
| 工具里读不到 wid，护栏整体失效 | `threading.local` 在 ToolNode 的线程池里读不到图线程设的值 | wid 改**进程级** + 锁，约定「一个进程一个会话」 |
| 后台起的服务下一次工具调用就没了 | 沙箱 / 进程模型隔离 | 用后台任务方式起，别指望 `nohup` 存活 |
| `.venv\Scripts\python.exe` 报找不到 `3.13.12\python.exe` | 宿主自带运行时目录被重建（与用哪个 shell 无关） | `pyvenv.cfg` 的 `home` / `executable` 指向随应用分发的同版本 vendor python，或重建 venv |
| 本机 `127.0.0.1` 请求返回 **502** | 系统代理把回环请求也接走了 | `urllib` 一律 `build_opener(ProxyHandler({}))`；`curl` 加 `--noproxy "*"` |
| 切店铺版本后 `look_page(mode=dom)` 给的是上一版页面 | 切版本不重新导航，浏览器停在旧 DOM（`texts` 模式带 url 才会导航） | 切版本后**必须重新导航**再取证 |
| 子代理（brain 模式）每步几十秒 | 延迟是主会话转发开销 | 别拿延迟当质量信号 |
| 子代理把「禁止调用工具」理解成「我不能要求调工具」 | 措辞歧义 | 说「你是**调度决策器**，执行由框架完成」，并给输出示例 |

## 五、认知：工具能力 > Agent 智力

同一份 instruction、同一个 Agent，只加一个生成机制（`optional` 分支），从失败变成功：耗时 243 秒 → 156 秒，结果从「不可重跑」变「通过」。

它试过三种办法全失败：加 `wait_element`（弹窗不来就超时）、用 `execute_js`（**引擎没有这个节点类型，被静默跳过**）、调节点顺序（`patch_nodes` 只能改字段，改不了结构）。

**它知道该怎么做，但工具做不到。想让 Agent 变强，先看工具能不能表达这件事。**
