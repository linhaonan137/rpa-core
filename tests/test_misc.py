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

media = nodes.load("media")
desktop = nodes.load("desktop")
ai = nodes.load("ai")
trigger = nodes.load("trigger")


def new_ctx() -> ExecutionContext:
    return ExecutionContext(run_id="t")


# ================================================================ 媒体下载


@pytest.fixture
def fake_ytdlp(monkeypatch):
    """让二进制定位永远成功，把注意力放在命令拼装与结果处理上。"""
    monkeypatch.setattr(media, "_ytdlp", lambda args: "yt-dlp")
    monkeypatch.setattr(media, "_m3u8dl", lambda args: "N_m3u8DL-RE")


@pytest.fixture
def recorded_run(monkeypatch):
    """记录 argv，返回可编程的退出码。"""
    state: dict = {"code": 0, "out": "[download] 100%", "err": "", "argv": None}

    async def _fake(argv, timeout_ms):
        state["argv"] = argv
        state["timeout_ms"] = timeout_ms
        return state["code"], state["out"], state["err"]

    monkeypatch.setattr(media, "_run", _fake)
    return state


async def test_media_download_ok(tmp_path, fake_ytdlp, recorded_run):
    args = {"url": "https://example.com/v", "path": str(tmp_path), "save_as": "a.mp4"}
    r = await media.media_get(args, new_ctx())

    assert r.ok is True and "下载完成" in r.message
    assert r.data["dir"] == str(tmp_path)
    argv = recorded_run["argv"]
    assert argv[0] == "yt-dlp" and "--no-playlist" in argv
    assert argv[-1] == "https://example.com/v"
    assert os.path.join(str(tmp_path), "a.mp4") in argv


async def test_media_download_quality_passthrough(tmp_path, fake_ytdlp, recorded_run):
    await media.media_get(
        {"url": "u", "path": str(tmp_path), "quality": "bv*[height<=720]+ba"}, new_ctx()
    )
    argv = recorded_run["argv"]
    assert argv[argv.index("-f") + 1] == "bv*[height<=720]+ba"


async def test_media_download_nonzero_is_fail(tmp_path, fake_ytdlp, recorded_run):
    recorded_run.update(code=1, err="ERROR: Video unavailable")
    r = await media.media_get({"url": "u", "path": str(tmp_path)}, new_ctx())
    assert r.ok is False
    assert "退出码 1" in r.error and "Video unavailable" in r.error


async def test_media_download_missing_binary(monkeypatch, tmp_path):
    """二进制不在 → 明确 fail，不能静默成功。"""
    missing = str(tmp_path / "nope" / "yt-dlp.exe")
    monkeypatch.setenv("RPA_YTDLP", missing)
    r = await media.media_get({"url": "u", "path": str(tmp_path)}, new_ctx())
    assert r.ok is False
    assert "找不到" in r.error and "RPA_YTDLP" in r.error


async def test_media_download_requires_url():
    r = await media.media_get({}, new_ctx())
    assert r.ok is False and "url" in r.error


async def test_media_audio_builds_extract_flags(tmp_path, fake_ytdlp, recorded_run):
    r = await media.media_audio(
        {"url": "u", "path": str(tmp_path), "kind": "m4a"}, new_ctx()
    )
    assert r.ok is True and r.data["format"] == "m4a"
    argv = recorded_run["argv"]
    assert "-x" in argv
    assert argv[argv.index("--audio-format") + 1] == "m4a"
    assert argv[argv.index("--audio-quality") + 1] == "0"


async def test_media_playlist_skip_existing_and_max(tmp_path, fake_ytdlp, recorded_run):
    r = await media.media_playlist(
        {"url": "u", "path": str(tmp_path), "count": 3}, new_ctx()
    )
    assert r.ok is True
    argv = recorded_run["argv"]
    assert "--download-archive" in argv
    assert argv[argv.index("--max-downloads") + 1] == "3"
    assert "--yes-playlist" in argv


async def test_media_playlist_no_archive_when_disabled(tmp_path, fake_ytdlp, recorded_run):
    await media.media_playlist(
        {"url": "u", "path": str(tmp_path), "skip_existing": False}, new_ctx()
    )
    assert "--download-archive" not in recorded_run["argv"]


async def test_media_info_parses_and_saves_var(fake_ytdlp, recorded_run):
    recorded_run["out"] = json.dumps(
        {
            "id": "abc",
            "title": "标题",
            "duration": 61,
            "view_count": 9,
            "width": 1920,
            "height": 1080,
            "formats": [{"x": "y" * 100}],
        },
        ensure_ascii=False,
    )
    ctx = new_ctx()
    r = await media.media_info({"url": "u", "save_as": "info"}, ctx)

    assert r.ok is True
    assert r.data["title"] == "标题"
    assert r.data["resolution"] == "1920x1080"
    assert "formats" not in r.data  # 大块头要丢掉
    assert ctx.get_var("info")["id"] == "abc"
    assert "--dump-single-json" in recorded_run["argv"]


async def test_media_info_single_field(fake_ytdlp, recorded_run):
    recorded_run["out"] = json.dumps({"title": "T", "id": "1"}, ensure_ascii=False)
    ctx = new_ctx()
    r = await media.media_info({"url": "u", "kind": "title", "save_as": "v"}, ctx)
    assert r.ok is True and r.data == "T"
    assert ctx.get_var("v") == "T"


async def test_media_info_bad_json_is_fail(fake_ytdlp, recorded_run):
    recorded_run["out"] = "<html>blocked</html>"
    r = await media.media_info({"url": "u"}, new_ctx())
    assert r.ok is False and "解析" in r.error


async def test_m3u8_ok(tmp_path, fake_ytdlp, recorded_run):
    out = tmp_path / "video.mp4"
    out.write_bytes(b"fake")

    r = await media.media_m3u8(
        {
            "url": "https://e.com/a.m3u8",
            "path": str(tmp_path),
            "save_as": "video",
            "decryption_key": "0011",
            "referer": "https://e.com",
        },
        new_ctx(),
    )

    assert r.ok is True and r.data["files"] == ["video.mp4"]
    argv = recorded_run["argv"]
    assert argv[0] == "N_m3u8DL-RE"
    assert argv[argv.index("--key") + 1] == "0011"
    assert "Referer: https://e.com" in argv


async def test_m3u8_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("RPA_M3U8DL", str(tmp_path / "nope" / "N_m3u8DL-RE.exe"))
    r = await media.media_m3u8({"url": "u", "path": str(tmp_path)}, new_ctx())
    assert r.ok is False
    assert "N_m3u8DL-RE" in r.error and "RPA_M3U8DL" in r.error


async def test_m3u8_no_output_file_is_fail(tmp_path, fake_ytdlp, recorded_run):
    """退出码 0 但没落盘 → 仍然是失败（防"看起来成功"）。"""
    r = await media.media_m3u8({"url": "u", "path": str(tmp_path)}, new_ctx())
    assert r.ok is False and "没有生成媒体文件" in r.error


# ================================================================ 桌面


class _Cell:
    """普通单元格：没有 ValuePattern，靠 Name。"""

    def __init__(self, text: str) -> None:
        self.Name = text

    def GetValuePattern(self):
        raise RuntimeError("不支持 ValuePattern")

    def GetChildren(self):
        return []


class _ValueCell:
    """虚拟列表单元格：Name 是空的，只有 Value 有值（还带首尾空格）。"""

    def __init__(self, text: str) -> None:
        self.Name = ""
        self._value = f"  {text}  "

    def GetValuePattern(self):
        return SimpleNamespace(Value=self._value)


class _Row:
    def __init__(self, cells: list, rid: str) -> None:
        self._cells = cells
        self._rid = rid

    def GetChildren(self):
        return self._cells

    def GetRuntimeId(self):
        return (self._rid, 1)


class _Grid:
    ControlTypeName = "DataGridControl"

    def __init__(self, rows: list, pages: list | None = None) -> None:
        self._rows = list(rows)
        self._pages = pages or []
        self.scrolls = 0

    def GetChildren(self):
        return self._rows

    def Exists(self, a=None, b=None):
        return True

    def GetScrollPattern(self):
        if not self._pages:
            raise RuntimeError("没有 ScrollPattern")

        class _Pattern:
            VerticallyScrollable = True

            def Scroll(this, h, v):  # noqa: N805
                self.scrolls += 1
                if self._pages:
                    self._rows.extend(self._pages.pop(0))

        return _Pattern()


def fake_auto(grid):
    """最小可用的 uiautomation 替身。"""

    def _pane(**kwargs):
        return grid

    return SimpleNamespace(
        PaneControl=_pane,
        ControlFromHandle=lambda h: grid,
        DataGridControl=object(),
        TableControl=object(),
        ListControl=object(),
        TreeControl=object(),
    )


def table_rows() -> list:
    return [
        _Row([_Cell("姓名"), _Cell("金额"), _Cell("日期")], "h"),
        _Row([_Cell("张三"), _ValueCell("100"), _Cell("2026-01-01")], "r1"),
        _Row([_Cell("李四"), _ValueCell("200"), _Cell("2026-01-02")], "r2"),
    ]


async def test_desktop_table_dicts(monkeypatch):
    grid = _Grid(table_rows())
    monkeypatch.setattr(desktop, "_uia", lambda: fake_auto(grid))

    ctx = new_ctx()
    r = await desktop.ui_table({"target": "订单表", "save_as": "rows"}, ctx)

    assert r.ok is True
    assert ctx.get_var("rows") == [
        {"姓名": "张三", "金额": "100", "日期": "2026-01-01"},
        {"姓名": "李四", "金额": "200", "日期": "2026-01-02"},
    ]


async def test_desktop_table_rows_kind_and_dedupe(monkeypatch):
    rows = table_rows()
    rows.append(rows[1])  # 同一个 RuntimeId 再来一次 → 必须去重
    grid = _Grid(rows)
    monkeypatch.setattr(desktop, "_uia", lambda: fake_auto(grid))

    r = await desktop.ui_table({"target": "t", "kind": "rows"}, new_ctx())
    assert r.ok is True
    assert len(r.data) == 3  # 表头 + 2 行


async def test_desktop_table_scrolls_for_more_rows(monkeypatch):
    """滚动加载：第二屏出现的行也要抓到，且不重复。"""
    first = table_rows()
    second = [_Row([_Cell("王五"), _ValueCell("300"), _Cell("2026-01-03")], "r3")]
    grid = _Grid(first, pages=[second, []])
    monkeypatch.setattr(desktop, "_uia", lambda: fake_auto(grid))

    r = await desktop.ui_table(
        {"target": "t", "scroll": True, "max_rounds": 5}, new_ctx()
    )
    assert r.ok is True
    assert len(r.data) == 3  # 3 条数据行
    assert r.data[-1]["姓名"] == "王五"
    assert grid.scrolls >= 1


async def test_desktop_table_missing_lib(monkeypatch):
    def _boom():
        raise desktop.UiaMissing("缺少 uiautomation 库（仅 Windows 可用）。pip install uiautomation")

    monkeypatch.setattr(desktop, "_uia", _boom)
    r = await desktop.ui_table({"target": "t"}, new_ctx())
    assert r.ok is False
    assert "pip install uiautomation" in r.error


async def test_desktop_table_no_data(monkeypatch):
    grid = _Grid([])
    monkeypatch.setattr(desktop, "_uia", lambda: fake_auto(grid))
    r = await desktop.ui_table({"target": "t"}, new_ctx())
    assert r.ok is False and "没抓到数据行" in r.error


async def test_desktop_select_text(monkeypatch):
    sent: list[str] = []

    class _Ctrl:
        def SetFocus(self):
            sent.append("focus")

        def Exists(self, a=None, b=None):
            return True

    auto = SimpleNamespace(
        PaneControl=lambda **kw: _Ctrl(),
        SendKeys=lambda keys, waitTime=0: sent.append(keys),
        GetClipboardText=lambda: "选中的文字",
    )
    monkeypatch.setattr(desktop, "_uia", lambda: auto)

    ctx = new_ctx()
    r = await desktop.ui_text({"target": "编辑器"}, ctx)
    assert r.ok is True and r.data == "选中的文字"
    assert "{Ctrl}a" in sent and "{Ctrl}c" in sent
    assert ctx.get_var("selected_text") == "选中的文字"


async def test_desktop_select_text_empty_clipboard(monkeypatch):
    auto = SimpleNamespace(
        PaneControl=lambda **kw: SimpleNamespace(
            SetFocus=lambda: None, Exists=lambda a=None, b=None: True
        ),
        SendKeys=lambda keys, waitTime=0: None,
        GetClipboardText=lambda: "   ",
    )
    monkeypatch.setattr(desktop, "_uia", lambda: auto)
    r = await desktop.ui_text({"target": "x"}, new_ctx())
    assert r.ok is False and "剪贴板为空" in r.error


async def test_desktop_focused(monkeypatch):
    focused = SimpleNamespace(
        AutomationId="txtSearch",
        Name="搜索",
        ClassName="Edit",
        ControlTypeName="EditControl",
        GetRuntimeId=lambda: ("42", 7),
    )
    monkeypatch.setattr(desktop, "_uia", lambda: SimpleNamespace(GetFocusedControl=lambda: focused))

    ctx = new_ctx()
    r = await desktop.ui_focus({}, ctx)
    assert r.ok is True
    assert r.data["automation_id"] == "txtSearch"
    assert ctx.get_var("focused_control")["class_name"] == "Edit"


async def test_desktop_focused_expect_mismatch(monkeypatch):
    focused = SimpleNamespace(
        AutomationId="a", Name="b", ClassName="c", ControlTypeName="EditControl",
        GetRuntimeId=lambda: ("1",),
    )
    monkeypatch.setattr(desktop, "_uia", lambda: SimpleNamespace(GetFocusedControl=lambda: focused))
    r = await desktop.ui_focus({"target": "别的控件"}, new_ctx())
    assert r.ok is False and "不是期望的" in r.error


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


# ================================================================ 触发器


async def test_trigger_webhook_reads_and_consumes(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    event = spool / "0001.json"
    event.write_text(json.dumps({"action": "run", "n": 1}, ensure_ascii=False), encoding="utf-8")

    ctx = new_ctx()
    r = await trigger.trig_hook(
        {"path": str(spool), "save_as": "hook", "limit_ms": 2000, "interval_ms": 10}, ctx
    )

    assert r.ok is True
    assert r.data["body"] == {"action": "run", "n": 1}
    assert event.exists() is False  # consume 默认删
    assert ctx.get_var("hook")["body"]["action"] == "run"


async def test_trigger_webhook_keep_file(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    event = spool / "a.txt"
    event.write_text("hello", encoding="utf-8")

    r = await trigger.trig_hook(
        {"path": str(spool), "kind": "text", "consume": False, "limit_ms": 2000}, new_ctx()
    )
    assert r.ok is True and r.data["body"] == "hello"
    assert event.exists() is True


async def test_trigger_webhook_timeout(tmp_path):
    r = await trigger.trig_hook(
        {"path": str(tmp_path / "empty"), "limit_ms": 60, "interval_ms": 5}, new_ctx()
    )
    assert r.ok is False and "超时" in r.error


async def test_trigger_api_waits_for_condition(monkeypatch):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        status = "pending" if state["calls"] < 2 else "done"
        return httpx.Response(200, json={"data": {"status": status}})

    monkeypatch.setattr(trigger, "_TRANSPORT", httpx.MockTransport(handler))
    ctx = new_ctx()
    r = await trigger.trig_api(
        {
            "target": "https://api.example.com/job",
            "path": "data.status",
            "value": "done",
            "save_as": "resp",
            "limit_ms": 2000,
            "interval_ms": 5,
        },
        ctx,
    )

    assert r.ok is True
    assert r.data["value"] == "done"
    assert state["calls"] >= 2
    assert ctx.get_var("resp")["attempts"] == state["calls"]


async def test_trigger_api_timeout(monkeypatch):
    monkeypatch.setattr(
        trigger,
        "_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": False})),
    )
    r = await trigger.trig_api(
        {"target": "https://e.com", "path": "ok", "value": True, "limit_ms": 60, "interval_ms": 5},
        new_ctx(),
    )
    assert r.ok is False and "超时" in r.error


async def test_trigger_api_http_error_then_timeout(monkeypatch):
    monkeypatch.setattr(
        trigger,
        "_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(503, text="down")),
    )
    r = await trigger.trig_api(
        {"target": "https://e.com", "limit_ms": 60, "interval_ms": 5}, new_ctx()
    )
    assert r.ok is False and "503" in r.error


async def test_trigger_api_requires_url():
    r = await trigger.trig_api({}, new_ctx())
    assert r.ok is False and "target" in r.error


async def test_trigger_file_exists(tmp_path):
    f = tmp_path / "ready.csv"
    f.write_text("a,b", encoding="utf-8")
    r = await trigger.trig_file({"target": str(f), "kind": "exists"}, new_ctx())
    assert r.ok is True and r.data["size"] == 3


async def test_trigger_file_timeout(tmp_path):
    r = await trigger.trig_file(
        {"target": str(tmp_path / "never.txt"), "limit_ms": 60, "interval_ms": 5}, new_ctx()
    )
    assert r.ok is False and "超时" in r.error


async def test_trigger_file_change(monkeypatch, tmp_path):
    """mtime/size 变化才算 change —— 用桩序列避免依赖真实等待。"""
    f = tmp_path / "grow.log"
    f.write_text("x", encoding="utf-8")
    states = [{"size": 1, "mtime": 1.0}, {"size": 9, "mtime": 2.0}, {"size": 9, "mtime": 2.0}]
    seq = iter(states)

    monkeypatch.setattr(trigger, "_stat", lambda p: next(seq, states[-1]))

    r = await trigger.trig_file(
        {"target": str(f), "kind": "change", "limit_ms": 2000, "interval_ms": 1}, new_ctx()
    )
    assert r.ok is True and r.data["size"] == 9


async def test_trigger_file_change_requires_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(trigger, "_stat", lambda p: None)
    r = await trigger.trig_file({"target": str(tmp_path / "x"), "kind": "change"}, new_ctx())
    assert r.ok is False and "已存在" in r.error


async def test_trigger_file_bad_kind():
    r = await trigger.trig_file({"target": "x", "kind": "explode"}, new_ctx())
    assert r.ok is False and "不支持的 kind" in r.error


async def test_trigger_time_delay():
    ctx = new_ctx()
    r = await trigger.trig_time({"kind": "delay", "delay_ms": 10, "limit_ms": 2000}, ctx)
    assert r.ok is True
    assert ctx.get_var("time_event")["delay_ms"] == 10


async def test_trigger_time_past_datetime_fires_immediately():
    r = await trigger.trig_time({"target": "2020-01-01 00:00:00"}, new_ctx())
    assert r.ok is True and r.data["target"] == "2020-01-01 00:00:00"


async def test_trigger_time_bad_format():
    r = await trigger.trig_time({"target": "下午茶时间"}, new_ctx())
    assert r.ok is False and "认不出时间格式" in r.error


async def test_trigger_time_timeout():
    r = await trigger.trig_time({"target": "23:59:59", "limit_ms": 50, "interval_ms": 5}, new_ctx())
    # 除非测试正好在 23:59:59 之后跑，否则一定超时
    assert r.ok is False and "超时" in r.error


async def test_trigger_hotkey_fires(monkeypatch):
    fake_kb = SimpleNamespace(is_pressed=lambda key: True)
    monkeypatch.setattr(trigger, "_keyboard", lambda: fake_kb)
    monkeypatch.setattr(trigger, "_release", lambda kb, keys: _noop())

    ctx = new_ctx()
    r = await trigger.trig_hotkey({"target": "ctrl+alt+f1", "limit_ms": 1000}, ctx)

    assert r.ok is True
    assert r.data["keys"] == ["ctrl", "alt", "f1"]
    assert ctx.get_var("hotkey_event")["hotkey"] == "ctrl+alt+f1"


async def _noop() -> None:
    return None


async def test_trigger_hotkey_missing_lib(monkeypatch):
    def _boom():
        raise trigger.TriggerError("缺少 keyboard 库：pip install keyboard")

    monkeypatch.setattr(trigger, "_keyboard", _boom)
    r = await trigger.trig_hotkey({"target": "ctrl+k"}, new_ctx())
    assert r.ok is False and "pip install keyboard" in r.error


async def test_trigger_hotkey_timeout(monkeypatch):
    monkeypatch.setattr(
        trigger, "_keyboard", lambda: SimpleNamespace(is_pressed=lambda k: False)
    )
    r = await trigger.trig_hotkey(
        {"target": "ctrl+k", "limit_ms": 60, "interval_ms": 5}, new_ctx()
    )
    assert r.ok is False and "超时" in r.error


async def test_trigger_hotkey_requires_target():
    r = await trigger.trig_hotkey({}, new_ctx())
    assert r.ok is False and "target" in r.error


# ================================================================ 注册一致性


def test_all_misc_nodes_registered():
    from engine.registry import registry

    expect = {
        "media_get", "media_audio", "media_playlist", "media_info", "media_m3u8",
        "ui_table", "ui_text", "ui_focus",
        "llm_chat", "llm_vision", "llm_extract", "llm_normalize", "llm_classify",
        "llm_selector",
        "trig_hook", "trig_api", "trig_file", "trig_time", "trig_hotkey",
    }
    missing = {t for t in expect if not registry.has(t)}
    assert not missing, f"未注册: {missing}"


def test_missing_lib_does_not_break_import():
    """uiautomation 没装时模块仍可导入（延迟 import）。"""
    assert callable(desktop._uia)
    assert callable(media._run)
    assert callable(trigger._keyboard)
