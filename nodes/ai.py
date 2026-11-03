"""AI 节点：一个大脑 + 一个视觉 + 四个数据加工。

同一个 LLM 调用换 prompt，所以只有一处 HTTP 封装（`_call`）。

三条硬规则：
  1. **没有 key 必须 fail**——假装成功返回空字符串是最贵的 bug：
     下游拿着 "" 去填表单，流程全绿，数据全空。
  2. **只认 OpenAI 兼容协议**（/chat/completions）——一句话覆盖
     OpenAI / DeepSeek / Qwen / Ollama / vLLM，不为每家写适配器。
  3. **密钥只从 args 或环境变量读，不落日志**。

测试用 `httpx.MockTransport` 注入到模块级 `_TRANSPORT`，不碰网络。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from typing import Any

import httpx

from engine.context import ExecutionContext
from engine.registry import Result, register

# ---------------------------------------------------------------- 配置

KEY_ENV = "RPA_LLM_KEY"
URL_ENV = "RPA_LLM_URL"
MODEL_ENV = "RPA_LLM_MODEL"

DEFAULT_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_MS = 60_000

#: 测试注入点：httpx.MockTransport 实例。生产永远为 None。
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class AiError(RuntimeError):
    """LLM 调用相关的所有可预期失败（转成 Result.fail，不穿透）。"""


def _cfg(args: dict[str, Any]) -> dict[str, str]:
    """args 优先，环境变量兜底。没有 key 直接报错。"""
    key = str(args.get("api_key") or os.environ.get(KEY_ENV, "")).strip()
    if not key:
        raise AiError(
            f"缺少 API Key：请在 args.api_key 里给，或设置环境变量 {KEY_ENV}。"
            "（没有 key 时节点不会返回空结果冒充成功）"
        )
    url = str(args.get("api_url") or os.environ.get(URL_ENV, "") or DEFAULT_URL).strip()
    model = str(args.get("model") or os.environ.get(MODEL_ENV, "") or DEFAULT_MODEL).strip()
    return {"key": key, "url": url, "model": model}


def _endpoint(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _timeout_s(args: dict[str, Any], default_ms: int = DEFAULT_TIMEOUT_MS) -> float:
    raw = args.get("limit_ms") or default_ms
    try:
        return max(int(raw), 1) / 1000
    except (TypeError, ValueError):
        return default_ms / 1000


def _client(timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout_s, transport=_TRANSPORT)


def _content_of(payload: dict[str, Any]) -> str:
    """从 chat/completions 响应里取文本。兼容 content 是分段数组的厂商。"""
    try:
        message = payload["choices"][0]["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError):
        raise AiError(f"响应没有 choices[0].message.content：{json.dumps(payload, ensure_ascii=False)[:300]}")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content or "")


async def _call(
    args: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_tokens: int = 2000,
    temperature: float = 0.7,
) -> str:
    """一次 chat/completions 调用，返回文本。所有失败抛 AiError。"""
    cfg = _cfg(args)
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": float(args.get("temperature", temperature)),
        "max_tokens": int(args.get("max_tokens", max_tokens)),
    }
    url = _endpoint(cfg["url"])
    headers = {
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
    }

    try:
        async with _client(_timeout_s(args, timeout_ms)) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise AiError(f"LLM 请求超时（{url}）: {exc}") from exc
    except httpx.RequestError as exc:
        raise AiError(f"LLM 请求失败（{url}）: {type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 400:
        raise AiError(f"LLM 返回 {resp.status_code}（{url}）：{resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise AiError(f"LLM 响应不是 JSON（{url}）：{resp.text[:300]}") from exc
    return _content_of(data)


def _put(ctx: ExecutionContext, args: dict[str, Any], default: str, value: Any) -> str:
    name = str(args.get("save_as") or default)
    ctx.set_var(name, value)
    return name


def _text_arg(ctx: ExecutionContext, args: dict[str, Any]) -> str:
    """输入文本：text / input / target 三种写法都认（target 是统一参数名）。"""
    for key in ("text", "input", "target"):
        val = args.get(key)
        if val not in (None, ""):
            return str(ctx.resolve(val))
    return ""


def _strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏。模型很爱加。"""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_json(text: str) -> Any:
    raw = _strip_fence(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 模型常在前后加解释文字，退一步只取最外层 {} / []
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        raise AiError(f"模型没返回 JSON：{text[:300]}")
    start = min(starts)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if end <= start:
        raise AiError(f"模型返回的 JSON 不完整：{text[:300]}")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AiError(f"JSON 解析失败: {exc}；原文: {text[:300]}") from exc


# ---------------------------------------------------------------- 节点


@register("llm_chat")
async def llm_chat(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """通用对话。args: {prompt, system?, model?, save_as=ai_reply, temperature, max_tokens, limit_ms}"""
    prompt = str(ctx.resolve(args.get("prompt")) or "").strip()
    if not prompt:
        return Result.fail("llm_chat 缺 prompt")

    system = str(ctx.resolve(args.get("system")) or "").strip()
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        text = await _call(args, messages)
    except AiError as exc:
        return Result.fail(str(exc))

    if not text.strip():
        return Result.fail("llm_chat 返回空内容（模型拒答或被 max_tokens 截断）")

    name = _put(ctx, args, "ai_reply", text)
    return Result.done(f"AI 回复 {len(text)} 字 → {name}", data=text)


@register("llm_vision")
async def llm_vision(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """看图。args: {target=图片路径或 URL | path, prompt, kind=auto|file|url, save_as=llm_vision}"""
    image = str(ctx.resolve(args.get("target") or args.get("path")) or "").strip()
    if not image:
        return Result.fail("llm_vision 缺 target（图片路径或 URL）")

    prompt = str(ctx.resolve(args.get("prompt")) or "请详细描述这张图里的内容").strip()
    kind = str(args.get("kind") or "auto")

    if kind == "file" or (kind == "auto" and not image.lower().startswith(("http://", "https://", "data:"))):
        if not os.path.isfile(image):
            return Result.fail(f"llm_vision 找不到图片文件: {image}")
        mime = mimetypes.guess_type(image)[0] or "image/png"
        try:
            with open(image, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
        except OSError as exc:
            return Result.fail(f"llm_vision 读图片失败: {exc}")
        image_url = f"data:{mime};base64,{b64}"
    else:
        image_url = image

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }
    ]

    try:
        text = await _call(args, messages, max_tokens=1000)
    except AiError as exc:
        return Result.fail(str(exc))

    if not text.strip():
        return Result.fail("llm_vision 返回空内容")

    name = _put(ctx, args, "llm_vision", text)
    return Result.done(f"视觉结果 {len(text)} 字 → {name}", data=text)


@register("llm_extract")
async def llm_extract(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """按字段列表抽成 dict。

    args: {target=原文 | text, fields=["标题","价格"], save_as=llm_extract, prompt?}
    字段可以是字符串，也可以是 {name, desc} 描述对象。
    """
    text = _text_arg(ctx, args)
    if not text.strip():
        return Result.fail("llm_extract 缺输入文本（text / target）")

    fields = ctx.resolve(args.get("fields"))
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    if not fields:
        return Result.fail("llm_extract 缺 fields（字段列表）")

    names: list[str] = []
    spec_lines: list[str] = []
    for f in fields:
        if isinstance(f, dict):
            fname = str(f.get("name") or "").strip()
            if not fname:
                return Result.fail(f"llm_extract 的字段对象缺 name: {f}")
            names.append(fname)
            spec_lines.append(f'- "{fname}"：{f.get("desc") or "按原文提取"}')
        else:
            names.append(str(f))
            spec_lines.append(f'- "{f}"')

    system = (
        "你是结构化抽取器。只输出一个 JSON 对象，不要解释、不要 markdown 围栏。"
        f"字段必须且只能是：{', '.join(names)}。"
        "原文里没有的信息填 null，不要编造。"
    )
    user = "字段：\n" + "\n".join(spec_lines) + f"\n\n原文：\n{text}"

    try:
        raw = await _call(
            args,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=2000,
        )
        data = _parse_json(raw)
    except AiError as exc:
        return Result.fail(str(exc))

    if not isinstance(data, dict):
        return Result.fail(f"llm_extract 期望 JSON 对象，得到 {type(data).__name__}")

    missing = [n for n in names if n not in data]
    name = _put(ctx, args, "llm_extract", data)
    msg = f"抽取 {len(data)}/{len(names)} 字段 → {name}"
    if missing:
        msg += f"（模型漏了: {missing}）"
    # 漏字段不判失败：模型确实没找到时，下游更需要"部分结果 + 漏了哪些"
    return Result(ok=True, message=msg, data=data, meta={"missing": missing})


@register("llm_normalize")
async def llm_normalize(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """规整脏数据。args: {target|text, kind=date|money|phone|number|generic, format?, save_as}"""
    text = _text_arg(ctx, args)
    if not text.strip():
        return Result.fail("llm_normalize 缺输入值（text / target）")

    kind = str(args.get("kind") or "generic")
    target_format = str(args.get("format") or "").strip()
    rules = {
        "date": f"规整成日期，目标格式 {target_format or 'YYYY-MM-DD'}",
        "money": "规整成纯数字金额（去掉货币符号和千分位，保留两位小数）",
        "phone": "规整成纯数字手机号（去掉空格、横线、+86 前缀）",
        "number": "规整成数字（去掉千分位、单位、百分号）",
    }
    rule = rules.get(kind, "规整成干净的单个值，去掉多余空白与修饰词")

    system = (
        "你是数据清洗器。只输出规整后的最终值，不要引号、不要解释。"
        f"任务：{rule}。无法规整时输出空字符串。"
    )
    try:
        out = await _call(
            args,
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=100,
        )
    except AiError as exc:
        return Result.fail(str(exc))

    value = _strip_fence(out).strip().strip('"').strip("'").strip()
    name = _put(ctx, args, "ai_norm", value)
    return Result.done(f"规整为 {value!r} → {name}", data=value)


@register("llm_classify")
async def llm_classify(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """分类。args: {target|text, fields=[标签...] | labels, save_as=ai_class}"""
    text = _text_arg(ctx, args)
    if not text.strip():
        return Result.fail("llm_classify 缺输入文本（text / target）")

    labels = ctx.resolve(args.get("fields") or args.get("labels"))
    if isinstance(labels, str):
        labels = [x.strip() for x in labels.split(",") if x.strip()]
    if not labels:
        return Result.fail("llm_classify 缺标签列表（fields / labels）")
    labels = [str(x) for x in labels]

    system = (
        "你是分类器。只能从给定标签里选一个，原样输出标签文字，不要解释。\n"
        f"标签：{', '.join(labels)}"
    )
    try:
        out = await _call(
            args,
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=50,
        )
    except AiError as exc:
        return Result.fail(str(exc))

    answer = _strip_fence(out).strip().strip('"').strip("'").strip()
    # 模型偶尔加句号或前后缀，做一次包含匹配
    hit = answer if answer in labels else next((lb for lb in labels if lb in answer), None)
    if hit is None:
        return Result.fail(f"llm_classify 返回的不是给定标签: {answer!r}（可选 {labels}）")

    name = _put(ctx, args, "ai_class", hit)
    return Result.done(f"分类 = {hit} → {name}", data=hit)


@register("llm_selector")
async def llm_selector(args: dict[str, Any], ctx: ExecutionContext) -> Result:
    """自然语言描述 → CSS 选择器。

    args: {target|prompt=元素描述, html?=页面片段, save_as=ai_selector, kind=css|xpath}
    """
    desc = str(ctx.resolve(args.get("prompt") or args.get("target")) or "").strip()
    if not desc:
        return Result.fail("llm_selector 缺 prompt（元素描述）")

    kind = str(args.get("kind") or "css").lower()
    html = str(ctx.resolve(args.get("html")) or "").strip()
    system = (
        "你是 Playwright 选择器专家。只输出一行选择器本身，不要解释、不要反引号。"
        f"优先输出 {kind.upper()} 选择器；有 data-testid / aria-label / 稳定 id 时优先用它们。"
    )
    user = f"要找的元素：{desc}"
    if html:
        user += f"\n\n页面片段（截断）：\n{html[:4000]}"

    try:
        out = await _call(
            args,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=200,
        )
    except AiError as exc:
        return Result.fail(str(exc))

    selector = _strip_fence(out).strip().splitlines()[0].strip().strip("`").strip()
    if not selector:
        return Result.fail("llm_selector 返回空选择器")

    name = _put(ctx, args, "ai_selector", selector)
    return Result.done(f"选择器 {selector} → {name}", data=selector)
