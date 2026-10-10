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
) -> str:
    """reasoning_effort=None disables thinking (fast); "low"/"medium"/"high" enables it."""
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "extra_headers": EXTRA_HEADERS,
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


if __name__ == "__main__":
    print(chat("You are a helpful assistant.", " ".join(sys.argv[1:]) or "你好"))
