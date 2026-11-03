"""反检测：启动参数 + 运行时注入。

两件事分开做（对应 CHECKLIST X14）：
  ① 启动参数去掉 `AutomationControlled` 的 blink 特征
  ② 运行时注入：navigator.webdriver / chrome.runtime / plugins / languages

**接管指纹浏览器时不要注入**（`apply()` 会检查 ctx 上的标记）——指纹由 Ant
这类浏览器统一管，双重注入反而自相矛盾，更容易被识别。

不追求"完全隐身"。真正的隐身靠指纹浏览器，这里只解决"裸跑一眼假"的问题。
"""

from __future__ import annotations

from typing import Any

#: 启动参数：去掉自动化标志 + 关掉首启弹窗
LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
)

#: 运行时注入脚本。只在"自己 launch"时用。
STEALTH_JS = r"""
(() => {
  // ---------- 1. navigator.webdriver ----------
  try {
    delete Object.getPrototypeOf(navigator).webdriver;
  } catch (e) {}
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => false, configurable: true,
    });
  } catch (e) {}

  // ---------- 2. chrome.runtime（无头下缺失是最明显的破绽）----------
  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        connect: function () {}, sendMessage: function () {},
        id: undefined,
      };
    }
  } catch (e) {}

  // ---------- 3. navigator.plugins（空数组一眼假）----------
  try {
    const mk = (name, filename, desc) => ({
      name, filename, description: desc, length: 1,
    });
    const plugins = [
      mk('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      mk('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      mk('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    ];
    Object.defineProperty(Navigator.prototype, 'plugins', {
      get: () => plugins, configurable: true,
    });
  } catch (e) {}

  // ---------- 4. languages / platform ----------
  try {
    Object.defineProperty(Navigator.prototype, 'languages', {
      get: () => ['zh-CN', 'zh', 'en'], configurable: true,
    });
  } catch (e) {}

  // ---------- 5. 清理可能的自动化残留全局 ----------
  try {
    const bad = ['__webdriver_evaluate', '__selenium_evaluate', '__webdriver_script_function',
                 '__webdriver_script_func', '__webdriver_script_fn', '__fxdriver_evaluate',
                 '__driver_unwrapped', '__webdriver_unwrapped', '__playwright', '__pw_manual'];
    bad.forEach((k) => { try { delete window[k]; } catch (e) {} });
  } catch (e) {}
})();
"""

#: 只在"自己 launch 且没接管指纹浏览器"时注入
_MARK = "_stealth_applied"


def launch_kwargs(headless: bool = True, *, channel: str | None = "chrome") -> dict[str, Any]:
    """给 playwright 的 launch 用。优先系统 Chrome，失败由调用方降级。"""
    kw: dict[str, Any] = {"headless": headless, "args": list(LAUNCH_ARGS)}
    if channel:
        kw["channel"] = channel
    return kw


async def apply(context: Any, ctx: Any = None) -> bool:
    """给 context 注入脚本。接管指纹浏览器时跳过（返回 False）。

    判断依据：ctx 上有 `_ant_owned = True` 表示这只浏览器由指纹浏览器托管。
    """
    if ctx is not None and getattr(ctx, "_ant_owned", False):
        return False
    if getattr(context, _MARK, False):
        return False
    try:
        await context.add_init_script(STEALTH_JS)
        setattr(context, _MARK, True)
        return True
    except Exception:  # noqa: BLE001 - 注入失败不该中断流程
        return False
