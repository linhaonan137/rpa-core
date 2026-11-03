"""文件与表格节点：读写/搬运/比对/Excel。

三条约定：
  1. 路径参数统一叫 `path`，目标是 `dest`；相对路径按项目根解析（engine.paths），
     支持 ~ 与环境变量。
  2. 一切 IO 错误都转成 Result.fail，不抛异常。
  3. 读文件默认 utf-8，并自动吃掉 BOM（Windows 上 Excel 导出的 csv 常带 BOM）。
"""

from __future__ import annotations

import difflib
import fnmatch
import functools
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.context import ExecutionContext
from engine.paths import PROJECT_ROOT
from engine.registry import Result, registry

_DEFAULT_CHUNK = 1024 * 1024


# ---------------------------------------------------------------- 基础设施


def node(type_: str, raw: tuple[str, ...] = ()):
    """注册 + 参数解析 + 异常兜底（与 nodes.data 同一套约定）。

    raw 里的字段不做变量替换（写入内容里的 `{...}` 属于正文）。
    """

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(args: dict[str, Any], ctx: ExecutionContext) -> Result:
            if not isinstance(args, dict):
                return Result.fail(f"{type_}: args 必须是对象")
            try:
                a = {k: (v if k in raw else ctx.resolve(v)) for k, v in args.items()}
            except Exception as exc:  # noqa: BLE001
                return Result.fail(f"{type_}: 参数解析失败: {exc}")
            try:
                return await fn(a, ctx)
            except Exception as exc:  # noqa: BLE001
                return Result.fail(f"{type_}: {type(exc).__name__}: {exc}")

        registry.add(type_, wrapper, src=__name__)
        return wrapper

    return deco


def _save(ctx: ExecutionContext, args: dict[str, Any], data: Any) -> None:
    name = args.get("save_as")
    if name:
        ctx.set_var(str(name), data, scope=args.get("save_scope", "local"))


def _ok(ctx: ExecutionContext, args: dict[str, Any], msg: str, data: Any) -> Result:
    _save(ctx, args, data)
    return Result.done(msg, data=data)


def _path(value: Any) -> Path:
    if value is None or str(value).strip() == "":
        raise ValueError("缺 path 参数（copy/move/rename 还要传 dest 或 name）")
    raw = os.path.expandvars(os.path.expanduser(str(value).strip()))
    return Path(raw)


def _abs(p: Path) -> Path:
    """相对路径基于项目根解释，不跟随进程 cwd。"""
    from engine.paths import resolve_path

    return resolve_path(p)


def _need_file(p: Path, who: str) -> Path:
    ap = _abs(p)
    if not ap.exists():
        raise FileNotFoundError(f"{who}: 文件不存在 {ap}")
    if ap.is_dir():
        raise IsADirectoryError(f"{who}: 这是目录不是文件 {ap}")
    return ap


def _mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


def _read_text(p: Path, encoding: str, errors: str = "strict") -> str:
    with p.open("r", encoding=encoding, errors=errors) as f:
        text = f.read()
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _write_text(p: Path, text: str, encoding: str, append: bool, newline: str | None) -> int:
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with p.open(mode, encoding=encoding, newline=newline or "") as f:
        f.write(text)
    return len(text.encode(encoding, errors="replace"))


def _to_content(args: dict[str, Any]) -> str:
    """content 归一化：str 原样；list 一行一个；dict/list + kind=json → JSON 文本。"""
    content = args.get("content")
    if content is None:
        content = args.get("text")
    if content is None:
        raise ValueError("缺 content")
    kind = args.get("kind", "text")
    if kind == "json":
        return json.dumps(content, ensure_ascii=False, indent=int(args.get("indent", 2) or 0))
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        joiner = str(args.get("newline", "\n"))
        return joiner.join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in content)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, indent=int(args.get("indent", 2) or 0))
    return str(content)


# ---------------------------------------------------------------- 读写


@node("fs_read")
async def fs_read(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """读文件。args: {path, encoding=utf-8, kind=text|lines|json, errors?, save_as}"""
    p = _need_file(_path(args.get("path")), "fs_read")
    encoding = str(args.get("encoding", "utf-8"))
    kind = args.get("kind", "text")
    if kind not in ("text", "lines", "json"):
        return Result.fail(f"fs_read: kind 不支持 {kind}")

    text = _read_text(p, encoding, str(args.get("errors", "strict")))
    if kind == "lines":
        data: Any = text.splitlines()
        msg = f"读取 {len(data)} 行"
    elif kind == "json":
        try:
            data = json.loads(text) if text.strip() else None
        except json.JSONDecodeError as exc:
            return Result.fail(f"fs_read: JSON 解析失败 {exc}")
        msg = "读取 JSON 完成"
    else:
        data = text
        msg = f"读取 {len(text)} 字符"
    return _ok(ctx, args, msg, data)


@node("fs_write", raw=("content",))
async def fs_write(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """写文件。args: {path, content, mode=overwrite|append, encoding, kind=text|json, newline?}"""
    p = _abs(_path(args.get("path")))
    mode = args.get("mode", "overwrite")
    if mode not in ("overwrite", "append"):
        return Result.fail(f"fs_write: mode 不支持 {mode}")
    encoding = str(args.get("encoding", "utf-8"))
    text = _to_content(args)
    n = _write_text(p, text, encoding, append=(mode == "append"), newline=args.get("newline"))
    return _ok(ctx, args, f"写入 {n} 字节 → {p}", {"path": str(p), "bytes": n, "mode": mode})


@node("fs_append", raw=("content",))
async def fs_append(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """追加内容（文件不存在就建）。args: {path, content, encoding, kind}"""
    p = _abs(_path(args.get("path")))
    encoding = str(args.get("encoding", "utf-8"))
    text = _to_content(args)
    n = _write_text(p, text, encoding, append=True, newline=args.get("newline"))
    return _ok(ctx, args, f"追加 {n} 字节 → {p}", {"path": str(p), "bytes": n})


@node("fs_copy")
async def fs_copy(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """复制。args: {path, dest, overwrite=false}

    dest 是已存在的目录 → 复制进去并沿用原文件名。
    """
    src = _need_file(_path(args.get("path")), "fs_copy")
    dest = _abs(_path(args.get("dest")))
    target = dest / src.name if dest.is_dir() else dest
    if target.exists() and not args.get("overwrite"):
        return Result.fail(f"fs_copy: 目标已存在 {target}（要覆盖请设 overwrite=true）")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    return _ok(ctx, args, f"复制 → {target}", str(target))


@node("fs_move")
async def fs_move(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """移动/剪切。args: {path, dest, overwrite=false}"""
    src = _abs(_path(args.get("path")))
    if not src.exists():
        return Result.fail(f"fs_move: 源不存在 {src}")
    dest = _abs(_path(args.get("dest")))
    target = dest / src.name if dest.is_dir() else dest
    if target.exists():
        if not args.get("overwrite"):
            return Result.fail(f"fs_move: 目标已存在 {target}（要覆盖请设 overwrite=true）")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    return _ok(ctx, args, f"移动 → {target}", str(target))


@node("fs_delete")
async def fs_delete(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """删除。args: {path, kind=file|dir|any, missing_ok=true}"""
    p = _abs(_path(args.get("path")))
    kind = args.get("kind", "any")
    if kind not in ("file", "dir", "any"):
        return Result.fail(f"fs_delete: kind 不支持 {kind}")
    missing_ok = bool(args.get("missing_ok", True))

    if not p.exists():
        if missing_ok:
            _save(ctx, args, {"path": str(p), "deleted": False})
            return Result.done(f"不存在，跳过 {p}", data={"path": str(p), "deleted": False})
        return Result.fail(f"fs_delete: 不存在 {p}")

    if p.is_dir():
        if kind == "file":
            return Result.fail("fs_delete: 目标是目录，kind=file 不删")
        shutil.rmtree(p)
    else:
        if kind == "dir":
            return Result.fail("fs_delete: 目标是文件，kind=dir 不删")
        p.unlink()
    return _ok(ctx, args, f"已删除 {p}", {"path": str(p), "deleted": True})


@node("fs_rename")
async def fs_rename(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """原地改名。args: {path, name, overwrite=false}——name 只能是文件名，不能带路径"""
    src = _abs(_path(args.get("path")))
    if not src.exists():
        return Result.fail(f"fs_rename: 不存在 {src}")
    name = str(args.get("name") or "").strip()
    if not name:
        return Result.fail("fs_rename: 缺 name")
    if any(sep in name for sep in ("/", "\\")) or name in (".", ".."):
        return Result.fail(f"fs_rename: name 只能是文件名，不能带路径: {name!r}")
    target = src.with_name(name)
    if target == src:
        return _ok(ctx, args, "名字相同，未改动", str(src))
    if target.exists() and not args.get("overwrite"):
        return Result.fail(f"fs_rename: 目标已存在 {target}")
    src.rename(target)
    return _ok(ctx, args, f"改名 → {target.name}", str(target))


@node("fs_has")
async def fs_has(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """存在性判断。args: {path, kind=any|file|dir} → data=bool"""
    p = _abs(_path(args.get("path")))
    kind = args.get("kind", "any")
    if kind == "file":
        ok = p.is_file()
    elif kind == "dir":
        ok = p.is_dir()
    elif kind == "any":
        ok = p.exists()
    else:
        return Result.fail(f"fs_has: kind 不支持 {kind}")
    _save(ctx, args, ok)
    return Result.done(f"{p} {'存在' if ok else '不存在'}", data=ok)


@node("fs_info")
async def fs_info(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """文件元信息。args: {path} → {path,name,stem,suffix,size,size_kb,mtime,ctime,is_file,is_dir}"""
    p = _abs(_path(args.get("path")))
    if not p.exists():
        return Result.fail(f"fs_info: 不存在 {p}")
    st = p.stat()
    data = {
        "path": str(p),
        "name": p.name,
        "stem": p.stem,
        "suffix": p.suffix,
        "size": st.st_size,
        "size_kb": round(st.st_size / 1024, 2),
        "mtime": _mtime(p),
        "ctime": datetime.fromtimestamp(st.st_ctime).isoformat(timespec="seconds"),
        "is_file": p.is_file(),
        "is_dir": p.is_dir(),
    }
    return _ok(ctx, args, f"{p.name} {data['size']} 字节", data)


@node("fs_list")
async def fs_list(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """列目录。args: {path, pattern='*', recursive=false, kind=files|dirs|all,
                      skip_hidden=false, as_paths=false, limit?}

    返回按路径排序的数组，元素是 {name,path,rel,is_dir,size,mtime,suffix}。
    """
    root = _abs(_path(args.get("path")))
    if not root.exists():
        return Result.fail(f"fs_list: 目录不存在 {root}")
    if not root.is_dir():
        return Result.fail(f"fs_list: 不是目录 {root}")
    pattern = str(args.get("pattern", "*") or "*")
    recursive = bool(args.get("recursive", False))
    kind = args.get("kind", "files")
    if kind not in ("files", "dirs", "all"):
        return Result.fail(f"fs_list: kind 不支持 {kind}")
    skip_hidden = bool(args.get("skip_hidden", False))
    limit = int(args.get("limit", 0) or 0)

    it = root.rglob(pattern) if recursive else root.glob(pattern)
    items = []
    for p in sorted(it, key=lambda x: str(x).lower()):
        if p == root:
            continue
        if skip_hidden and any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        is_dir = p.is_dir()
        if kind == "files" and is_dir:
            continue
        if kind == "dirs" and not is_dir:
            continue
        rel = p.relative_to(root).as_posix()
        if not recursive and not fnmatch.fnmatch(p.name, pattern):
            continue
        items.append(
            {
                "name": p.name,
                "path": str(p),
                "rel": rel,
                "is_dir": is_dir,
                "size": 0 if is_dir else p.stat().st_size,
                "mtime": _mtime(p),
                "suffix": p.suffix,
            }
        )
        if limit and len(items) >= limit:
            break

    data: Any = [x["path"] for x in items] if args.get("as_paths") else items
    return _ok(ctx, args, f"列出 {len(items)} 项", data)


@node("fs_mkdir")
async def fs_mkdir(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """建目录（含父级）。args: {path}"""
    p = _abs(_path(args.get("path")))
    p.mkdir(parents=True, exist_ok=True)
    return _ok(ctx, args, f"目录就绪 {p}", str(p))


@node("fs_hash")
async def fs_hash(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """文件摘要。args: {path, algo=sha256, chunk_size?} → {algo,hash,size}"""
    p = _need_file(_path(args.get("path")), "fs_hash")
    algo = str(args.get("algo", "sha256")).lower()
    if algo not in hashlib.algorithms_available:
        return Result.fail(f"fs_hash: 不支持的算法 {algo}")
    try:
        h = hashlib.new(algo)
    except ValueError as exc:
        return Result.fail(f"fs_hash: {exc}")
    chunk = int(args.get("chunk_size", _DEFAULT_CHUNK) or _DEFAULT_CHUNK)
    with p.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    data = {"algo": algo, "hash": h.hexdigest(), "size": p.stat().st_size}
    return _ok(ctx, args, f"{algo}={data['hash'][:12]}…", data)


# ---------------------------------------------------------------- 比对


def _lines(p: Path, encoding: str, args: dict[str, Any]) -> list[str]:
    text = _read_text(p, encoding)
    lines = text.splitlines()
    if args.get("ignore_blank"):
        lines = [x for x in lines if x.strip()]
    if args.get("ignore_case"):
        lines = [x.casefold() for x in lines]
    return lines


@node("fs_diff")
async def fs_diff(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """逐行比对两个文本文件。args: {path, dest, encoding, ignore_case?, ignore_blank?}

    返回 {identical, added, removed, changed, same, stats}
      added   只在新文件里的行   [{line, text}]
      removed 只在旧文件里的行   [{line, text}]
      changed 同一位置上被改写的行 [{line_a, line_b, old, new}]
    块长度不等时，多出来的部分按 added / removed 记，不硬凑成 changed。
    """
    a_path = _need_file(_path(args.get("path")), "fs_diff")
    b_path = _need_file(_path(args.get("dest")), "fs_diff")
    encoding = str(args.get("encoding", "utf-8"))
    a_lines = _lines(a_path, encoding, args)
    b_lines = _lines(b_path, encoding, args)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    same = 0

    sm = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            same += i2 - i1
        elif tag == "delete":
            removed += [{"line": i + 1, "text": a_lines[i]} for i in range(i1, i2)]
        elif tag == "insert":
            added += [{"line": j + 1, "text": b_lines[j]} for j in range(j1, j2)]
        else:  # replace
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                changed.append(
                    {
                        "line_a": i1 + k + 1,
                        "line_b": j1 + k + 1,
                        "old": a_lines[i1 + k],
                        "new": b_lines[j1 + k],
                    }
                )
            removed += [{"line": i + 1, "text": a_lines[i]} for i in range(i1 + n, i2)]
            added += [{"line": j + 1, "text": b_lines[j]} for j in range(j1 + n, j2)]

    identical = not added and not removed and not changed
    data = {
        "identical": identical,
        "added": added,
        "removed": removed,
        "changed": changed,
        "same": same,
        "stats": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "same": same,
        },
    }
    msg = "两个文件一致" if identical else f"+{len(added)} -{len(removed)} ~{len(changed)}"
    return _ok(ctx, args, msg, data)


def _hash_tree(root: Path, pattern: str, algo: str, recursive: bool) -> dict[str, str]:
    it = root.rglob(pattern) if recursive else root.glob(pattern)
    out: dict[str, str] = {}
    for p in it:
        if not p.is_file():
            continue
        h = hashlib.new(algo)
        with p.open("rb") as f:
            for block in iter(lambda: f.read(_DEFAULT_CHUNK), b""):
                h.update(block)
        out[p.relative_to(root).as_posix()] = h.hexdigest()
    return out


@node("fs_dir_diff")
async def fs_dir_diff(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """目录内容比对（按相对路径 + 内容哈希）。args: {path, dest, pattern='*', recursive=true, algo=sha256}

    适合校验"两边备份是否一致"，不受 mtime 干扰。
    """
    a_root = _abs(_path(args.get("path")))
    b_root = _abs(_path(args.get("dest")))
    for r, who in ((a_root, "path"), (b_root, "dest")):
        if not r.is_dir():
            return Result.fail(f"fs_dir_diff: {who} 不是目录 {r}")
    pattern = str(args.get("pattern", "*") or "*")
    recursive = bool(args.get("recursive", True))
    algo = str(args.get("algo", "sha256")).lower()
    if algo not in hashlib.algorithms_available:
        return Result.fail(f"fs_dir_diff: 不支持的算法 {algo}")

    a = _hash_tree(a_root, pattern, algo, recursive)
    b = _hash_tree(b_root, pattern, algo, recursive)
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    changed = [
        {"path": k, "hash_a": a[k][:12], "hash_b": b[k][:12]}
        for k in sorted(set(a) & set(b))
        if a[k] != b[k]
    ]
    same = len(set(a) & set(b)) - len(changed)
    identical = not only_a and not only_b and not changed
    data = {
        "identical": identical,
        "only_in_path": only_a,
        "only_in_dest": only_b,
        "changed": changed,
        "same": same,
        "stats": {
            "files_path": len(a),
            "files_dest": len(b),
            "only_in_path": len(only_a),
            "only_in_dest": len(only_b),
            "changed": len(changed),
            "same": same,
        },
    }
    msg = "两个目录一致" if identical else (
        f"仅左 {len(only_a)} / 仅右 {len(only_b)} / 不同 {len(changed)}"
    )
    return _ok(ctx, args, msg, data)


# ---------------------------------------------------------------- 表格


def _openpyxl(what: str):
    try:
        import openpyxl  # noqa: PLC0415 - 可选依赖，按需导入
    except ImportError:
        raise RuntimeError(f"{what}: 未安装 openpyxl，请先 pip install openpyxl") from None
    return openpyxl


def _sheet_of(wb: Any, name: Any, who: str):
    if not name:
        return wb.active
    if name not in wb.sheetnames:
        raise KeyError(f"{who}: 工作表不存在 {name!r}，现有: {wb.sheetnames}")
    return wb[name]


def _unique_heads(heads: list[Any]) -> list[str]:
    out: list[str] = []
    used: dict[str, int] = {}
    for i, h in enumerate(heads):
        name = ("" if h is None else str(h)).strip() or f"col{i + 1}"
        if name in used:
            used[name] += 1
            name = f"{name}_{used[name]}"
        else:
            used[name] = 1
        out.append(name)
    return out


def _norm_rows(rows: Any, columns: Any, header: Any) -> tuple[list[str] | None, list[list[Any]]]:
    """→ (表头 或 None, 二维数组)。rows 可以是字典数组或二维数组。"""
    if rows is None:
        raise ValueError("缺 rows（或 data）")
    if not isinstance(rows, (list, tuple)):
        raise ValueError(f"rows 必须是数组，得到 {type(rows).__name__}")
    if not rows:
        return ([str(c) for c in columns] if columns else None), []

    if isinstance(rows[0], dict):
        heads: list[str] = [str(c) for c in columns] if columns else []
        if not heads:
            for r in rows:
                for k in r:
                    if str(k) not in heads:
                        heads.append(str(k))
        body = [[r.get(h) for h in heads] for r in rows]
        return (heads if header else None), body

    body = [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows]
    if isinstance(header, (list, tuple)):
        return [str(c) for c in header], body
    if columns:
        return [str(c) for c in columns], body
    return None, body


def _is_blank_sheet(ws: Any) -> bool:
    if ws.max_row > 1 or ws.max_column > 1:
        return False
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=ws.max_column):
        for c in row:
            if c.value not in (None, ""):
                return False
    return True


@node("data_sheet_read")
async def data_sheet_read(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """读表 → 字典数组。args: {path, sheet?, range?, header_row=1, kind=dicts|rows, save_as}

    header_row 从 1 开始；设 0/空 表示没有表头，返回二维数组。
    range 形如 "A1:C10"（给了 range 时以 range 的首行为表头）。
    """
    p = _abs(_path(args.get("path")))
    if not p.exists():
        return Result.fail(f"data_sheet_read: 文件不存在 {p}")
    _openpyxl("data_sheet_read")
    from openpyxl import load_workbook  # noqa: PLC0415

    try:
        wb = load_workbook(p, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 损坏文件/格式不对
        return Result.fail(f"data_sheet_read: 打不开 {p.name}: {type(exc).__name__}: {exc}")

    try:
        ws = _sheet_of(wb, args.get("sheet"), "data_sheet_read")
        rng = args.get("range")
        if rng:
            from openpyxl.utils import range_boundaries  # noqa: PLC0415

            try:
                min_c, min_r, max_c, max_r = range_boundaries(str(rng))
            except ValueError:
                return Result.fail(f"data_sheet_read: range 格式不对 {rng!r}（应如 A1:C10）")
            raw = [
                list(row)
                for row in ws.iter_rows(
                    min_row=min_r, max_row=max_r, min_col=min_c, max_col=max_c, values_only=True
                )
            ]
        else:
            raw = [list(row) for row in ws.iter_rows(values_only=True)]
    except KeyError as exc:
        wb.close()
        return Result.fail(str(exc))
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass

    header_row = args.get("header_row", 1)
    header_row = 0 if header_row in (None, "") else int(header_row)
    raw = [r for r in raw if any(v is not None and str(v).strip() != "" for v in r)]
    if not raw:
        return _ok(ctx, args, "空表", [])

    if header_row <= 0 or args.get("kind") == "rows":
        body = raw if header_row <= 0 else raw[header_row - 1 :]
        return _ok(ctx, args, f"{len(body)} 行", body)

    if header_row > len(raw):
        return Result.fail(f"data_sheet_read: header_row={header_row} 超出表范围（共 {len(raw)} 行）")

    heads = _unique_heads(raw[header_row - 1])
    body = raw[header_row:]
    out = [
        {h: (r[i] if i < len(r) else None) for i, h in enumerate(heads)}
        for r in body
    ]
    return _ok(ctx, args, f"读取 {len(out)} 行 × {len(heads)} 列", out)


@node("data_sheet_write")
async def data_sheet_write(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """写表。args: {path, sheet='Sheet1', rows 或 data, mode=overwrite|append, header=true, columns?}

    overwrite 只清空目标工作表，同文件的其它 sheet 保留。
    """
    p = _abs(_path(args.get("path")))
    sheet = str(args.get("sheet") or "Sheet1")
    mode = args.get("mode", "overwrite")
    if mode not in ("overwrite", "append"):
        return Result.fail(f"data_sheet_write: mode 不支持 {mode}")
    header = args.get("header", True)
    heads, body = _norm_rows(args.get("rows", args.get("data")), args.get("columns"), header)

    openpyxl = _openpyxl("data_sheet_write")
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists() and mode == "overwrite":
        try:
            wb = openpyxl.load_workbook(p)
        except Exception as exc:  # noqa: BLE001
            return Result.fail(f"data_sheet_write: 打不开 {p.name}: {type(exc).__name__}: {exc}")
        if sheet in wb.sheetnames:
            del wb[sheet]
        ws = wb.create_sheet(sheet)
        start = 1
    elif p.exists():
        wb = openpyxl.load_workbook(p)
        ws = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)
        start = 1 if _is_blank_sheet(ws) else ws.max_row + 1
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet
        start = 1

    written = 0
    if heads and start == 1:
        for i, h in enumerate(heads, start=1):
            ws.cell(row=1, column=i, value=h)
        written += 1
        start = 2
    for r in body:
        for i, v in enumerate(r, start=1):
            ws.cell(row=start, column=i, value=v)
        start += 1
        written += 1

    wb.save(p)
    data = {"path": str(p), "sheet": sheet, "rows": len(body), "written": written, "mode": mode}
    return _ok(ctx, args, f"写入 {len(body)} 行 → {p.name}[{sheet}]", data)


@node("data_sheet_append")
async def data_sheet_append(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """追加一行。args: {path, sheet='Sheet1', row, has_header?}

    row 是字典 → 表为空时先写表头（列名取字典键）；row 是数组 → 直接追加。
    """
    p = _abs(_path(args.get("path")))
    sheet = str(args.get("sheet") or "Sheet1")
    row = args.get("row", args.get("row_data"))
    if row is None:
        return Result.fail("data_sheet_append: 缺 row")

    openpyxl = _openpyxl("data_sheet_append")
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        try:
            wb = openpyxl.load_workbook(p)
        except Exception as exc:  # noqa: BLE001
            return Result.fail(f"data_sheet_append: 打不开 {p.name}: {type(exc).__name__}: {exc}")
        ws = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet

    blank = _is_blank_sheet(ws)
    if isinstance(row, dict):
        if blank:
            heads = [str(k) for k in row]
            for i, h in enumerate(heads, start=1):
                ws.cell(row=1, column=i, value=h)
            values = [row[k] for k in row]
            target = 2
        else:
            heads = [
                (ws.cell(row=1, column=c).value) for c in range(1, ws.max_column + 1)
            ]
            mapped = [str(h) if h is not None else "" for h in heads]
            if args.get("has_header", True) and any(mapped):
                values = [row.get(h) for h in mapped]
                target = ws.max_row + 1
            else:
                values = [row[k] for k in row]
                target = (1 if blank else ws.max_row + 1)
    elif isinstance(row, (list, tuple)):
        values = list(row)
        target = 1 if blank else ws.max_row + 1
    else:
        values = [row]
        target = 1 if blank else ws.max_row + 1

    for i, v in enumerate(values, start=1):
        ws.cell(row=target, column=i, value=v)
    wb.save(p)
    data = {"path": str(p), "sheet": sheet, "row": target, "values": len(values)}
    return _ok(ctx, args, f"追加到第 {target} 行", data)
