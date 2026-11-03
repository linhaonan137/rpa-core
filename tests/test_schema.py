"""契约层测试：Flow / Node / Edge 的校验规则。"""

from __future__ import annotations

import pytest

from engine.schema import Edge, Flow, Node, OnFail, Expect, edge, node


def test_minimal_flow_ok():
    f = Flow(
        name="最小流程",
        nodes=[node("n1", "open", url="https://example.com")],
        edges=[],
    )
    assert f.name == "最小流程"
    assert f.version == 1
    assert f.to_json()["nodes"][0]["args"]["url"] == "https://example.com"


def test_serial_flow_ok():
    f = Flow(
        name="两节点串行",
        nodes=[node("n1", "open", url="u"), node("n2", "click", target="#go")],
        edges=[edge("n1", "n2")],
    )
    assert len(f.edges) == 1
    # 普通边序列化后 exit 被排除（exclude_none）
    assert "exit" not in f.to_json()["edges"][0]


def test_edge_alias_roundtrip():
    e = Edge(id="e1", **{"from": "n1"}, to="n2")
    dumped = e.model_dump(by_alias=True)
    assert dumped["from"] == "n1"
    assert "from_" not in dumped


def test_duplicate_node_id_rejected():
    with pytest.raises(ValueError, match="节点 id 重复"):
        Flow(name="x", nodes=[node("n1", "open"), node("n1", "click")])


def test_edge_to_unknown_node_rejected():
    with pytest.raises(ValueError, match="不存在的节点"):
        Flow(
            name="x",
            nodes=[node("n1", "open")],
            edges=[Edge(id="e", **{"from": "n1"}, to="n404")],
        )


def test_branch_exit_on_non_branch_node_rejected():
    with pytest.raises(ValueError, match="不是分支/循环类"):
        Flow(
            name="x",
            nodes=[node("n1", "open"), node("n2", "click")],
            edges=[Edge(id="e", **{"from": "n1"}, to="n2", exit="true")],
        )


def test_branch_exit_on_if_node_ok():
    f = Flow(
        name="x",
        nodes=[node("n1", "if"), node("n2", "click"), node("n3", "click")],
        edges=[
            Edge(id="e1", **{"from": "n1"}, to="n2", exit="true"),
            Edge(id="e2", **{"from": "n1"}, to="n3", exit="false"),
        ],
    )
    assert len(f.edges) == 2


def test_loop_exit_ok():
    f = Flow(
        name="x",
        nodes=[node("n1", "loop"), node("n2", "click"), node("n3", "close")],
        edges=[
            Edge(id="e1", **{"from": "n1"}, to="n2", exit="loop"),
            Edge(id="e2", **{"from": "n1"}, to="n3", exit="done"),
        ],
    )
    assert f.node("n1").type == "loop"


def test_on_fail_back_requires_rollback_to():
    with pytest.raises(ValueError, match="rollback_to"):
        OnFail(mode="back")


def test_on_fail_defaults():
    of = OnFail()
    assert of.mode == "retry"
    assert of.attempts == 2
    assert of.backoff_ms == 1000
    assert of.then is None


def test_expect_requires_at_least_one_signal():
    with pytest.raises(ValueError, match="至少要给"):
        Expect()


def test_expect_empty_string_still_rejected():
    with pytest.raises(ValueError, match="至少要给"):
        Expect(target="", text="", url="")


def test_node_metadata_alias():
    n = Node(id="n1", type="click", **{"_note": "这是注释", "_heal": {"from": "#a"}})
    assert n.note == "这是注释"
    assert n.heal == {"from": "#a"}
    dumped = n.model_dump(by_alias=True)
    assert dumped["_note"] == "这是注释"


def test_unknown_node_field_rejected():
    with pytest.raises(Exception):
        Node(id="n1", type="click", unknown_field=1)  # extra=forbid


def test_flow_extra_key_rejected():
    with pytest.raises(Exception):
        Flow(name="x", unknown_top=1)


def test_display_label():
    assert node("n1", "click").display == "click"
    assert Node(id="n1", type="click", label="点登录").display == "点登录"
