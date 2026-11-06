"""跨进程浏览器独占锁。

为什么需要：HTTP 服务和 MCP 服务是两个独立进程，各自通过 CDP 连着同一只 Ant。
两边同时跑流程会抢同一个标签页（互相导航、互相关页），表现为"元素找不到""页面已关闭"
这类随机失败，而且极难定位。

用法：
    async with browser_lock():
        ... 操作浏览器 ...

设计取舍：
- 用 **OS 文件锁**（Windows msvcrt / POSIX fcntl），不用 pid 文件：进程崩溃时内核自动释放，
  不会留下死锁文件把后面全卡住。
- 非阻塞尝试 + 异步轮询：不阻塞事件循环。
- `RPA_BROWSER_LOCK=off` 可关（单进程调试时）。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any


class BrowserBusy(RuntimeError):
    """另一个进程正在使用浏览器，等到超时了。"""


def _lock_path() -> Path:
    from engine.paths import OUT_DIR

    return Path(OUT_DIR) / ".browser.lock"


class _FileLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: Any = None

    def try_acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+", encoding="utf-8")
        try:
            # Windows 的 msvcrt.locking 需要锁区存在，空文件先补一个字节
            fh.seek(0)
            if fh.read(1) == "":
                fh.write("\0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        # 记持有者，便于排查"谁占着浏览器"
        try:
            fh.seek(1)
            fh.truncate()
            fh.write(f" pid={os.getpid()} at={time.strftime('%H:%M:%S')}")
            fh.flush()
        except Exception:  # noqa: BLE001 - 写不进去不影响持锁
            pass
        return True

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001
            pass
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
        self._fh = None

    def holder(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8", errors="ignore").strip("\0 \n")
        except Exception:  # noqa: BLE001
            return ""


class browser_lock:
    """`async with browser_lock(timeout_s=300): ...`

    超时抛 `BrowserBusy`（带持有者信息）—— 让调用方明确知道"另一个进程在跑"，
    而不是随机踩到别人的页面上。
    """

    def __init__(self, timeout_s: float = 300.0, poll_s: float = 0.5) -> None:
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._lock = _FileLock(_lock_path())

    @staticmethod
    def enabled() -> bool:
        return (os.getenv("RPA_BROWSER_LOCK") or "").strip().lower() not in ("off", "0", "false")

    async def __aenter__(self) -> "browser_lock":
        if not self.enabled():
            return self
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while True:
            if self._lock.try_acquire():
                return self
            if loop.time() >= deadline:
                holder = self._lock.holder() or "未知进程"
                raise BrowserBusy(
                    f"另一个进程正在使用浏览器（{holder}），等待 {self.timeout_s:.0f}s 超时。"
                    f" 等它跑完再试；确认无冲突可设 RPA_BROWSER_LOCK=off"
                )
            await asyncio.sleep(self.poll_s)

    async def __aexit__(self, *exc: Any) -> None:
        if self.enabled():
            self._lock.release()
