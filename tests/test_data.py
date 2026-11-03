"""nodes/data.py 与 nodes/files.py 的测试。

约定：
  - 不依赖外部文件，一切落盘走 tmp_path
  - 算法节点（data_regex / csv_* / map_path / seq_flat / fs_diff）
    都覆盖正常、边界、异常三类
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import nodes  # noqa: F401  触发注册
from engine.context import ExecutionContext
from nodes import data, files


def C() -> ExecutionContext:
    return ExecutionContext()


# ================================================================ 列表


async def test_list_sort_auto_mixed():
    r = await data.seq_order({"items": [3, "b", 1, None, "a"]}, C())
    assert r.ok
    assert r.data == [1, 3, "a", "b", None]


async def test_list_sort_by_dict_key_desc():
    rows = [{"n": 2, "name": "b"}, {"n": 1, "name": "a"}]
    r = await data.seq_order({"items": rows, "key": "n", "desc": True}, C())
    assert [x["n"] for x in r.data] == [2, 1]


async def test_list_sort_bad_kind():
    r = await data.seq_order({"items": [1], "kind": "magic"}, C())
    assert not r.ok


async def test_list_sort_string_items_rejected():
    r = await data.seq_order({"items": "abc"}, C())
    assert not r.ok
    assert "字符串" in r.error


async def test_list_unique_keeps_order():
    r = await data.seq_uniq({"items": [3, 1, 3, 2, 1]}, C())
    assert r.data == [3, 1, 2]


async def test_list_unique_by_key():
    rows = [{"id": 1, "v": "a"}, {"id": 1, "v": "b"}, {"id": 2, "v": "c"}]
    r = await data.seq_uniq({"items": rows, "key": "id"}, C())
    assert [x["v"] for x in r.data] == ["a", "c"]


async def test_list_unique_last_wins():
    r = await data.seq_uniq({"items": [1, 2, 1], "kind": "last"}, C())
    assert r.data == [2, 1]


async def test_list_flatten_depth_one():
    assert (await data.seq_flat({"items": [1, [2, [3]]]}, C())).data == [1, 2, [3]]


async def test_list_flatten_infinite():
    r = await data.seq_flat({"items": [1, [2, [3, [4]]]], "depth": -1}, C())
    assert r.data == [1, 2, 3, 4]


async def test_list_flatten_depth_zero_is_flat_copy():
    r = await data.seq_flat({"items": [1, [2]] , "depth": 0}, C())
    assert r.data == [1, [2]]


async def test_list_chunk_remainder():
    r = await data.seq_split({"items": [1, 2, 3, 4, 5], "size": 2}, C())
    assert r.data == [[1, 2], [3, 4], [5]]


async def test_list_chunk_size_invalid():
    assert not (await data.seq_split({"items": [1], "size": 0}, C())).ok


async def test_list_filter_gt_on_dicts():
    rows = [{"age": 10}, {"age": 30}, {"age": "x"}]
    r = await data.seq_pick({"items": rows, "key": "age", "op": "gt", "value": 18}, C())
    assert r.data == [{"age": 30}]


async def test_list_filter_regex_ci():
    r = await data.seq_pick(
        {"items": ["Abc", "xbc", "zzz"], "op": "regex", "value": "^a", "ignore_case": True}, C()
    )
    assert r.data == ["Abc"]


async def test_list_filter_bad_op():
    assert not (await data.seq_pick({"items": [1], "op": "wat"}, C())).ok


async def test_list_map_extract_key():
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    assert (await data.seq_map({"items": rows, "key": "a"}, C())).data == [1, 3]


async def test_list_map_project_keys():
    rows = [{"a": 1, "b": 2, "c": 3}]
    r = await data.seq_map({"items": rows, "keys": ["a", "c"]}, C())
    assert r.data == [{"a": 1, "c": 3}]


async def test_list_map_template_and_missing():
    rows = [{"name": "张三", "age": 30}, {"name": "李四"}]
    r = await data.seq_map({"items": rows, "template": "{name}/{age}", "missing": "blank"}, C())
    assert r.data == ["张三/30", "李四/"]


async def test_list_sum_skips_non_numeric():
    r = await data.seq_sum({"items": [1, "2", "x", None, 3.5]}, C())
    assert r.data == 6.5


async def test_list_sum_empty_is_zero():
    assert (await data.seq_sum({"items": []}, C())).data == 0


async def test_list_sum_precision_and_key():
    rows = [{"v": 1.234}, {"v": 2.345}]
    r = await data.seq_sum({"items": rows, "key": "v", "precision": 2}, C())
    assert r.data == 3.58


async def test_list_avg_basic():
    assert (await data.seq_avg({"items": [2, 4]}, C())).data == 3


async def test_list_avg_empty_fails():
    r = await data.seq_avg({"items": ["x"]}, C())
    assert not r.ok


async def test_list_min_max_numbers():
    r = await data.seq_minmax({"items": [5, 2, 9]}, C())
    assert r.data["min"] == 2 and r.data["max"] == 9
    assert r.data["imin"] == 1 and r.data["imax"] == 2


async def test_list_min_max_by_key_and_empty():
    rows = [{"v": 3}, {"v": 1}]
    r = await data.seq_minmax({"items": rows, "key": "v"}, C())
    assert r.data["min"] == {"v": 1}
    assert not (await data.seq_minmax({"items": []}, C())).ok


async def test_list_slice_negative_step():
    r = await data.seq_slice({"items": [1, 2, 3, 4], "start": None, "end": None, "step": -1}, C())
    assert r.data == [4, 3, 2, 1]


async def test_list_slice_bad_step():
    assert not (await data.seq_slice({"items": [1], "step": 0}, C())).ok


async def test_list_union_order():
    r = await data.seq_union({"lists": [[1, 2], [2, 3]]}, C())
    assert r.data == [1, 2, 3]


async def test_list_union_by_key():
    a = [{"id": 1}, {"id": 2}]
    b = [{"id": 2}, {"id": 3}]
    r = await data.seq_union({"lists": [a, b], "key": "id"}, C())
    assert [x["id"] for x in r.data] == [1, 2, 3]


async def test_list_intersect():
    r = await data.seq_inter({"lists": [[1, 2, 3], [2, 3, 4], [3, 2]]}, C())
    assert r.data == [2, 3]


async def test_list_intersect_disjoint():
    assert (await data.seq_inter({"a": [1], "b": [2]}, C())).data == []


async def test_list_diff():
    r = await data.seq_diff({"lists": [[1, 2, 3], [2]]}, C())
    assert r.data == [1, 3]


async def test_list_diff_dedupes_left():
    r = await data.seq_diff({"a": [1, 1, 2], "b": [9]}, C())
    assert r.data == [1, 2]


async def test_list_cartesian_basic():
    r = await data.seq_product({"lists": [[1, 2], ["a"]]}, C())
    assert r.data == [[1, "a"], [2, "a"]]


async def test_list_cartesian_as_dict():
    r = await data.seq_product(
        {"lists": [["S", "M"], ["红"]], "as_dict": True, "keys": ["尺寸", "颜色"]}, C()
    )
    assert r.data == [
        {"尺寸": "S", "颜色": "红"},
        {"尺寸": "M", "颜色": "红"},
    ]


async def test_list_cartesian_limit_guard():
    r = await data.seq_product({"lists": [list(range(100)), list(range(100))], "limit": 10}, C())
    assert not r.ok
    assert "上限" in r.error


# ================================================================ 字典


async def test_dict_get_path_nested():
    d = {"a": {"b": [{"c": 7}]}}
    assert (await data.map_path({"data": d, "path": "a.b[0].c"}, C())).data == 7


async def test_dict_get_path_default_when_missing():
    r = await data.map_path({"data": {"a": 1}, "path": "a.x.y", "default": "无"}, C())
    assert r.ok and r.data == "无"


async def test_dict_get_path_required_fails():
    r = await data.map_path({"data": {}, "path": "a", "required": True}, C())
    assert not r.ok


async def test_dict_flatten():
    d = {"a": {"b": 1}, "c": [7, 8]}
    r = await data.map_flat({"data": d}, C())
    assert r.data == {"a.b": 1, "c.0": 7, "c.1": 8}


async def test_dict_flatten_keep_list_and_sep():
    d = {"a": {"b": 1}, "c": [7]}
    r = await data.map_flat({"data": d, "flatten_list": False, "sep": "/"}, C())
    assert r.data == {"a/b": 1, "c": [7]}


async def test_dict_flatten_max_depth():
    d = {"a": {"b": {"c": 1}}}
    r = await data.map_flat({"data": d, "max_depth": 1}, C())
    assert r.data == {"a.b": {"c": 1}}


async def test_dict_merge_deep():
    r = await data.map_merge({"dicts": [{"a": {"x": 1, "y": 2}}, {"a": {"y": 3}}]}, C())
    assert r.data == {"a": {"x": 1, "y": 3}}


async def test_dict_merge_shallow_and_keep_left():
    d = [{"a": {"x": 1}}, {"a": {"y": 2}}]
    r = await data.map_merge({"dicts": d, "op": "shallow"}, C())
    assert r.data == {"a": {"y": 2}}
    r2 = await data.map_merge({"dicts": [{"k": 1}, {"k": 2}], "overwrite": False}, C())
    assert r2.data == {"k": 1}


async def test_dict_merge_concat_lists():
    r = await data.map_merge({"dicts": [{"t": [1]}, {"t": [2]}], "concat_lists": True}, C())
    assert r.data == {"t": [1, 2]}


async def test_dict_merge_non_dict_fails():
    assert not (await data.map_merge({"dicts": [{"a": 1}, 5]}, C())).ok


async def test_dict_filter_by_keys():
    r = await data.map_pick({"data": {"a": 1, "b": 2, "c": 3}, "keys": ["a", "c"]}, C())
    assert r.data == {"a": 1, "c": 3}


async def test_dict_filter_by_op_and_exclude():
    d = {"a": 1, "b": 5, "c": 9}
    assert (await data.map_pick({"data": d, "op": "gt", "value": 4}, C())).data == {"b": 5, "c": 9}
    assert (await data.map_pick({"data": d, "exclude": "b"}, C())).data == {"a": 1, "c": 9}


async def test_dict_sort_by_value_desc():
    r = await data.map_order({"data": {"a": 1, "b": 9, "c": 5}, "by": "value", "desc": True}, C())
    assert list(r.data) == ["b", "c", "a"]


async def test_dict_sort_pairs_with_nested_key():
    d = {"x": {"n": 2}, "y": {"n": 1}}
    r = await data.map_order({"data": d, "by": "value", "key": "n", "as_pairs": True}, C())
    assert [p["key"] for p in r.data] == ["y", "x"]


async def test_dict_sort_bad_by():
    assert not (await data.map_order({"data": {}, "by": "size"}, C())).ok


# ================================================================ 字符串 / CSV


async def test_regex_extract_named_groups():
    r = await data.data_regex(
        {"text": "a=1, b=22", "pattern": r"(?P<k>\w)=(?P<v>\d+)", "groups": ["k", "v"]}, C()
    )
    assert r.data == [{"k": "a", "v": "1"}, {"k": "b", "v": "22"}]


async def test_regex_extract_positional_groups():
    r = await data.data_regex({"text": "x1 y2", "pattern": r"([a-z])(\d)", "groups": ["c", "n"]}, C())
    assert r.data == [{"c": "x", "n": "1"}, {"c": "y", "n": "2"}]


async def test_regex_extract_ignore_case_and_limit():
    r = await data.data_regex(
        {"text": "A-a-A", "pattern": r"a", "ignore_case": True, "limit": 2}, C()
    )
    assert r.data == ["A", "a"]


async def test_regex_extract_first_only():
    r = await data.data_regex({"text": "1 2 3", "pattern": r"\d+", "all": False}, C())
    assert r.data == "1"


async def test_regex_extract_no_match_default():
    r = await data.data_regex({"text": "abc", "pattern": r"\d+", "default": []}, C())
    assert r.ok and r.data == []


async def test_regex_extract_bad_pattern():
    r = await data.data_regex({"text": "abc", "pattern": "("}, C())
    assert not r.ok
    assert "正则非法" in r.error


async def test_regex_extract_group_mismatch():
    r = await data.data_regex({"text": "ab", "pattern": r"[ab]", "groups": ["x"]}, C())
    assert not r.ok


async def test_str_template_dict():
    r = await data.data_template({"template": "{姓名}今年{age}岁", "data": {"姓名": "张三", "age": 30}}, C())
    assert r.data == "张三今年30岁"


async def test_str_template_escape_and_blank_missing():
    r = await data.data_template(
        {"template": "{{literal}} {missing}", "data": {}, "missing": "blank"}, C()
    )
    assert r.data == "{literal} "


async def test_str_template_error_mode():
    r = await data.data_template({"template": "{nope}", "data": {}, "missing": "error"}, C())
    assert not r.ok


async def test_str_template_list_renders_each():
    r = await data.data_template({"template": "{n}", "data": [{"n": 1}, {"n": 2}]}, C())
    assert r.data == ["1", "2"]


async def test_csv_parse_header_and_quotes():
    text = 'a,b\n1,"x,y"\n2,"line1\nline2"'
    r = await data.data_csv_read({"text": text}, C())
    assert r.data == [{"a": "1", "b": "x,y"}, {"a": "2", "b": "line1\nline2"}]


async def test_csv_parse_no_header_as_rows():
    r = await data.data_csv_read({"text": "1,2\n3,4", "header": False, "kind": "rows"}, C())
    assert r.data == [["1", "2"], ["3", "4"]]


async def test_csv_parse_header_dedupe_and_bom():
    r = await data.data_csv_read({"text": "\ufeff, b, b\n1, 2, 3", "delimiter": ","}, C())
    assert list(r.data[0]) == ["col1", "b", "b_2"]


async def test_csv_parse_skip_empty_and_semicolon():
    r = await data.data_csv_read({"text": "a;b\n\n1;2\n", "delimiter": ";"}, C())
    assert r.data == [{"a": "1", "b": "2"}]


async def test_csv_parse_bad_delimiter():
    assert not (await data.data_csv_read({"text": "a", "delimiter": ";;"}, C())).ok


async def test_csv_generate_dicts_escapes():
    r = await data.data_csv_write({"rows": [{"a": 'x,y', "b": 'say "hi"'}]}, C())
    assert r.data == 'a,b\r\n"x,y","say ""hi"""\r\n'


async def test_csv_generate_rows_with_header_list():
    r = await data.data_csv_write({"rows": [[1, 2]], "header": ["x", "y"], "newline": "\n"}, C())
    assert r.data == "x,y\n1,2\n"


async def test_csv_generate_union_columns():
    rows = [{"a": 1}, {"b": 2}]
    r = await data.data_csv_write({"rows": rows, "newline": "\n"}, C())
    assert r.data.splitlines()[0] == "a,b"


async def test_csv_generate_bad_quoting():
    assert not (await data.data_csv_write({"rows": [[1]], "quoting": "weird"}, C())).ok


# ================================================================ 统计


async def test_stat_summary_full():
    r = await data.data_summary({"items": [1, 2, 3, 4]}, C())
    d = r.data
    assert d["count"] == 4
    assert d["min"] == 1 and d["max"] == 4
    assert d["mean"] == 2.5
    assert d["median"] == 2.5
    assert d["stdev"] == pytest.approx(1.290994, abs=1e-6)
    assert d["p95"] == pytest.approx(3.85, abs=1e-6)


async def test_stat_summary_single_value_stdev_zero():
    d = (await data.data_summary({"items": [7]}, C())).data
    assert d["count"] == 1 and d["stdev"] == 0.0 and d["p95"] == 7


async def test_stat_summary_skips_non_numeric_and_custom_pct():
    d = (await data.data_summary({"items": [10, "5", "x"], "percentiles": [50, 100]}, C())).data
    assert d["count"] == 2 and d["skipped"] == 1
    assert d["p50"] == 7.5 and d["p100"] == 10


async def test_stat_summary_empty():
    d = (await data.data_summary({"items": []}, C())).data
    assert d["count"] == 0 and d["mean"] is None


async def test_stat_summary_by_key():
    rows = [{"v": 2}, {"v": 4}]
    assert (await data.data_summary({"items": rows, "key": "v"}, C())).data["mean"] == 3


# ================================================================ 通用行为


async def test_save_as_writes_variable():
    ctx = C()
    await data.seq_split({"items": [1, 2], "size": 1, "save_as": "chunks"}, ctx)
    assert ctx.get_var("chunks") == [[1], [2]]


async def test_undefined_variable_reports_fail():
    r = await data.seq_order({"items": "{nope}"}, C())
    assert not r.ok
    assert "变量未定义" in r.error


async def test_reserved_save_as_fails_softly():
    r = await data.seq_split({"items": [1], "size": 1, "save_as": "_index"}, C())
    assert not r.ok


# ================================================================ 文件读写


async def test_file_read_text(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("你好", encoding="utf-8")
    r = await files.fs_read({"path": str(p)}, C())
    assert r.ok and r.data == "你好"


async def test_file_read_lines_and_bom(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("\ufeffl1\nl2\n", encoding="utf-8")
    r = await files.fs_read({"path": str(p), "kind": "lines"}, C())
    assert r.data == ["l1", "l2"]


async def test_file_read_json(tmp_path: Path):
    p = tmp_path / "a.json"
    p.write_text('{"k": [1, 2]}', encoding="utf-8")
    assert (await files.fs_read({"path": str(p), "kind": "json"}, C())).data == {"k": [1, 2]}


async def test_file_read_json_broken(tmp_path: Path):
    p = tmp_path / "a.json"
    p.write_text("{oops", encoding="utf-8")
    assert not (await files.fs_read({"path": str(p), "kind": "json"}, C())).ok


async def test_file_read_missing_does_not_raise(tmp_path: Path):
    r = await files.fs_read({"path": str(tmp_path / "none.txt")}, C())
    assert not r.ok and "不存在" in r.error


async def test_file_write_creates_parents(tmp_path: Path):
    p = tmp_path / "x" / "y" / "a.txt"
    r = await files.fs_write({"path": str(p), "content": "hi"}, C())
    assert r.ok and p.read_text(encoding="utf-8") == "hi"


async def test_file_write_json_kind(tmp_path: Path):
    p = tmp_path / "a.json"
    await files.fs_write({"path": str(p), "content": {"a": 1}, "kind": "json"}, C())
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}


async def test_file_write_list_content_and_append_mode(tmp_path: Path):
    p = tmp_path / "a.txt"
    await files.fs_write({"path": str(p), "content": ["a", "b"]}, C())
    await files.fs_write({"path": str(p), "content": "-c", "mode": "append"}, C())
    assert p.read_text(encoding="utf-8") == "a\nb-c"


async def test_file_write_bad_mode(tmp_path: Path):
    r = await files.fs_write({"path": str(tmp_path / "a.txt"), "content": "x", "mode": "wat"}, C())
    assert not r.ok


async def test_file_append_creates_file(tmp_path: Path):
    p = tmp_path / "new.txt"
    await files.fs_append({"path": str(p), "content": "1"}, C())
    await files.fs_append({"path": str(p), "content": "2"}, C())
    assert p.read_text(encoding="utf-8") == "12"


async def test_file_copy(tmp_path: Path):
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "b.txt"
    r = await files.fs_copy({"path": str(src), "dest": str(dst)}, C())
    assert r.ok and dst.read_text(encoding="utf-8") == "x"


async def test_file_copy_into_dir(tmp_path: Path):
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    d = tmp_path / "sub"
    d.mkdir()
    r = await files.fs_copy({"path": str(src), "dest": str(d)}, C())
    assert r.data == str(d / "a.txt")


async def test_file_copy_no_overwrite_fails(tmp_path: Path):
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "b.txt"
    dst.write_text("y", encoding="utf-8")
    assert not (await files.fs_copy({"path": str(src), "dest": str(dst)}, C())).ok
    r = await files.fs_copy({"path": str(src), "dest": str(dst), "overwrite": True}, C())
    assert r.ok


async def test_file_move(tmp_path: Path):
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "b.txt"
    r = await files.fs_move({"path": str(src), "dest": str(dst)}, C())
    assert r.ok and not src.exists() and dst.exists()


async def test_file_move_missing_source(tmp_path: Path):
    r = await files.fs_move({"path": str(tmp_path / "no.txt"), "dest": str(tmp_path / "b.txt")}, C())
    assert not r.ok


async def test_file_delete_file(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    r = await files.fs_delete({"path": str(p)}, C())
    assert r.ok and r.data["deleted"] is True and not p.exists()


async def test_file_delete_missing_ok_and_strict(tmp_path: Path):
    p = tmp_path / "no.txt"
    r = await files.fs_delete({"path": str(p)}, C())
    assert r.ok and r.data["deleted"] is False
    assert not (await files.fs_delete({"path": str(p), "missing_ok": False}, C())).ok


async def test_file_delete_dir(tmp_path: Path):
    d = tmp_path / "sub"
    (d / "x").mkdir(parents=True)
    r = await files.fs_delete({"path": str(d), "kind": "dir"}, C())
    assert r.ok and not d.exists()


async def test_file_delete_dir_with_file_kind_refused(tmp_path: Path):
    d = tmp_path / "sub"
    d.mkdir()
    assert not (await files.fs_delete({"path": str(d), "kind": "file"}, C())).ok


async def test_file_rename(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    r = await files.fs_rename({"path": str(p), "name": "b.txt"}, C())
    assert r.ok and (tmp_path / "b.txt").exists() and not p.exists()


async def test_file_rename_rejects_path(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    r = await files.fs_rename({"path": str(p), "name": "../b.txt"}, C())
    assert not r.ok


async def test_file_exists_kinds(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    d = tmp_path / "sub"
    d.mkdir()
    assert (await files.fs_has({"path": str(f)}, C())).data is True
    assert (await files.fs_has({"path": str(d), "kind": "dir"}, C())).data is True
    assert (await files.fs_has({"path": str(d), "kind": "file"}, C())).data is False
    assert (await files.fs_has({"path": str(tmp_path / "no"), "kind": "any"}, C())).data is False


async def test_file_info(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("hey", encoding="utf-8")
    d = (await files.fs_info({"path": str(p)}, C())).data
    assert d["name"] == "a.txt" and d["suffix"] == ".txt"
    assert d["size"] == 3 and d["is_file"] is True


async def test_file_info_missing(tmp_path: Path):
    assert not (await files.fs_info({"path": str(tmp_path / "no.txt")}, C())).ok


async def test_file_list_pattern(tmp_path: Path):
    for name in ("a.txt", "b.txt", "c.log"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    r = await files.fs_list({"path": str(tmp_path), "pattern": "*.txt"}, C())
    assert [x["name"] for x in r.data] == ["a.txt", "b.txt"]


async def test_file_list_recursive_dirs_only(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("x", encoding="utf-8")
    r = await files.fs_list({"path": str(tmp_path), "recursive": True, "kind": "dirs"}, C())
    assert [x["rel"] for x in r.data] == ["sub", "sub/deep"]


async def test_file_list_as_paths_and_missing_dir(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    r = await files.fs_list({"path": str(tmp_path), "as_paths": True}, C())
    assert r.data == [str(tmp_path / "a.txt")]
    assert not (await files.fs_list({"path": str(tmp_path / "no")}, C())).ok


async def test_dir_create_nested(tmp_path: Path):
    p = tmp_path / "a" / "b" / "c"
    r = await files.fs_mkdir({"path": str(p)}, C())
    assert r.ok and p.is_dir()


async def test_file_hash_sha256(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    d = (await files.fs_hash({"path": str(p)}, C())).data
    assert d["hash"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert d["size"] == 5


async def test_file_hash_bad_algo(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    assert not (await files.fs_hash({"path": str(p), "algo": "nope"}, C())).ok


# ================================================================ 比对


async def test_file_diff_identical(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("1\n2\n", encoding="utf-8")
    b.write_text("1\n2\n", encoding="utf-8")
    r = await files.fs_diff({"path": str(a), "dest": str(b)}, C())
    assert r.data["identical"] is True and r.data["same"] == 2


async def test_file_diff_added_removed_changed(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("1\n2\n3\n", encoding="utf-8")
    b.write_text("1\nX\n3\n4\n", encoding="utf-8")
    d = (await files.fs_diff({"path": str(a), "dest": str(b)}, C())).data
    assert d["identical"] is False
    assert d["changed"] == [{"line_a": 2, "line_b": 2, "old": "2", "new": "X"}]
    assert d["added"] == [{"line": 4, "text": "4"}]
    assert d["removed"] == []
    assert d["stats"] == {"added": 1, "removed": 0, "changed": 1, "same": 2}


async def test_file_diff_removed_block(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a\nb\nc\n", encoding="utf-8")
    b.write_text("a\n", encoding="utf-8")
    d = (await files.fs_diff({"path": str(a), "dest": str(b)}, C())).data
    assert [x["text"] for x in d["removed"]] == ["b", "c"]


async def test_file_diff_ignore_case(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("Hello\n", encoding="utf-8")
    b.write_text("hello\n", encoding="utf-8")
    assert (await files.fs_diff({"path": str(a), "dest": str(b)}, C())).data["identical"] is False
    r = await files.fs_diff({"path": str(a), "dest": str(b), "ignore_case": True}, C())
    assert r.data["identical"] is True


async def test_file_diff_missing_file(tmp_path: Path):
    a = tmp_path / "a.txt"
    a.write_text("x", encoding="utf-8")
    assert not (await files.fs_diff({"path": str(a), "dest": str(tmp_path / "no.txt")}, C())).ok


async def test_dir_hash_compare_same(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b" / "x.txt").write_text("x", encoding="utf-8")
    r = await files.fs_dir_diff({"path": str(tmp_path / "a"), "dest": str(tmp_path / "b")}, C())
    assert r.data["identical"] is True and r.data["same"] == 1


async def test_dir_hash_compare_changed_and_only(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x.txt").write_text("1", encoding="utf-8")
    (b / "x.txt").write_text("2", encoding="utf-8")
    (a / "only_a.txt").write_text("1", encoding="utf-8")
    (b / "only_b.txt").write_text("1", encoding="utf-8")
    d = (await files.fs_dir_diff({"path": str(a), "dest": str(b)}, C())).data
    assert d["identical"] is False
    assert [c["path"] for c in d["changed"]] == ["x.txt"]
    assert d["only_in_path"] == ["only_a.txt"]
    assert d["only_in_dest"] == ["only_b.txt"]
    assert d["same"] == 0


async def test_dir_hash_compare_bad_dir(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    assert not (await files.fs_dir_diff({"path": str(f), "dest": str(tmp_path)}, C())).ok


# ================================================================ 表格


async def test_sheet_write_read_roundtrip(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    rows = [{"名称": "A", "数量": 1}, {"名称": "B", "数量": 2}]
    w = await files.data_sheet_write({"path": str(p), "sheet": "数据", "rows": rows}, C())
    assert w.ok and w.data["rows"] == 2
    r = await files.data_sheet_read({"path": str(p), "sheet": "数据"}, C())
    assert r.data == rows


async def test_sheet_write_overwrite_keeps_other_sheets(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    await files.data_sheet_write({"path": str(p), "sheet": "A", "rows": [{"x": 1}]}, C())
    await files.data_sheet_write({"path": str(p), "sheet": "B", "rows": [{"y": 2}]}, C())
    await files.data_sheet_write({"path": str(p), "sheet": "A", "rows": [{"x": 9}]}, C())
    assert (await files.data_sheet_read({"path": str(p), "sheet": "A"}, C())).data == [{"x": 9}]
    assert (await files.data_sheet_read({"path": str(p), "sheet": "B"}, C())).data == [{"y": 2}]


async def test_sheet_write_append_mode(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    await files.data_sheet_write({"path": str(p), "rows": [{"a": 1}]}, C())
    await files.data_sheet_write({"path": str(p), "rows": [{"a": 2}], "mode": "append"}, C())
    assert (await files.data_sheet_read({"path": str(p)}, C())).data == [{"a": 1}, {"a": 2}]


async def test_sheet_write_matrix_with_header(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    r = await files.data_sheet_write(
        {"path": str(p), "rows": [[1, 2], [3, 4]], "header": ["x", "y"]}, C()
    )
    assert r.ok
    assert (await files.data_sheet_read({"path": str(p)}, C())).data == [{"x": 1, "y": 2}, {"x": 3, "y": 4}]


async def test_sheet_read_no_header_and_range(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    await files.data_sheet_write({"path": str(p), "rows": [[1, 2], [3, 4]], "header": False}, C())
    assert (await files.data_sheet_read({"path": str(p), "header_row": 0}, C())).data == [[1, 2], [3, 4]]
    r = await files.data_sheet_read({"path": str(p), "header_row": 0, "range": "A2:B2"}, C())
    assert r.data == [[3, 4]]


async def test_sheet_read_missing_file(tmp_path: Path):
    r = await files.data_sheet_read({"path": str(tmp_path / "no.xlsx")}, C())
    assert not r.ok


async def test_sheet_read_missing_sheet(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    await files.data_sheet_write({"path": str(p), "sheet": "S1", "rows": [{"a": 1}]}, C())
    r = await files.data_sheet_read({"path": str(p), "sheet": "NOPE"}, C())
    assert not r.ok and "工作表不存在" in r.error


async def test_sheet_append_dict_creates_header(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    r = await files.data_sheet_append({"path": str(p), "sheet": "S", "row": {"a": 1, "b": 2}}, C())
    assert r.ok and r.data["row"] == 2
    assert (await files.data_sheet_read({"path": str(p), "sheet": "S"}, C())).data == [{"a": 1, "b": 2}]


async def test_sheet_append_dict_maps_columns(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    await files.data_sheet_write({"path": str(p), "rows": [{"a": 1, "b": 2}]}, C())
    await files.data_sheet_append({"path": str(p), "row": {"a": 3, "b": 4}}, C())
    assert (await files.data_sheet_read({"path": str(p)}, C())).data == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]


async def test_sheet_append_list_row(tmp_path: Path):
    p = tmp_path / "t.xlsx"
    await files.data_sheet_write({"path": str(p), "rows": [[1, 2]], "header": ["x", "y"]}, C())
    await files.data_sheet_append({"path": str(p), "row": [3, 4]}, C())
    assert (await files.data_sheet_read({"path": str(p)}, C())).data == [
        {"x": 1, "y": 2},
        {"x": 3, "y": 4},
    ]


async def test_sheet_append_missing_row(tmp_path: Path):
    assert not (await files.data_sheet_append({"path": str(tmp_path / "t.xlsx")}, C())).ok


async def test_sheet_write_bad_rows_type(tmp_path: Path):
    r = await files.data_sheet_write({"path": str(tmp_path / "t.xlsx"), "rows": "not-a-list"}, C())
    assert not r.ok
