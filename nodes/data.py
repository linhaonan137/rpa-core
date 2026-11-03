"""数据加工节点：列表、字典、字符串/CSV、统计。

设计取舍：
  1. **不为一行代码立节点**（list_length / str_concat 之类），
     合并成带 `kind` / `op` 参数的少数节点，减少选节点的认知负担。
  2. 每个节点的 `save_as` 会把结果写进上下文变量，同时也在 Result.data 返回。
  3. 参数先经 `ctx.resolve`，失败一律 Result.fail，不抛异常穿透。
  4. 列表节点统一接受 list/tuple/set；字符串视为错误（防止把 "abc" 当列表迭代出 3 个字符）。
"""

from __future__ import annotations

import csv
import functools
import io
import itertools
import json
import math
import re
import statistics
from typing import Any, Iterable

from engine.context import ExecutionContext
from engine.registry import Result, registry

# ---------------------------------------------------------------- 基础设施

_MISSING = object()

#: 所有列表节点默认的上限，防止笛卡尔积之类把内存打爆
_MAX_OUT = 100_000


def node(type_: str, raw: tuple[str, ...] = ()):
    """注册 + 统一的参数解析与兜底。

    包一层的目的：节点内部可以放心写 `int(args["size"])`，
    任何异常都会变成 Result.fail，符合「不许抛异常穿透」的硬规则。

    raw 里的字段**不做**变量替换 —— 模板、正则、正文这类内容里的
    `{...}` 是数据本身（如 `{姓名}`、`\\d{3}`），替换掉就毁了。
    """

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(args: dict[str, Any], ctx: ExecutionContext) -> Result:
            if not isinstance(args, dict):
                return Result.fail(f"{type_}: args 必须是对象")
            try:
                a = {k: (v if k in raw else ctx.resolve(v)) for k, v in args.items()}
            except Exception as exc:  # noqa: BLE001 - 变量缺失也算参数错误
                return Result.fail(f"{type_}: 参数解析失败: {exc}")
            try:
                return await fn(a, ctx)
            except Exception as exc:  # noqa: BLE001
                return Result.fail(f"{type_}: {type(exc).__name__}: {exc}")

        registry.add(type_, wrapper, src=__name__, raw=raw)
        return wrapper

    return deco


def _save(ctx: ExecutionContext, args: dict[str, Any], data: Any) -> None:
    name = args.get("save_as")
    if name:
        ctx.set_var(str(name), data, scope=args.get("save_scope", "local"))


def _ok(ctx: ExecutionContext, args: dict[str, Any], msg: str, data: Any) -> Result:
    _save(ctx, args, data)
    return Result.done(msg, data=data)


def _as_list(value: Any, who: str) -> list[Any]:
    if value is None:
        raise ValueError(f"{who}: 缺 items")
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set, frozenset)):
        return list(value)
    if isinstance(value, str):
        raise ValueError(f"{who}: items 不能是字符串，请传列表")
    if isinstance(value, dict):
        raise ValueError(f"{who}: items 不能是字典，请用 dict_* 节点")
    if isinstance(value, Iterable):
        return list(value)
    raise ValueError(f"{who}: items 类型不支持 {type(value).__name__}")


def _tokens(path: Any) -> list[str]:
    """把 `a.b[0].c` / `a.b.0.c` / `[0].a` 拆成 ['a','b','0','c']。"""
    if path is None or path == "":
        return []
    p = re.sub(r"\[\s*['\"]?([^\[\]'\"]*)['\"]?\s*\]", r".\1", str(path))
    return [t for t in p.split(".") if t != ""]


def _step(cur: Any, tok: str) -> Any:
    if isinstance(cur, dict):
        if tok in cur:
            return cur[tok]
        try:
            num = int(tok)
        except ValueError:
            return _MISSING
        if num in cur:
            return cur[num]
        return _MISSING
    if isinstance(cur, (list, tuple)):
        try:
            return cur[int(tok)]
        except (ValueError, IndexError):
            return _MISSING
    return _MISSING


def get_path(obj: Any, path: Any, default: Any = _MISSING) -> Any:
    """安全取嵌套值。空路径返回原对象。"""
    cur = obj
    for tok in _tokens(path):
        cur = _step(cur, tok)
        if cur is _MISSING:
            return default
    return cur


def _hkey(v: Any) -> Any:
    """可哈希化，用于去重/集合运算；dict 用稳定 JSON 串。"""
    try:
        hash(v)
        return v
    except TypeError:
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


def _to_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def _num(v: Any) -> float | None:
    """能当数字用就返回 float，否则 None。bool 不算数字。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("%", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _numbers(items: list[Any], key: Any = None) -> tuple[list[float], int]:
    nums: list[float] = []
    bad = 0
    for it in items:
        v = it if key in (None, "") else get_path(it, key)
        if v is _MISSING:
            bad += 1
            continue
        f = _num(v)
        if f is None:
            bad += 1
        else:
            nums.append(f)
    return nums, bad


def _cmp(a: Any, op: str, b: Any, ci: bool = False) -> bool:
    """通用比较。字符串类操作可选忽略大小写。"""
    def _s(x: Any) -> str:
        return _to_text(x).casefold() if ci else _to_text(x)

    if op in ("truthy", "is_true"):
        return bool(a)
    if op in ("falsy", "is_false"):
        return not bool(a)
    if op == "empty":
        return a is None or a == "" or a == [] or a == {}
    if op == "not_empty":
        return not _cmp(a, "empty", b)
    if op == "exists":
        return a is not _MISSING and a is not None
    if op == "eq":
        return _s(a) == _s(b) if isinstance(a, str) or isinstance(b, str) else a == b
    if op == "ne":
        return not _cmp(a, "eq", b, ci)
    if op in ("gt", "ge", "lt", "le"):
        fa, fb = _num(a), _num(b)
        if fb is not None:
            # 门槛是数字时，非数字的值一律判 False（否则 "x" > "18" 会意外成立）
            if fa is None:
                return False
            x, y = fa, fb
        else:
            x, y = _s(a), _s(b)
        return {"gt": x > y, "ge": x >= y, "lt": x < y, "le": x <= y}[op]
    if op == "contains":
        if isinstance(a, str):
            return _s(b) in _s(a)
        if isinstance(a, (list, tuple, set)):
            return any(_hkey(x) == _hkey(b) for x in a)
        return False
    if op == "not_contains":
        return not _cmp(a, "contains", b, ci)
    if op == "in":
        return _cmp(b, "contains", a, ci)
    if op == "not_in":
        return not _cmp(b, "contains", a, ci)
    if op == "startswith":
        return _s(a).startswith(_s(b))
    if op == "endswith":
        return _s(a).endswith(_s(b))
    if op in ("regex", "match"):
        return re.search(str(b), _to_text(a), re.IGNORECASE if ci else 0) is not None
    raise ValueError(f"不支持的 op: {op}")


def _render(tpl: str, data: Any, missing: str = "keep") -> str:
    """`{a.b}` / `{a[0]}` 占位符渲染，`{{` `}}` 转义。"""
    out: list[str] = []
    i = 0
    n = len(tpl)
    while i < n:
        ch = tpl[i]
        if ch in "{}" and i + 1 < n and tpl[i + 1] == ch:
            out.append(ch)
            i += 2
            continue
        if ch == "{":
            j = tpl.find("}", i + 1)
            if j == -1:
                out.append(tpl[i:])
                break
            expr = tpl[i + 1 : j].strip()
            val = get_path(data, expr) if data is not None else _MISSING
            if val is _MISSING:
                if missing == "blank":
                    pass
                elif missing == "error":
                    raise KeyError(f"模板变量不存在: {expr}")
                else:
                    out.append(tpl[i : j + 1])
            else:
                out.append(_to_text(val))
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- 列表


@node("seq_order")
async def seq_order(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """排序。args: {items, key?, desc?, kind=auto, ignore_case?}

    kind: auto（数字在前、字符串在后、None 最后）/ text（全按字符串）/ number（强制数值）
    key: 元素是 dict 时取哪个字段参与比较，支持 a.b[0]
    """
    items = _as_list(args.get("items"), "seq_order")
    key = args.get("key")
    desc = bool(args.get("desc", False))
    ci = bool(args.get("ignore_case", False))
    kind = args.get("kind", "auto")
    if kind not in ("auto", "text", "number"):
        return Result.fail(f"seq_order: kind 不支持 {kind}")

    def sk(it: Any) -> tuple:
        v = it if key in (None, "") else get_path(it, key)
        if v is _MISSING or v is None:
            return (2, 0.0, "")
        if kind == "text":
            s = _to_text(v)
            return (0, 0.0, s.casefold() if ci else s)
        if kind == "number":
            f = _num(v)
            return (0, f, "") if f is not None else (2, 0.0, "")
        if isinstance(v, bool):
            return (0, float(v), "")
        if isinstance(v, (int, float)):
            return (0, float(v), "")
        f = _num(v)
        if f is not None:
            return (0, f, "")
        s = _to_text(v)
        return (1, 0.0, s.casefold() if ci else s)

    out = sorted(items, key=sk, reverse=desc)
    return _ok(ctx, args, f"排序 {len(out)} 项", out)


@node("seq_uniq")
async def seq_uniq(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """去重保序。args: {items, key?, kind=first|last}"""
    items = _as_list(args.get("items"), "seq_uniq")
    key = args.get("key")
    kind = args.get("kind", "first")
    if kind not in ("first", "last"):
        return Result.fail(f"seq_uniq: kind 不支持 {kind}")

    seen: dict[Any, int] = {}
    for idx, it in enumerate(items):
        k = _hkey(it if key in (None, "") else get_path(it, key, None))
        if kind == "last":
            seen[k] = idx
        elif k not in seen:
            seen[k] = idx
    order = sorted(seen.values())
    out = [items[i] for i in order]
    return _ok(ctx, args, f"去重 {len(items)} → {len(out)}", out)


@node("seq_flat")
async def seq_flat(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """拍平嵌套列表。args: {items, depth=1}（depth<=0 或 None = 无限递归）"""
    items = _as_list(args.get("items"), "seq_flat")
    depth = args.get("depth", 1)
    depth = 0 if depth is None else int(depth)

    out: list[Any] = []

    def walk(seq: Iterable[Any], d: int) -> None:
        for x in seq:
            if isinstance(x, (list, tuple)) and d != 0:
                walk(x, d - 1 if d > 0 else d)
            else:
                out.append(x)

    walk(items, depth)
    return _ok(ctx, args, f"拍平 {len(items)} → {len(out)}", out)


@node("seq_split")
async def seq_split(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """按 size 分块，最后一块可短。args: {items, size}"""
    items = _as_list(args.get("items"), "seq_split")
    try:
        size = int(args.get("size") or 0)
    except (TypeError, ValueError):
        return Result.fail("seq_split: size 必须是整数")
    if size < 1:
        return Result.fail("seq_split: size 必须 >= 1")
    out = [items[i : i + size] for i in range(0, len(items), size)]
    return _ok(ctx, args, f"{len(items)} 项分 {len(out)} 块", out)


@node("seq_pick")
async def seq_pick(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """按条件过滤。args: {items, key?, op=truthy, value?, ignore_case?}

    op: truthy falsy empty not_empty eq ne gt ge lt le contains not_contains
        in not_in startswith endswith regex
    """
    items = _as_list(args.get("items"), "seq_pick")
    key = args.get("key")
    op = str(args.get("op", "truthy"))
    value = args.get("value")
    ci = bool(args.get("ignore_case", False))
    out = []
    for it in items:
        v = it if key in (None, "") else get_path(it, key)
        if _cmp(v if v is not _MISSING else None, op, value, ci):
            out.append(it)
    return _ok(ctx, args, f"过滤 {len(items)} → {len(out)}", out)


@node("seq_map", raw=("template",))
async def seq_map(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """逐项转换。args: {items, key?, keys?, rename?, template?}

    四选一（优先级从高到低）：
      template  用 {字段} 渲染每个元素（元素是 dict 时可直接用字段名）
      keys      只保留这些字段（投影）
      rename    {旧名: 新名} 改名
      key       取出该字段，得到值列表
    """
    items = _as_list(args.get("items"), "seq_map")
    key = args.get("key")
    keys = args.get("keys")
    rename = args.get("rename")
    template = args.get("template")

    if template is not None:
        out = [_render(str(template), it, args.get("missing", "keep")) for it in items]
    elif keys:
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        out = [{k: get_path(it, k, None) for k in keys} for it in items]
    elif rename and isinstance(rename, dict):
        out = []
        for it in items:
            if not isinstance(it, dict):
                return Result.fail("seq_map: 用 rename 时元素必须是字典")
            out.append({rename.get(k, k): v for k, v in it.items()})
    elif key not in (None, ""):
        out = [get_path(it, key, None) for it in items]
    else:
        out = list(items)
    return _ok(ctx, args, f"映射 {len(out)} 项", out)


@node("seq_sum")
async def seq_sum(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """求和（忽略非数字）。args: {items, key?, precision?}"""
    items = _as_list(args.get("items"), "seq_sum")
    nums, bad = _numbers(items, args.get("key"))
    total: float | int = sum(nums)
    if isinstance(total, float) and total.is_integer():
        total = int(total)
    total = _round(total, args.get("precision"))
    return _ok(ctx, args, f"求和 {total}（跳过 {bad} 个非数字）", total)


@node("seq_avg")
async def seq_avg(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """平均值（忽略非数字）。args: {items, key?, precision?}"""
    items = _as_list(args.get("items"), "seq_avg")
    nums, bad = _numbers(items, args.get("key"))
    if not nums:
        return Result.fail("seq_avg: 没有可计算的数字")
    avg = _round(sum(nums) / len(nums), args.get("precision", 6))
    return _ok(ctx, args, f"均值 {avg}（跳过 {bad} 个非数字）", avg)


@node("seq_minmax")
async def seq_minmax(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """最小/最大。args: {items, key?} → {min, max, imin, imax}"""
    items = _as_list(args.get("items"), "seq_minmax")
    if not items:
        return Result.fail("seq_minmax: 列表为空")
    key = args.get("key")
    vals = (
        [(i, get_path(it, key, None)) for i, it in enumerate(items)]
        if key not in (None, "")
        else list(enumerate(items))
    )
    nums = [(i, _num(v)) for i, v in vals]
    if all(f is not None for _, f in nums):
        imin = min(nums, key=lambda x: x[1])[0]
        imax = max(nums, key=lambda x: x[1])[0]
    else:
        texts = [(i, _to_text(v)) for i, v in vals]
        imin = min(texts, key=lambda x: x[1])[0]
        imax = max(texts, key=lambda x: x[1])[0]
    data = {"min": items[imin], "max": items[imax], "imin": imin, "imax": imax}
    return _ok(ctx, args, f"min={data['min']!r} max={data['max']!r}", data)


@node("seq_slice")
async def seq_slice(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """切片（Python 语义，支持负数）。args: {items, start=0, end?, step=1}"""
    items = _as_list(args.get("items"), "seq_slice")
    try:
        start_raw = args.get("start", 0)
        start = None if start_raw in (None, "") else int(start_raw)
        end_raw = args.get("end")
        end = None if end_raw in (None, "") else int(end_raw)
        step_raw = args.get("step", 1)
        step = 1 if step_raw in (None, "") else int(step_raw)
    except (TypeError, ValueError):
        return Result.fail("seq_slice: start/end/step 必须是整数")
    if step == 0:
        return Result.fail("seq_slice: step 不能为 0")
    out = items[start:end:step]
    return _ok(ctx, args, f"切片得 {len(out)} 项", out)


def _multi_lists(args: dict[str, Any], who: str) -> list[list[Any]]:
    lists = args.get("lists")
    if lists is None:
        a, b = args.get("a"), args.get("b")
        if a is None and b is None:
            raise ValueError(f"{who}: 需要 lists 或 a/b")
        lists = [a, b]
    if isinstance(lists, dict):
        raise ValueError(f"{who}: lists 里应是多个列表")
    if not isinstance(lists, (list, tuple)) or len(lists) == 0:
        raise ValueError(f"{who}: lists 必须是非空数组")
    return [_as_list(x, who) for x in lists]


@node("seq_union")
async def seq_union(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """并集保序。args: {lists: [[..],[..]]} 或 {a, b}；key? 用于字典去重"""
    groups = _multi_lists(args, "seq_union")
    key = args.get("key")
    seen: set[Any] = set()
    out: list[Any] = []
    for g in groups:
        for it in g:
            k = _hkey(it if key in (None, "") else get_path(it, key, None))
            if k not in seen:
                seen.add(k)
                out.append(it)
    return _ok(ctx, args, f"并集 {len(out)} 项", out)


@node("seq_inter")
async def seq_inter(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """交集保序（以第一个列表为基准）。args: {lists} 或 {a, b}；key?"""
    groups = _multi_lists(args, "seq_inter")
    key = args.get("key")

    def ks(g: list[Any]) -> set[Any]:
        return {_hkey(it if key in (None, "") else get_path(it, key)) for it in g}

    if len(groups) == 1:
        common = ks(groups[0])
    else:
        common = set.intersection(*(ks(g) for g in groups))
    out: list[Any] = []
    seen: set[Any] = set()
    for it in groups[0]:
        k = _hkey(it if key in (None, "") else get_path(it, key, None))
        if k in common and k not in seen:
            seen.add(k)
            out.append(it)
    return _ok(ctx, args, f"交集 {len(out)} 项", out)


@node("seq_diff")
async def seq_diff(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """差集：在第一个列表、但不在其余任一列表。args: {lists} 或 {a, b}；key?"""
    groups = _multi_lists(args, "seq_diff")
    key = args.get("key")

    def kof(it: Any) -> Any:
        return _hkey(it if key in (None, "") else get_path(it, key, None))

    rest: set[Any] = set()
    for g in groups[1:]:
        rest |= {kof(it) for it in g}
    out: list[Any] = []
    seen: set[Any] = set()
    for it in groups[0]:
        k = kof(it)
        if k not in rest and k not in seen:
            seen.add(k)
            out.append(it)
    return _ok(ctx, args, f"差集 {len(out)} 项", out)


@node("seq_product")
async def seq_product(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """笛卡尔积。args: {lists, limit=100000, as_dict?, keys?}

    as_dict + keys=["尺寸","颜色"] → [{"尺寸":..,"颜色":..}, ...]
    """
    groups = _multi_lists(args, "seq_product")
    try:
        limit = int(args.get("limit", _MAX_OUT) or _MAX_OUT)
    except (TypeError, ValueError):
        return Result.fail("seq_product: limit 必须是整数")
    total = 1
    for g in groups:
        total *= len(g)
    if total > limit:
        return Result.fail(f"seq_product: 结果 {total} 超过上限 {limit}，请调小输入或加大 limit")

    combos = [list(c) for c in itertools.product(*groups)]
    as_dict = bool(args.get("as_dict", False))
    keys = args.get("keys")
    if as_dict:
        if not keys or len(keys) != len(groups):
            return Result.fail("seq_product: as_dict 时 keys 数量必须与 lists 一致")
        combos = [dict(zip(keys, c)) for c in combos]
    return _ok(ctx, args, f"笛卡尔积 {total} 组", combos)


# ---------------------------------------------------------------- 字典


@node("map_path")
async def map_path(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """安全取嵌套值。args: {data, path, default?, required=false}

    path 支持 data.a / a.b[0] / a.b.0.c；缺失返回 default（默认 null）而不是报错。
    想"缺失就失败"就设 required=true。
    """
    data = args.get("data")
    path = args.get("path", "")
    default = args.get("default")
    val = get_path(data, path)
    if val is _MISSING:
        if args.get("required"):
            return Result.fail(f"map_path: 路径不存在 {path!r}")
        _save(ctx, args, default)
        return Result.done(f"路径 {path!r} 不存在，用默认值", data=default)
    _save(ctx, args, val)
    return Result.done(f"取到 {path}", data=val)


@node("map_flat")
async def map_flat(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """扁平化字典。args: {data, sep='.', max_depth?=0, flatten_list=true}

    {"a":{"b":1},"c":[7,8]} → {"a.b":1,"c.0":7,"c.1":8}
    """
    data = args.get("data")
    if not isinstance(data, dict):
        return Result.fail("map_flat: data 必须是字典")
    sep = str(args.get("sep", "."))
    max_depth = int(args.get("max_depth", 0) or 0)
    flatten_list = bool(args.get("flatten_list", True))

    out: dict[str, Any] = {}

    def walk(val: Any, prefix: str, depth: int) -> None:
        nested = isinstance(val, dict) or (flatten_list and isinstance(val, (list, tuple)))
        if nested and (max_depth <= 0 or depth < max_depth):
            seq = val.items() if isinstance(val, dict) else enumerate(val)
            for k, v in seq:
                walk(v, f"{prefix}{sep}{k}" if prefix else str(k), depth + 1)
        else:
            out[prefix] = val

    if not data:
        return _ok(ctx, args, "空字典", {})
    for k, v in data.items():
        walk(v, str(k), 0)
    return _ok(ctx, args, f"扁平化 {len(out)} 个键", out)


@node("map_merge")
async def map_merge(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """合并多个字典。args: {dicts: [{..},{..}], op=deep|shallow, overwrite=true, concat_lists=false}"""
    ds = args.get("dicts")
    if ds is None:
        ds = [d for d in (args.get("a"), args.get("b")) if d is not None]
    if not isinstance(ds, (list, tuple)) or not ds:
        return Result.fail("map_merge: 需要 dicts 数组（或 a/b）")
    for d in ds:
        if not isinstance(d, dict):
            return Result.fail(f"map_merge: 第 {ds.index(d) + 1} 项不是字典")

    op = args.get("op", "deep")
    if op not in ("deep", "shallow"):
        return Result.fail(f"map_merge: op 不支持 {op}")
    overwrite = bool(args.get("overwrite", True))
    concat_lists = bool(args.get("concat_lists", False))

    def merge(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in b.items():
            if k in out and isinstance(out[k], dict) and isinstance(v, dict) and op == "deep":
                out[k] = merge(out[k], v)
            elif k in out and concat_lists and isinstance(out[k], list) and isinstance(v, list):
                out[k] = out[k] + v
            elif k in out and not overwrite:
                continue
            else:
                out[k] = v
        return out

    acc: dict = {}
    for d in ds:
        acc = merge(acc, d)
    return _ok(ctx, args, f"合并 {len(ds)} 个字典 → {len(acc)} 键", acc)


@node("map_pick")
async def map_pick(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """筛字典。args: {data, keys?, exclude?, op?, value?, ignore_case?}

    keys/exclude 按 key 过滤；op/value 按 value 过滤（op 同 seq_pick）。
    """
    data = args.get("data")
    if not isinstance(data, dict):
        return Result.fail("map_pick: data 必须是字典")
    keys = args.get("keys")
    exclude = args.get("exclude")
    op = args.get("op")
    value = args.get("value")
    ci = bool(args.get("ignore_case", False))

    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    if isinstance(exclude, str):
        exclude = [k.strip() for k in exclude.split(",") if k.strip()]

    out: dict[str, Any] = {}
    for k, v in data.items():
        if keys and k not in keys:
            continue
        if exclude and k in exclude:
            continue
        if op and not _cmp(v, str(op), value, ci):
            continue
        out[k] = v
    return _ok(ctx, args, f"保留 {len(out)}/{len(data)} 键", out)


@node("map_order")
async def map_order(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """字典排序。args: {data, by=key|value, key?, desc?, ignore_case?, as_pairs=false}

    by=value 且 value 是 dict 时，key 指定按其中哪个字段排。
    """
    data = args.get("data")
    if not isinstance(data, dict):
        return Result.fail("map_order: data 必须是字典")
    by = args.get("by", "key")
    if by not in ("key", "value"):
        return Result.fail(f"map_order: by 不支持 {by}")
    desc = bool(args.get("desc", False))
    ci = bool(args.get("ignore_case", False))
    field = args.get("key")

    def sk(item: tuple[Any, Any]) -> tuple:
        k, v = item
        target = k if by == "key" else (get_path(v, field) if field not in (None, "") else v)
        if target is _MISSING or target is None:
            return (2, 0.0, "")
        f = _num(target)
        if f is not None:
            return (0, f, "")
        s = _to_text(target)
        return (1, 0.0, s.casefold() if ci else s)

    pairs = sorted(data.items(), key=sk, reverse=desc)
    if args.get("as_pairs"):
        out: Any = [{"key": k, "value": v} for k, v in pairs]
    else:
        out = dict(pairs)
    return _ok(ctx, args, f"排序 {len(pairs)} 键", out)


# ---------------------------------------------------------------- 字符串 / CSV


@node("data_regex", raw=("pattern",))
async def data_regex(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """正则抽取。args: {text, pattern, groups?, ignore_case?, multiline?, dotall?,
                        all=true, group?, limit?, default?}

    - groups 给 ["name","age"]（按捕获组顺序）或命名组名 → 每条匹配返回 dict
    - groups 第 i 项写整数 → 取第 i 个捕获组，键名用 "g{i}"
    - 不给 groups → 返回 group(0) 字符串列表
    - all=false 只取第一条
    """
    text = args.get("text")
    pattern = args.get("pattern")
    if text is None:
        return Result.fail("data_regex: 缺 text")
    if not pattern:
        return Result.fail("data_regex: 缺 pattern")

    flags = 0
    if args.get("ignore_case"):
        flags |= re.IGNORECASE
    if args.get("multiline", True):
        flags |= re.MULTILINE
    if args.get("dotall"):
        flags |= re.DOTALL

    try:
        rx = re.compile(str(pattern), flags)
    except re.error as exc:
        return Result.fail(f"data_regex: 正则非法: {exc}")

    text_s = text if isinstance(text, str) else _to_text(text)
    fetch_all = bool(args.get("all", True))
    limit = int(args.get("limit", 0) or 0)
    groups = args.get("groups")
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]

    def one(m: re.Match[str]) -> Any:
        if groups:
            row: dict[str, Any] = {}
            for i, g in enumerate(groups):
                if isinstance(g, int):
                    row[f"g{g}"] = m.group(g)
                    continue
                name = str(g)
                idx = rx.groupindex.get(name, i + 1 if i + 1 <= rx.groups else None)
                if idx is None:
                    return None
                row[name] = m.group(idx)
            return row
        if args.get("group") is not None:
            g = args.get("group")
            g = int(g) if isinstance(g, str) and g.isdigit() else g
            return m.group(g)
        return m.group(0)

    matches: list[Any] = []
    err: str | None = None
    for m in (rx.finditer(text_s) if fetch_all else [rx.search(text_s)]):
        if m is None:
            continue
        val = one(m)
        if val is None:
            err = "data_regex: groups 与正则捕获组不匹配"
            break
        matches.append(val)
        if limit and len(matches) >= limit:
            break

    if err:
        return Result.fail(err)
    if not matches:
        default = args.get("default")
        data = default if default is not None else ([] if fetch_all else None)
        _save(ctx, args, data)
        return Result.done("无匹配", data=data)

    data = matches if fetch_all else matches[0]
    return _ok(ctx, args, f"匹配 {len(matches)} 条", data)


@node("data_template", raw=("template",))
async def data_template(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """模板渲染。args: {template, data?, missing=keep|blank|error}

    data 是字典（按 data 里的字段取值）；data 是列表 → 逐项渲染成列表。
    取不到时走 missing：keep 保留 {xx} / blank 置空 / error 报错。
    """
    tpl = args.get("template")
    if tpl is None:
        return Result.fail("data_template: 缺 template")
    tpl = str(tpl)
    data = args.get("data")
    missing = args.get("missing", "keep")
    if missing not in ("keep", "blank", "error"):
        return Result.fail(f"data_template: missing 不支持 {missing}")

    if isinstance(data, list):
        out: Any = [_render(tpl, it, missing) for it in data]
        msg = f"渲染 {len(out)} 条"
    elif isinstance(data, dict) or data is None:
        out = _render(tpl, data, missing)
        msg = "渲染完成"
    else:
        out = _render(tpl, {args.get("as", "value"): data}, missing)
        msg = "渲染完成"
    return _ok(ctx, args, msg, out)


@node("data_csv_read", raw=("text",))
async def data_csv_read(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """CSV 文本 → 数据。args: {text, delimiter=',', quotechar='"', header=true,
                              skip_empty=true, kind=dicts|rows, limit?}

    kind=dicts 且有表头 → [{列:值}]；无表头或 kind=rows → [[值]]
    空表头补 col1/col2，重复表头自动加 _2
    """
    text = args.get("text")
    if text is None:
        return Result.fail("data_csv_read: 缺 text")
    delimiter = str(args.get("delimiter", ","))
    quotechar = str(args.get("quotechar", '"')) or '"'
    if len(delimiter) != 1:
        return Result.fail("data_csv_read: delimiter 必须是单个字符")
    header = bool(args.get("header", True))
    skip_empty = bool(args.get("skip_empty", True))
    kind = args.get("kind", "dicts")
    if kind not in ("dicts", "rows"):
        return Result.fail(f"data_csv_read: kind 不支持 {kind}")
    limit = int(args.get("limit", 0) or 0)

    text = text if isinstance(text, str) else _to_text(text)
    if text.startswith("\ufeff"):
        text = text[1:]

    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar=quotechar)
        raw = list(reader)
    except csv.Error as exc:
        return Result.fail(f"data_csv_read: CSV 解析失败: {exc}")

    rows = [r for r in raw if not (skip_empty and all((c or "").strip() == "" for c in r))]
    if not rows:
        return _ok(ctx, args, "空 CSV", [])

    if not header or kind == "rows":
        if header and kind == "rows":
            rows = rows[1:] if rows else []
        out = rows[:limit] if limit else rows
        return _ok(ctx, args, f"解析 {len(out)} 行", out)

    heads = rows[0]
    cols: list[str] = []
    used: dict[str, int] = {}
    for i, h in enumerate(heads):
        name = (h or "").strip() or f"col{i + 1}"
        if name in used:
            used[name] += 1
            name = f"{name}_{used[name]}"
        else:
            used[name] = 1
        cols.append(name)

    out_dicts: list[dict[str, Any]] = []
    for r in rows[1:]:
        item = {c: (r[i] if i < len(r) else "") for i, c in enumerate(cols)}
        if limit and len(out_dicts) >= limit:
            break
        out_dicts.append(item)
    return _ok(ctx, args, f"解析 {len(out_dicts)} 行 × {len(cols)} 列", out_dicts)


@node("data_csv_write")
async def data_csv_write(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """数据 → CSV 文本。args: {rows, delimiter=',', header=true, columns?, newline, quoting?}

    rows 是字典数组 → 自动出表头；是二维数组 → 原样写（header 给列表则先写表头）。
    """
    rows = args.get("rows", args.get("data"))
    if rows is None:
        return Result.fail("data_csv_write: 缺 rows")
    rows = _as_list(rows, "data_csv_write")
    delimiter = str(args.get("delimiter", ","))
    if len(delimiter) != 1:
        return Result.fail("data_csv_write: delimiter 必须是单个字符")
    header_arg = args.get("header", True)
    newline = str(args.get("newline", "\r\n"))
    quoting = str(args.get("quoting", "minimal"))
    qmap = {"minimal": csv.QUOTE_MINIMAL, "all": csv.QUOTE_ALL, "none": csv.QUOTE_NONE}
    if quoting not in qmap:
        return Result.fail(f"data_csv_write: quoting 不支持 {quoting}")

    columns = args.get("columns")
    if isinstance(columns, str):
        columns = [c.strip() for c in columns.split(",") if c.strip()]

    buf = io.StringIO(newline="")
    writer = csv.writer(
        buf, delimiter=delimiter, lineterminator=newline, quoting=qmap[quoting]
    )

    if rows and isinstance(rows[0], dict):
        if columns is None:
            columns = []
            for r in rows:
                for k in r:
                    if k not in columns:
                        columns.append(k)
        if header_arg:
            writer.writerow(columns)
        for r in rows:
            writer.writerow([_to_text(r.get(c, "")) for c in columns])
    else:
        if header_arg:
            if isinstance(header_arg, (list, tuple)):
                writer.writerow([_to_text(c) for c in header_arg])
            elif columns:
                writer.writerow([_to_text(c) for c in columns])
        for r in rows:
            r = r if isinstance(r, (list, tuple)) else [r]
            writer.writerow([_to_text(c) for c in r])

    text = buf.getvalue()

    # 给了 path 就直接落盘（原来只返回字符串，传了 path 会被静默忽略，
    # 调用方以为"生成成功"其实文件根本没写出来）
    path = str(args.get("path") or "").strip()
    if path:
        from engine.paths import resolve_path

        target = resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding=str(args.get("encoding") or "utf-8-sig"))
        return _ok(
            ctx,
            args,
            f"写入 {len(rows)} 行 CSV → {path}（{len(text)} 字符）",
            {"path": str(target), "rows": len(rows), "chars": len(text)},
        )

    return _ok(ctx, args, f"生成 {len(rows)} 行 CSV（{len(text)} 字符，未落盘：没给 path）", text)


# ---------------------------------------------------------------- 统计


def _round(v: Any, precision: Any) -> Any:
    if precision in (None, ""):
        return v
    return round(v, int(precision))


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """线性插值分位数（与 numpy.percentile 默认一致）。"""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    p = min(max(float(p), 0.0), 100.0)
    k = (n - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


@node("data_summary")
async def data_summary(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """一次算全：count/sum/min/max/mean/median/stdev/pXX。args: {items, key?, percentiles=[95], precision?}"""
    items = _as_list(args.get("items"), "data_summary")
    nums, bad = _numbers(items, args.get("key"))
    pcts = args.get("percentiles", [95])
    if pcts is None:
        pcts = []
    if not isinstance(pcts, (list, tuple)):
        pcts = [pcts]
    try:
        pcts = [float(p) for p in pcts]
    except (TypeError, ValueError):
        return Result.fail("data_summary: percentiles 必须是数字数组")

    if not nums:
        data = {
            "count": 0, "sum": 0, "min": None, "max": None,
            "mean": None, "median": None, "stdev": None, "skipped": bad,
        }
        for p in pcts:
            data[f"p{p:g}"] = None
        _save(ctx, args, data)
        return Result.done("没有可统计的数字", data=data)

    s = sorted(nums)
    data = {
        "count": len(nums),
        "sum": round(sum(nums), 6),
        "min": s[0],
        "max": s[-1],
        "mean": round(sum(nums) / len(nums), 6),
        "median": statistics.median(s),
        "stdev": round(statistics.stdev(s), 6) if len(s) >= 2 else 0.0,
        "skipped": bad,
    }
    for p in pcts:
        data[f"p{p:g}"] = round(_percentile(s, p), 6)

    if args.get("precision") not in (None, ""):
        prec = int(args["precision"])
        data = {k: (round(v, prec) if isinstance(v, float) else v) for k, v in data.items()}
    return _ok(ctx, args, f"统计 {len(nums)} 个数字", data)
