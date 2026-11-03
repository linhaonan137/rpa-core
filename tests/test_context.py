"""上下文与变量解析测试。重点验证「不静默失败」。"""

from __future__ import annotations

import pytest

from engine.context import BadAccess, BadSyntax, ContextError, ExecutionContext, UndefinedVar


@pytest.fixture()
def ctx() -> ExecutionContext:
    c = ExecutionContext(run_id="r-test")
    c.set_var("n", 5, scope="global")
    c.set_var("name", "发票", scope="global")
    c.set_var("rows", [{"标题": "A", "价": 1}, {"标题": "B", "价": 2}], scope="global")
    c.set_var("d", {"k": "v", "1": "numeric-key"}, scope="global")
    c.set_var("none_val", None, scope="global")
    return c


# ------------------------------------------------------------ 基础替换


def test_simple_token(ctx):
    assert ctx.resolve("值={n}") == "值=5"


def test_whole_token_keeps_type(ctx):
    """整段引用保留原类型（常见实现会变字符串）。"""
    assert ctx.resolve("{n}") == 5
    assert isinstance(ctx.resolve("{rows}"), list)
    assert ctx.resolve("{d}") == {"k": "v", "1": "numeric-key"}


def test_plain_text_untouched(ctx):
    assert ctx.resolve("no braces here") == "no braces here"


def test_none_becomes_empty_string(ctx):
    assert ctx.resolve("x={none_val}") == "x="


# ------------------------------------------------------------ 取值


def test_dict_access(ctx):
    assert ctx.resolve("{rows[0][标题]}") == "A"
    assert ctx.resolve("{d[k]}") == "v"


def test_quoted_key(ctx):
    assert ctx.resolve('{d["k"]}') == "v"
    assert ctx.resolve("{d['k']}") == "v"


def test_negative_index(ctx):
    assert ctx.resolve("{rows[-1][标题]}") == "B"


def test_numeric_string_key(ctx):
    assert ctx.resolve("{d[1]}") == "numeric-key"


def test_mixed_text_with_access(ctx):
    assert ctx.resolve("第一条：{rows[0][标题]}") == "第一条：A"


# ------------------------------------------------------------ 必须报错


def test_dot_notation_rejected(ctx):
    """点号必须报错——否则会静默返回整个对象。"""
    with pytest.raises(BadSyntax, match="点号"):
        ctx.resolve("{rows.标题}")


def test_dot_notation_on_plain_var_rejected(ctx):
    with pytest.raises(BadSyntax, match="点号"):
        ctx.resolve("{d.k}")


def test_undefined_var_rejected(ctx):
    """未定义必须报错——否则会把 {var} 原样当字符串。"""
    with pytest.raises(UndefinedVar, match="未定义"):
        ctx.resolve("{不存在的变量}")


def test_index_out_of_range(ctx):
    with pytest.raises(BadAccess, match="越界"):
        ctx.resolve("{rows[99]}")


def test_missing_key(ctx):
    with pytest.raises(BadAccess, match="键不存在"):
        ctx.resolve("{d[没有这个键]}")


def test_list_with_string_index(ctx):
    with pytest.raises(BadAccess, match="整数下标"):
        ctx.resolve("{rows[标题]}")


def test_scalar_subscript_rejected(ctx):
    with pytest.raises(BadAccess, match="不支持下标"):
        ctx.resolve("{n[0]}")


def test_empty_placeholder_rejected(ctx):
    with pytest.raises(BadSyntax, match="空占位符"):
        ctx.resolve("{}")


# ------------------------------------------------------------ 作用域


def test_scope_isolation(ctx):
    """子作用域改变量不影响外层（S23）。"""
    ctx.set_var("x", "outer", scope="global")
    ctx.push_scope()
    ctx.set_var("x", "inner")
    assert ctx.get_var("x") == "inner"
    ctx.pop_scope()
    assert ctx.get_var("x") == "outer"


def test_inner_reads_outer(ctx):
    ctx.set_var("g", 1, scope="global")
    ctx.push_scope()
    assert ctx.get_var("g") == 1
    ctx.pop_scope()


def test_scope_stack_underflow(ctx):
    with pytest.raises(ContextError, match="作用域栈已空"):
        ctx.pop_scope()


def test_reserved_var_cannot_be_overwritten(ctx):
    with pytest.raises(ContextError, match="保留变量"):
        ctx.set_var("_index", 3)


def test_all_vars_merges_inner_over_outer(ctx):
    ctx.set_var("a", 1, scope="global")
    ctx.push_scope()
    ctx.set_var("a", 2)
    assert ctx.all_vars()["a"] == 2
    ctx.pop_scope()


# ------------------------------------------------------------ 循环


def test_loop_stack(ctx):
    assert not ctx.in_loop
    ctx.push_loop({"kind": "count", "index": 0})
    assert ctx.in_loop
    assert ctx.get_var("_loop_depth") == 1
    ctx.pop_loop()
    assert not ctx.in_loop


def test_loop_pop_without_push(ctx):
    with pytest.raises(ContextError, match="不在循环中"):
        ctx.pop_loop()


# ------------------------------------------------------------ 转义


def test_escaped_braces(ctx):
    assert ctx.resolve(r"\{n\}") == "{n}"


# ------------------------------------------------------------ 快照


def test_snapshot_contains_vars(ctx):
    snap = ctx.snapshot()
    assert snap["run_id"] == "r-test"
    assert snap["vars"]["n"] == 5
    assert snap["loop_depth"] == 0
