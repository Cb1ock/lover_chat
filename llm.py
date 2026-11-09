"""DeepSeek V4 Flash via DashScope's OpenAI-compatible API (adapted from deepseek_call.py).

Quick check:  .venv/bin/python llm.py "你好"
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

load_dotenv(Path(__file__).with_name(".env"))

BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
EXTRA_HEADERS = {
    "X-DashScope-DataInspection": '{"input": "disable", "output": "disable"}',
    # Chat replies are latency-sensitive: queue briefly instead of the original 180s.
    "X-DashScope-Wait-Timeout": "30",
}
MAX_ATTEMPTS = 4

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise SystemExit("缺少 DASHSCOPE_API_KEY，请写进项目根目录的 .env")
        _client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=60.0, max_retries=0)
    return _client


def chat(system: str, user: str, **kw) -> str:
    return chat_messages([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)


def chat_messages(
    messages: list[dict],
    *,
    max_tokens: int = 300,
    temperature: float = 0.8,
    reasoning_effort: str | None = None,
    timeout: float = 60.0,
) -> str:
    """reasoning_effort=None disables thinking (fast); "low"/"medium"/"high" enables it.
    Thinking calls (memory, review) need a longer timeout than a quick reply."""
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "extra_headers": EXTRA_HEADERS,
        "timeout": timeout,
    }
    if reasoning_effort is None:
        kwargs["temperature"] = temperature
        kwargs["extra_body"] = {"enable_thinking": False}
    else:
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["extra_body"] = {"enable_thinking": True}
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = _get_client().chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except (RateLimitError, APIConnectionError, APITimeoutError):
            if attempt == MAX_ATTEMPTS - 1:
                raise
            time.sleep(2**attempt + random.uniform(0, 1))
    raise AssertionError("unreachable")


VISION_URL = os.environ.get("VISION_URL", "")  # Gemini-style generateContent, with {model} in it
# Cheapest first; the next one is the fallback.
VISION_MODELS = [m for m in os.environ.get("VISION_MODELS", "gemini-2.5-flash-lite").split(",") if m.strip()]
STRONG_VISION_MODELS = [m for m in os.environ.get("STRONG_VISION_MODELS", "gemini-4-argon").split(",") if m.strip()]
VISION_KEYS = [k for k in os.environ.get("VISION_KEYS", "").split(",") if k.strip()]
VISION_PROMPT = ("这是微信里朋友发来的照片。用一两句中文客观描述：拍的是什么、在哪、人在做什么、氛围怎样。"
                 "不猜身份，不评价长相，不超过 60 字。")


def _gemini(parts: list[dict], *, max_tokens: int = 1024, timeout: float = 60.0,
            models: list[str] | None = None) -> str:
    """One call to the Gemini endpoint in .env (VISION_URL / VISION_MODELS / VISION_KEYS):
    each model, cheapest first, with each key in turn."""
    import json
    import urllib.request
    if not (VISION_URL and VISION_KEYS):
        raise RuntimeError("没配置 VISION_URL / VISION_KEYS")
    body = json.dumps({"contents": [{"role": "user", "parts": parts}],
                       "generationConfig": {"temperature": 0.0, "maxOutputTokens": max_tokens}}).encode()
    last = None
    for model, key in [(m.strip(), k.strip()) for m in (models or VISION_MODELS) for k in VISION_KEYS]:
        req = urllib.request.Request(VISION_URL.format(model=model), data=body, headers={
            "Authorization": "Bearer " + key, "x-goog-api-key": key, "Content-Type": "application/json"})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=timeout))
            return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
        except Exception as e:  # noqa: BLE001 - next model/key
            last = e
    raise RuntimeError(f"看图接口都失败了：{str(last)[:150]}")


def describe_image(data_b64: str, mime: str = "image/jpeg", *, prompt: str = VISION_PROMPT) -> str:
    """One or two plain Chinese sentences about a picture, for the chat model to react to."""
    return _gemini([{"inline_data": {"mime_type": mime, "data": data_b64}}, {"text": prompt}])


STICKER_CHECK = """\
我在微信上跟一个女生聊天，想发这张表情包回她。最近的聊天（“我”是我，最后一行是她刚说的）：
{context}

我们现在还是朋友，我在慢慢往暧昧走：飞吻、亲亲、爱心、比心这类太过了，不合适。
这张表情包放在这里合适吗？要贴合她刚说的话和气氛，不尴尬、不低幼、不显得敷衍。拿不准就答不合适。
只输出一行：合适 或 不合适，后面加一句理由。"""


def sticker_fits(data_b64: str, mime: str, context: str) -> tuple[bool, str]:
    """Look at the sticker itself before sending it: does it fit the moment?"""
    answer = _gemini([{"inline_data": {"mime_type": mime, "data": data_b64}},
                      {"text": STICKER_CHECK.format(context=context)}], max_tokens=400,
                     models=STRONG_VISION_MODELS)  # judging taste needs the better model
    return answer.startswith("合适"), answer


if __name__ == "__main__":
    print(chat("You are a helpful assistant.", " ".join(sys.argv[1:]) or "你好"))
