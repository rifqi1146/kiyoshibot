from __future__ import annotations

import logging
import os

from utils import web_tools

log = logging.getLogger(__name__)

ASK_API_BASE = os.getenv("ASK_API_BASE", os.getenv("CACA_API_BASE", "https://shiteru.id/v1")).strip().rstrip("/")
ASK_ENDPOINT = f"{ASK_API_BASE}/chat/completions"
ASK_MODEL = os.getenv("ASK_MODEL", os.getenv("CACA_MODEL", "deepseek-v4-flash")).strip()
ASK_MAX_TOKENS = min(int(os.getenv("ASK_MAX_TOKENS", "4096")), 8192)
ASK_TIMEOUT = int(os.getenv("ASK_TIMEOUT", "120"))
ASK_TEMPERATURE = float(os.getenv("ASK_TEMPERATURE", "0.7"))


def _api_key() -> str:
    return os.getenv("SHITERU_KEY", "").strip()


def get_provider() -> str:
    return "shiteru"


def set_provider(name: str) -> bool:
    return (name or "").strip().lower() == "shiteru"


def shiteru_ready() -> bool:
    return bool(_api_key())


def _usage_from(data: dict) -> dict:
    usage = (data or {}).get("usage") or {}
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return {
        "prompt_tokens": max(0, prompt),
        "completion_tokens": max(0, completion),
        "total_tokens": max(0, total),
    }


async def chat_stream(messages: list[dict], usage_out: dict | None = None, sources_out: list | None = None):
    key = _api_key()
    if not key:
        raise RuntimeError("SHITERU_KEY is not set in the environment.")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    got_any = False
    async for piece in web_tools.stream_completion(
        messages,
        endpoint=ASK_ENDPOINT,
        headers=headers,
        model=ASK_MODEL,
        temperature=ASK_TEMPERATURE,
        max_tokens=ASK_MAX_TOKENS,
        timeout=ASK_TIMEOUT,
        usage_out=usage_out,
        sources_out=sources_out,
    ):
        got_any = True
        yield piece

    if not got_any:
        raise RuntimeError("Shiteru returned an empty stream.")
    log.info("Ask stream success | provider=shiteru model=%s usage=%s", ASK_MODEL, usage_out or {})


async def chat(messages: list[dict], usage_out: dict | None = None, sources_out: list | None = None) -> str:
    parts: list[str] = []
    async for piece in chat_stream(messages, usage_out, sources_out=sources_out):
        if piece is web_tools.RESET:
            parts = []
            continue
        parts.append(piece)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("AI provider returned an empty answer.")
    return text


def model_label() -> str:
    return ASK_MODEL
