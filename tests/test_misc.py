"""杂项节点测试：媒体下载 5 / 桌面 3 / AI 6 / 触发器 5。

全部脱机：外部二进制用假 `_run`，UIA 用假模块，LLM 用 httpx.MockTransport，
`keyboard` 用假模块。**没有一条测试依赖真实网络 / 真实浏览器 / 真实桌面。**
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest

import nodes
from engine.context import ExecutionContext

ai = nodes.load("ai")


def new_ctx() -> ExecutionContext:
    return ExecutionContext(run_id="t")


# ================================================================ AI


def llm_transport(content: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"choices": [{"message": {"content": content}}]}
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def llm(monkeypatch):
    """给 key + 可替换的 transport。返回 setter。"""
    monkeypatch.setenv("RPA_LLM_KEY", "test-key")
    monkeypatch.delenv("RPA_LLM_URL", raising=False)
    monkeypatch.delenv("RPA_LLM_MODEL", raising=False)
    calls: list[dict] = []

    def _set(content: str, status: int = 200) -> list[dict]:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(
                {
                    "url": str(request.url),
                    "headers": dict(request.headers),
                    "json": json.loads(request.content),
                }
            )
            return httpx.Response(
                status, json={"choices": [{"message": {"content": content}}]}
            )

        monkeypatch.setattr(ai, "_TRANSPORT", httpx.MockTransport(handler))
        return calls

    return _set


async def test_ai_chat_ok(llm):
    calls = llm("你好，我是助手")
    ctx = new_ctx()
    r = await ai.llm_chat({"prompt": "在吗", "system": "你是客服"}, ctx)

    assert r.ok is True and r.data == "你好，我是助手"
    assert ctx.get_var("ai_reply") == "你好，我是助手"
    body = calls[0]["json"]
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"] == "在吗"
    assert calls[0]["url"].endswith("/chat/completions")
    assert calls[0]["headers"]["authorization"] == "Bearer test-key"


async def test_ai_chat_without_key_is_fail(monkeypatch):
    monkeypatch.delenv("RPA_LLM_KEY", raising=False)
    r = await ai.llm_chat({"prompt": "hi"}, new_ctx())
    assert r.ok is False
    assert "缺少 API Key" in r.error and "RPA_LLM_KEY" in r.error


async def test_ai_chat_http_error(llm):
    llm("", status=500)
    r = await ai.llm_chat({"prompt": "hi"}, new_ctx())
    assert r.ok is False and "500" in r.error


async def test_ai_chat_empty_content_is_fail(llm):
    llm("   ")
    r = await ai.llm_chat({"prompt": "hi"}, new_ctx())
    assert r.ok is False and "空内容" in r.error


async def test_ai_chat_requires_prompt():
    r = await ai.llm_chat({}, new_ctx())
    assert r.ok is False and "prompt" in r.error


async def test_ai_vision_local_file_as_data_uri(llm, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG fake")
    calls = llm("图里有一只猫")

    r = await ai.llm_vision({"target": str(img), "prompt": "看到什么"}, new_ctx())
    assert r.ok is True and r.data == "图里有一只猫"

    content = calls[0]["json"]["messages"][0]["content"]
    image_part = next(p for p in content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


async def test_ai_vision_url_passthrough(llm):
    calls = llm("ok")
    await ai.llm_vision({"target": "https://e.com/a.jpg"}, new_ctx())
    content = calls[0]["json"]["messages"][0]["content"]
    image = next(p for p in content if p["type"] == "image_url")
    assert image["image_url"]["url"] == "https://e.com/a.jpg"


async def test_ai_vision_missing_file(llm):
    r = await ai.llm_vision({"target": "C:/definitely/not/here.png"}, new_ctx())
    assert r.ok is False and "找不到图片文件" in r.error


async def test_ai_extract_ok(llm):
    llm('```json\n{"标题": "合同A", "金额": null}\n```')
    ctx = new_ctx()
    r = await ai.llm_extract(
        {"text": "合同A，金额待定", "fields": ["标题", "金额"], "save_as": "rec"}, ctx
    )

    assert r.ok is True
    assert r.data == {"标题": "合同A", "金额": None}
    assert ctx.get_var("rec")["标题"] == "合同A"
    assert r.meta["missing"] == []


async def test_ai_extract_reports_missing_field(llm):
    llm('{"标题": "x"}')
    r = await ai.llm_extract({"target": "原文", "fields": ["标题", "价格"]}, new_ctx())
    assert r.ok is True and r.meta["missing"] == ["价格"]


async def test_ai_extract_bad_json_is_fail(llm):
    llm("抱歉，我无法回答")
    r = await ai.llm_extract({"target": "原文", "fields": ["a"]}, new_ctx())
    assert r.ok is False and "JSON" in r.error


async def test_ai_extract_requires_fields(llm):
    r = await ai.llm_extract({"target": "原文"}, new_ctx())
    assert r.ok is False and "fields" in r.error


async def test_ai_normalize_date(llm):
    calls = llm('"2026-11-03"')
    ctx = new_ctx()
    r = await ai.llm_normalize({"target": "2026年11月3号", "kind": "date"}, ctx)

    assert r.ok is True and r.data == "2026-11-03"
    assert ctx.get_var("ai_norm") == "2026-11-03"
    assert "YYYY-MM-DD" in calls[0]["json"]["messages"][0]["content"]


async def test_ai_normalize_money(llm):
    llm("1234.00")
    r = await ai.llm_normalize({"target": "¥1,234", "kind": "money"}, new_ctx())
    assert r.ok is True and r.data == "1234.00"


async def test_ai_classify_ok(llm):
    llm("退款")
    ctx = new_ctx()
    r = await ai.llm_classify({"target": "我要退款", "fields": ["退款", "咨询"]}, ctx)
    assert r.ok is True and r.data == "退款"
    assert ctx.get_var("ai_class") == "退款"


async def test_ai_classify_out_of_labels_is_fail(llm):
    llm("投诉")
    r = await ai.llm_classify({"target": "x", "fields": ["退款", "咨询"]}, new_ctx())
    assert r.ok is False and "不是给定标签" in r.error


async def test_ai_element_selector_ok(llm):
    llm("```\nbutton[data-testid='submit']\n```")
    ctx = new_ctx()
    r = await ai.llm_selector(
        {"prompt": "提交按钮", "save_as": "sel"}, ctx
    )
    assert r.ok is True and r.data == "button[data-testid='submit']"
    assert ctx.get_var("sel") == "button[data-testid='submit']"


async def test_ai_element_selector_requires_prompt(llm):
    r = await ai.llm_selector({}, new_ctx())
    assert r.ok is False and "prompt" in r.error


# ================================================================ 注册一致性


def test_all_misc_nodes_registered():
    from engine.registry import registry

    expect = {
        "llm_chat", "llm_vision", "llm_extract", "llm_normalize", "llm_classify",
        "llm_selector",
    }
    missing = {t for t in expect if not registry.has(t)}
    assert not missing, f"未注册: {missing}"
