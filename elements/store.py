"""元素库：六维档案的存储、查询、反向引用、失效检测。

一条元素存六个维度的身份，按稳定性从高到低：
  1 语义  role + name          ★★★★★  改文案才失效
  2 属性  attrs（id/testid/name/type）  ★★★★
  3 辅助  placeholder/aria-label/title  ★★★
  4 结构  path（父链）+ frame            ★★
  5 邻居  siblings（邻近文本）           ★★
  6 几何  geometry（文档坐标）            ★

存储是单个 JSON（`elements.json`），跨流程共享；流程导出时另带 manifest 快照。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from engine.paths import ELEMENTS_PATH

#: 反向索引用：某个元素被哪些流程/节点引用
RefIndex = dict[str, set[str]]


class ElementError(Exception):
    pass


@contextmanager
def _file_lock(path: str, timeout: float = 5.0):
    """跨进程排他锁（Windows 用 msvcrt，POSIX 用 flock）。

    为什么需要：`save()` 是"读磁盘 → 合并 → 写回"，两个进程同时进来就会
    各自读到旧快照再互覆盖。加了锁，这段才是真正的 read-modify-write。
    锁文件单独放（不是锁 elements.json 本身）——文件会被 os.replace 换掉，
    锁在它上面会跟着失效。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    f = open(path, "a+b")
    try:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:      # 空文件锁 1 字节会失败，先垫一个
            f.write(b"\0")
            f.flush()
        deadline = time.time() + timeout
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise ElementError(f"元素库被占用，{timeout}s 内拿不到锁: {path}") from None
                time.sleep(0.02)
        yield
    finally:
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


@dataclass
class Geometry:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    scroll_x: int = 0
    scroll_y: int = 0
    viewport_w: int = 0
    viewport_h: int = 0


@dataclass
class Fingerprint:
    """结构指纹：六维身份，用于打分器做相似度比对。"""

    role: str | None = None  # 第 1 层（语义）
    name: str | None = None  # 第 1 层
    tag: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    path: list[str] = field(default_factory=list)
    siblings: list[str] = field(default_factory=list)
    geometry: Geometry = field(default_factory=Geometry)
    frame: list[str] = field(default_factory=list)  # iframe 链


@dataclass
class ElementEntry:
    key: str
    role: str | None = None
    name: str | None = None
    css: str | None = None  # 显式 css 兜底
    hints: dict[str, Any] = field(default_factory=dict)
    fingerprint: Fingerprint = field(default_factory=Fingerprint)
    meta: dict[str, Any] = field(default_factory=dict)  # created_at / source / hits / misses

    def __post_init__(self) -> None:
        """role/name 是第 1 层，同步进指纹，避免两处不一致。"""
        if self.role and not self.fingerprint.role:
            self.fingerprint.role = self.role
        if self.name and not self.fingerprint.name:
            self.fingerprint.name = self.name
        if self.fingerprint.role and not self.role:
            self.role = self.fingerprint.role
        if self.fingerprint.name and not self.name:
            self.name = self.fingerprint.name

    # ---------------------------------------------------------- 校验

    def validate(self) -> None:
        if not self.key:
            raise ElementError("元素 key 不能为空")
        if not self.role and not self.name:
            raise ElementError(f"{self.key}: role 和 name 至少一个非空")
        if not self.fingerprint.path and not self.css:
            raise ElementError(f"{self.key}: path 与 css 至少给一个")

    # ---------------------------------------------------------- 序列化

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # 去掉空值，减小文件；但 key 必留
        return {k: v for k, v in d.items() if v not in (None, "", {}, [])} | {"key": self.key}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ElementEntry":
        fp_raw = d.get("fingerprint") or {}
        geo_raw = fp_raw.get("geometry") or {}
        fp = Fingerprint(
            role=fp_raw.get("role") or d.get("role"),
            name=fp_raw.get("name") or d.get("name"),
            tag=fp_raw.get("tag", ""),
            attrs=dict(fp_raw.get("attrs") or {}),
            path=list(fp_raw.get("path") or []),
            siblings=list(fp_raw.get("siblings") or []),
            frame=list(fp_raw.get("frame") or []),
            geometry=Geometry(**{k: int(v) for k, v in geo_raw.items() if k in Geometry.__dataclass_fields__}),
        )
        return cls(
            key=d["key"],
            role=d.get("role"),
            name=d.get("name"),
            css=d.get("css"),
            hints=dict(d.get("hints") or {}),
            fingerprint=fp,
            meta=dict(d.get("meta") or {}),
        )

    # ---------------------------------------------------------- 便捷

    @property
    def hits(self) -> int:
        return int(self.meta.get("hits", 0))

    @property
    def misses(self) -> int:
        return int(self.meta.get("misses", 0))

    def hit(self) -> None:
        self.meta["hits"] = self.hits + 1

    def miss(self) -> None:
        self.meta["misses"] = self.misses + 1

    def is_stale(self, threshold: int = 3) -> bool:
        """连续失效超过阈值 → 建议重新拾取。"""
        return self.misses >= threshold and self.misses > self.hits


class ElementStore:
    """元素库：内存 + JSON 落盘 + 反向引用索引。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = os.fspath(path) if path is not None else None
        self._items: dict[str, ElementEntry] = {}
        self._refs: RefIndex = {}
        #: 载入时磁盘上已有的 key。save() 用它算"本实例删了谁"——
        #  合并写只会新增/更新，不靠这个的话删除永远落不了盘（实测 element_drop
        #  报成功但条目还在，库只增不减）。
        #: 墓碑：key → 删除时间。合并写里"删除"必须显式记录，
        #  否则任何持有旧副本的实例写回统计时都会把删掉的条目复活（实测）。
        self._deleted: dict[str, str] = {}
        #: 本实例重新拾取过的 key。墓碑按秒记，同一秒内"删了又加"分不出先后，
        #  所以复活必须是显式动作，不能靠时间戳比较。
        self._revived: set[str] = set()
        self._loaded_keys: set[str] = set()
        if path and os.path.exists(path):
            self.load()

    # ---------------------------------------------------------- 存取

    def load(self) -> None:
        if not self.path:
            return
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        data = raw.get("elements", raw) if isinstance(raw, dict) else {}
        self._items = {k: ElementEntry.from_dict({**v, "key": k}) for k, v in data.items()}
        tomb = raw.get("deleted") if isinstance(raw, dict) else None
        self._deleted = dict(tomb) if isinstance(tomb, dict) else {}
        # 墓碑优先：磁盘上若还残留已删条目，读的时候就不该再有
        for key in [k for k in self._items if k in self._deleted]:
            self._items.pop(key, None)
        self._loaded_keys = set(self._items)

    def save(self) -> None:
        """落盘（持锁 read-modify-write）。

        两个进程（HTTP 服务 / MCP 服务）共享同一个 elements.json。原来直接把
        构造时的 `_items` 全量重写，另一个进程在这期间新增的条目会被旧快照覆盖
        —— 实测 `web_pick` 报成功但库里没有，就是这个。

        现在：进锁 → 重读磁盘 → **先应用本实例的删除** → 再按 key 合并新增/更新
        → 原子换入。删除必须显式做，因为"合并"天然表达不了"消失"。

        已知边界：如果 A 进程删了某条、B 进程几乎同时更新同一条的统计，
        B 的写回会把它复活。概率很低（要恰好同一条），不为此引入墓碑表。
        """
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

        with _file_lock(self.path + ".lock"):
            merged: dict[str, Any] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as f:
                        raw = json.load(f)
                    data = raw.get("elements", raw) if isinstance(raw, dict) else {}
                    merged = {k: ElementEntry.from_dict({**v, "key": k}) for k, v in data.items()}
                except Exception:  # noqa: BLE001 - 磁盘文件坏了就用内存的覆盖回去
                    merged = {}

            # ① 本实例载入过、现在内存里没有的 → 是删掉的，记墓碑
            for key in list(self._loaded_keys):
                if key not in self._items and key not in self._deleted:
                    self._deleted[key] = _now()

            # ② 合并墓碑（同 key 取早的：删除一旦发生就不该被后来的"复活"覆盖）
            tomb: dict[str, str] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as f:
                        on_disk = json.load(f)
                    if isinstance(on_disk, dict) and isinstance(on_disk.get("deleted"), dict):
                        tomb.update({k: str(v) for k, v in on_disk["deleted"].items()})
                except Exception:  # noqa: BLE001
                    pass
            for key, ts in self._deleted.items():
                tomb[key] = min(tomb.get(key, ts), ts)
            for key in self._revived:      # 本实例重新拾取过的，墓碑作废
                tomb.pop(key, None)
            # 磁盘上已经有、但墓碑比它更新时间还新的 → 不保留（挡住"旧副本复活")
            for key in list(merged):
                if key in tomb and tomb[key] >= _mtime(merged[key]):
                    merged.pop(key, None)

            # ③ 合并本实例的新增 / 更新（墓碑更新的条目一律不写回）
            for key, entry in self._items.items():
                if key in tomb and tomb[key] >= _mtime(entry):
                    continue
                old = merged.get(key)
                if old is None or _mtime(entry) >= _mtime(old):
                    merged[key] = entry

            self._deleted = tomb
            payload = {"version": 2, "elements": {k: v.to_dict() for k, v in merged.items()},
                       "deleted": tomb}
            tmp = f"{self.path}.{os.getpid()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)  # 原子写：不会留半截文件
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        self._items = merged
        self._loaded_keys = set(merged)
        self._revived.clear()

    # ---------------------------------------------------------- CRUD

    def add(self, entry: ElementEntry, *, overwrite: bool = False) -> None:
        entry.validate()
        if entry.key in self._items and not overwrite:
            raise ElementError(f"元素已存在: {entry.key}（要覆盖请传 overwrite=True）")
        entry.meta.setdefault("created_at", _now())
        entry.meta.setdefault("source", "manual")
        self._deleted.pop(entry.key, None)   # 重新拾取同名元素 → 墓碑作废
        self._revived.add(entry.key)
        # 初始就写 0：不写的话 JSON 里没有这两个键，读出来是 None（看着像"没统计"）
        entry.meta.setdefault("hits", 0)
        entry.meta.setdefault("misses", 0)
        self._items[entry.key] = entry

    def get(self, key: str) -> ElementEntry:
        try:
            return self._items[key]
        except KeyError:
            raise ElementError(f"元素不存在: {key}") from None

    def has(self, key: str) -> bool:
        return key in self._items

    def remove(self, key: str, *, force: bool = False) -> None:
        if key not in self._items:
            raise ElementError(f"元素不存在: {key}")
        users = self._refs.get(key, set())
        if users and not force:
            raise ElementError(f"{key} 仍被引用，不能删: {sorted(users)}（要强删请传 force=True）")
        del self._items[key]
        self._refs.pop(key, None)
        # 记墓碑：让别的实例手里的旧副本写回时不会把它复活
        self._deleted[key] = _now()

    def keys(self) -> list[str]:
        return sorted(self._items)

    def all(self) -> list[ElementEntry]:
        return [self._items[k] for k in self.keys()]

    def stale(self, threshold: int = 3) -> list[str]:
        return [k for k, e in self._items.items() if e.is_stale(threshold)]

    def __len__(self) -> int:
        return len(self._items)

    # ---------------------------------------------------------- 反向引用

    def set_refs(self, refs: RefIndex) -> None:
        """由流程扫描得出：元素 → 引用它的节点标识集合。"""
        self._refs = {k: set(v) for k, v in refs.items()}

    def refs_of(self, key: str) -> set[str]:
        return set(self._refs.get(key, set()))

    def unused(self) -> list[str]:
        """没有任何流程引用的元素（可清理候选）。"""
        return [k for k in self._items if not self._refs.get(k)]

    def describe(self) -> dict[str, Any]:
        return {
            "total": len(self._items),
            "stale": self.stale(),
            "unused": self.unused(),
            "referenced": {k: sorted(v) for k, v in self._refs.items() if v},
        }


# ---------------------------------------------------------------- 扫描流程引用


def scan_refs(flows: Iterable[dict[str, Any]], prefix: str = "@el:") -> RefIndex:
    """从若干流程 JSON 里扫出 `@el:xxx` 引用，返回 {元素key: {"flow:node", ...}}。"""
    refs: RefIndex = {}
    for flow in flows:
        fname = flow.get("name") or flow.get("id") or "?"
        for node in flow.get("nodes") or []:
            nid = node.get("id", "?")
            for value in _walk(node.get("args") or {}):
                if isinstance(value, str) and value.startswith(prefix):
                    key = value[len(prefix):].strip()
                    refs.setdefault(key, set()).add(f"{fname}:{nid}")
    return refs


def _walk(obj: Any) -> Iterable[Any]:
    """递归遍历 dict/list 里的所有叶子值。"""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def _mtime(entry: Any) -> str:
    """条目"最后修改时间"，合并写时用来判断取哪份。"""
    meta = getattr(entry, "meta", None) or {}
    try:
        return str(meta.get("updated_at") or meta.get("created_at") or "")
    except Exception:  # noqa: BLE001
        return ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- 便捷入口


def load_store(path: str | os.PathLike[str] | None = None) -> ElementStore:
    """不带参数时读项目默认元素库（``engine.paths.ELEMENTS_PATH``）。"""
    return ElementStore(path if path is not None else ELEMENTS_PATH)


def make_entry(key: str, role: str | None, name: str | None, **kw: Any) -> ElementEntry:
    """快速造一条档案（测试/拾取器用）。"""
    fp = kw.pop("fingerprint", None) or Fingerprint(**kw.pop("fp", {}))
    return ElementEntry(key=key, role=role, name=name, fingerprint=fp, **kw)
