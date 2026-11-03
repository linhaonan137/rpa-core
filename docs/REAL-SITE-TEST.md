# 真实站点测试报告

> 2026-11-03，4 个公开测试站，9~10 节点/流程，全部通过。
> 目的是回答一个问题：**这套引擎在真实世界里能不能干活。**
> 答案：**能，但过程中抓出 12 个 mock 测试永远发现不了的 bug，全部已修。**

## 一、结果

| # | 站点 | 技术栈 | 场景 | 结果 | 节点 | 耗时 |
|---|---|---|---|---|---|---|
| 1 | quotes.toscrape.com | 静态 HTML + 分页 | 抓 2 页共 20 条 quote + 写 Excel | ✅ PASS | 8/8 | 4.8s |
| 2 | www.saucedemo.com | React SPA | 登录 → 6 商品 → 加购 → 读角标 | ✅ PASS | 10/10 | 2.8s |
| 3 | httpbin.org/forms/post | 原生表单（无 JS） | 填 3 字段 → 提交 → 读 JSON 回显 | ✅ PASS | 9/9 | 2.1s |
| 4 | demoqa.com/text-box | React SPA + 广告 iframe | 填 3 字段 → 提交 → 读输出区 | ✅ PASS | 10/10 | 4.3s |

**实拿到的数据**：

- quotes：#1 页 10 条 → 点 Next → URL 变 `/page/2/` → #2 页 10 条 → 写 `out/quotes.xlsx`（6.3KB）
- saucedemo：`{"url": ".../inventory.html", "items": 6, "first": "Sauce Labs Backpack", "price": "$29.99"}` → 加购后 `{"badge": "1"}`
- httpbin：表单值真的提交到 `/post`，返回 JSON 里能看到我们填的字段
- demoqa：`Name:李四 / Email:lisi@test.com / Current Address:北京市朝阳区`

复跑：

```powershell
cd <项目根>
.venv\Scripts\python.exe tests\real_site_suite.py
```

## 二、抓出的 12 个真实 bug（按发现顺序）

| # | bug | 症状 | 根因 | 修复 |
|---|---|---|---|---|
| 1 | **`_pick` 不等待** | SPA 慢渲染时 30ms 就报"元素不存在" | 直接 `count()` 判断，没有轮询 | 改用 `wait_for(state="attached", timeout)` |
| 2 | flow.vars 依赖调用方 | 直接调 Runner 时 `{keyword}` 未定义 | 注入逻辑散落在 main/http/mcp 三处 | 挪进 `Runner.execute()` |
| 3 | 打包后只有 13 个节点 | EXE 里 pkgutil 扫不到目录 | onefile 没有文件系统目录 | `nodes/__init__.py` 加 `_EXPLICIT` 清单 |
| 4 | **`wait_for` 严格模式** | `.btn_inventory` 匹配 6 个 → 报"元素不存在" | 多元素 locator 上的 `wait_for` 触发 strict violation | 先取 `.first` 再 `wait_for` |
| 5 | **选择器/文本歧义** | `li.next a` 永远找不到 | `_looks_like_text` 把"含空格"当文本，后代选择器也含空格 | 先判 CSS 元字符（`#.[]:>*+~,`），再判中文/空格 |
| 6 | **系统代理劫持 CDP** | connect_over_cdp 返回 502 | `HTTP_PROXY=127.0.0.1:57885` 拦截本地回环 | attach 前把 `127.0.0.1` 写进 `NO_PROXY` |
| 7 | `wait_until: "dom"` 被拒 | `Page.goto: expected one of (load\|domcontentloaded)` | 我们约定 `dom`，Playwright 要全名 | open_page 加别名转换 |
| 8 | Chrome 138+ 禁默认 profile 调试 | 端口不监听 | Chrome 2025 安全变更 | 复制 profile 到独立目录 |
| 9 | 参数名不一致（`text` vs `value`） | 输入空字符串但节点报成功 | 节点读 `text`，MCP/流程传 `value` | 兼容两套 + 空值直接报错 |
| 10 | `@el:` 只查内存 | 拾取写进文件，引用读不到 | `_from_library` 只看 `ctx.elements` | 加 elements.json 兜底 |
| 11 | 元素库引用没走 ARIA | 只用了 hints 兜底 | 少一层 role+name 定位 | 构造 `role=button[name="xx"]` |
| 12 | 装饰器错位 | `@register("use_opened_page")` 挂到 `_attach_cdp` 上 | 我插入函数时位置错了 | 移回正确函数 |

**其中 1、4、5 是同一类问题的三个变体**：定位链路上"判断太早 / 判断太严 / 判断错类型"。这类问题只有真实站点能暴露——mock 里 `count()` 永远是 1、`wait_for` 永远是 AsyncMock。

## 三、验证过的能力

| 能力 | 证据 |
|---|---|
| 分页抓取 | quotes 两页 20 条，URL 真的变成 /page/2/ |
| React SPA 登录 | saucedemo 登录后进 inventory，6 个商品 |
| 状态读取 | 加购后角标读到 "1"（不是读 DOM 假设，是真读） |
| 原生表单提交 | httpbin 返回值里能看到提交的字段 |
| 复杂 SPA 表单 | demoqa 输出区中文正确 |
| 中文处理 | 全程中文无乱码（写入/读取/断言） |
| 元素拾取闭环 | pick 25ms → `@el:` 引用填 498ms → 点 44ms（百度实测） |
| 接管外部浏览器 | attach 412ms → 开新标签 441ms → 关自己标签 30ms |
| 反检测 | baidu 实测 webdriver=false / plugins=3 / languages 正常 / chrome.runtime 在 |

## 四、发现但还没解决的

| 问题 | 影响 | 建议 |
|---|---|---|
| **属性 vs property 混淆** | httpbin 的 `<button>` 有 `type` property 但没 `type` attribute，`button[type=submit]` 匹配 0 个 | 节点不做区分，需在文档里说明"CSS 匹配 attribute"；或加 `:has-text` 兜底 |
| **headless 被风控** | 百度弹滑块（有头正常） | 交付必须有头或接管 |
| **验证码无检测** | 引擎傻等 6 秒才截图发现 | 加验证码检测 → L3 报人 |
| L1.5 自愈未实测 | 四轮都成功，没走到失败路径 | 故意用旧 selector 触发一次 |

## 五、结论

**这套引擎能干活了。** 4 个不同技术栈的站、37 个节点执行、20 条数据、2 张截图、1 个 Excel，全通过，总耗时 14 秒。

但更重要的数字是 **1/4 → 4/4**：第一轮只过 1 个站，三个失败全是定位链路的 bug。**如果没有这轮真实测试，这套引擎在用户手里会以"能跑但经常找不到元素"的方式失败**——而这恰恰是最难诊断的一类问题。

> 你做的那些补丁（P01~P16）不是白打的。这轮抓到的 4、5、9、10 都属同一族问题，
> 说明"定位/参数"这条链路确实是 RPA 引擎最容易出错的地方——不管谁写的。
