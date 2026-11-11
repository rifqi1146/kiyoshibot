from __future__ import annotations

import json
import logging
import os
import aiohttp

from utils.http import get_http_session

log = logging.getLogger(__name__)

ASK_API_BASE = os.getenv("ASK_API_BASE", os.getenv("CACA_API_BASE", "https://shiteru.id/v1")).strip().rstrip("/")
ASK_ENDPOINT = f"{ASK_API_BASE}/chat/completions"
ASK_MODEL = os.getenv("ASK_MODEL", os.getenv("CACA_MODEL", "deepseek-v4.1-flash")).strip()
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


async def _session() -> aiohttp.ClientSession:
    return await get_http_session()


async def chat_stream(messages: list[dict], usage_out: dict | None = None):
    key = _api_key()
    if not key:
        raise RuntimeError("SHITERU_KEY is not set in the environment.")

    session = await _session()
    payload = {
        "model": ASK_MODEL,
        "messages": messages,
        "temperature": ASK_TEMPERATURE,
        "max_completion_tokens": ASK_MAX_TOKENS,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    async with session.post(
        ASK_ENDPOINT,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=ASK_TIMEOUT),
    ) as r:
        if r.status != 200:
            err_msg = ""
            try:
                err_data = await r.json(content_type=None)
                if isinstance(err_data, dict):
                    err_msg = (err_data.get("error") or {}).get("message") or str(err_data)
            except Exception:
                err_msg = await r.text()
            raise RuntimeError(f"Shiteru HTTP {r.status}: {err_msg or 'Request error'}")

        buffer = b""
        got_any = False
        async for chunk_bytes in r.content.iter_any():
            buffer += chunk_bytes
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                try:
                    obj = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                if usage_out is not None and obj.get("usage"):
                    parsed = _usage_from(obj)
                    if parsed["total_tokens"] > 0:
                        usage_out.update(parsed)
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    got_any = True
                    yield content
        if not got_any:
            raise RuntimeError("Shiteru returned an empty stream.")
        log.info("Ask stream success | provider=shiteru model=%s usage=%s", ASK_MODEL, usage_out or {})


async def chat(messages: list[dict], usage_out: dict | None = None) -> str:
    parts: list[str] = []
    async for piece in chat_stream(messages, usage_out):
        parts.append(piece)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("AI provider returned an empty answer.")
    return text


def model_label() -> str:
    return ASK_MODEL
