"""六维定位器的单元测试：选择器优先级、打分权重、歧义报错。"""

from __future__ import annotations

import pytest

from elements.resolve import SCORE, AmbiguousElement, _attr_selectors, _esc, _text_overlap


def test_attr_selector_priority():
    """属性选择器按可靠性排序：testid > id > name > type。"""
    out = _attr_selectors({
        "type": "checkbox",
        "name": "topping",
        "id": "chk1",
        "data-testid": "topping-bacon",
    })
    names = [x[0] for x in out]
    assert names.index("data-testid") < names.index("id") < names.index("name") < names.index("type")


def test_attr_selector_skips_empty():
    out = _attr_selectors({"id": "", "name": "", "type": "text"})
    assert [x[0] for x in out] == ["type"]


def test_attr_selector_skips_dunder_id():
    """__ 开头的 id 是框架生成的随机 id，不该当锚点。"""
    out = _attr_selectors({"id": "__react-abc123"})
    assert out == []


def test_attr_selector_escapes_quotes():
    out = _attr_selectors({"id": 'a"b'})
    assert out and '\\"' in out[0][1]


def test_score_weights_order():
    """权重关系：语义/属性 > 结构 > 邻居 > 几何。"""
    assert SCORE["role"] + SCORE["name"] > SCORE["path"]
    assert SCORE["attrs_id"] > SCORE["path"]
    assert SCORE["path"] > SCORE["sibling"] > SCORE["geometry"]


def test_ambiguous_message_lists_candidates():
    exc = AmbiguousElement("submit", [
        {"index": 0, "score": 40, "tag": "button", "text": "提交订单"},
        {"index": 1, "score": 40, "tag": "button", "text": "提交"},
    ])
    msg = str(exc)
    assert "2 个元素" in msg
    assert "提交订单" in msg
    assert "重新拾取" in msg


def test_esc_handles_quotes():
    assert _esc('he said "hi"') == 'he said \\"hi\\"'


def test_text_overlap_same():
    assert _text_overlap("提交订单", "提交订单")
    assert _text_overlap("提交订单", "请提交订单")      # 包含关系
    assert _text_overlap("Next →", "Next")             # 符号差异


def test_text_overlap_rejects_changed_copy():
    """文案改版（提交订单 → 确认下单）必须判为不同元素 —— path 层据此拒绝命中。

    不拦的话：结构没变、文案变了时 path 照样命中，会静默点到错误的按钮。
    """
    assert not _text_overlap("提交订单", "确认下单")
    assert not _text_overlap("保存", "删除")


def test_text_overlap_empty_allows():
    """没文本可比（纯图标按钮）时放行，不能因为读不到文字就判失败。"""
    assert _text_overlap("", "abc")
    assert _text_overlap("abc", "")
