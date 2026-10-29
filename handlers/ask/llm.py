from __future__ import annotations

import json
import logging
import os

import aiohttp

from handlers.caca import codebuddy
from utils.http import get_http_session

log = logging.getLogger(__name__)

ASK_API_BASE = os.getenv("ASK_API_BASE", os.getenv("CACA_API_BASE", "https://shiteru.id/v1")).strip().rstrip("/")
ASK_ENDPOINT = f"{ASK_API_BASE}/chat/completions"
ASK_MODEL = os.getenv("ASK_MODEL", os.getenv("CACA_MODEL", "deepseek-v4-flash")).strip()
ASK_MAX_TOKENS = min(int(os.getenv("ASK_MAX_TOKENS", "4096")), 8192)
ASK_TIMEOUT = int(os.getenv("ASK_TIMEOUT", "120"))
ASK_TEMPERATURE = float(os.getenv("ASK_TEMPERATURE", "0.7"))

_PROVIDER_FILE = os.path.join("data", "ask_provider.txt")
_PROVIDER_CHOICES = ("shiteru", "codebuddy")
_DEFAULT_PROVIDER = os.getenv("ASK_PROVIDER", "shiteru").strip().lower()


def _api_key() -> str:
    return os.getenv("SHITERU_KEY", "").strip()


def get_provider() -> str:
    try:
        with open(_PROVIDER_FILE, "r", encoding="utf-8") as f:
            saved = (f.read() or "").strip().lower()
        if saved in _PROVIDER_CHOICES:
            return saved
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Failed to read ask provider | err=%r", e)
    return _DEFAULT_PROVIDER if _DEFAULT_PROVIDER in _PROVIDER_CHOICES else "shiteru"


def set_provider(name: str) -> bool:
    name = (name or "").strip().lower()
    if name not in _PROVIDER_CHOICES:
        return False
    try:
        os.makedirs(os.path.dirname(_PROVIDER_FILE) or ".", exist_ok=True)
        tmp = _PROVIDER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(name)
        os.replace(tmp, _PROVIDER_FILE)
    except Exception as e:
        log.warning("Failed to save ask provider | err=%r", e)
        return False
    return True


def codebuddy_ready() -> bool:
    try:
        return codebuddy.configured()
    except Exception:
        return False


def shiteru_ready() -> bool:
    return bool(_api_key())


def _codebuddy_model() -> str:
    return os.getenv("CODEBUDDY_MODEL", "deepseek-v4.1-flash").strip() or "deepseek-v4.1-flash"


def _usage_from(data: dict) -> dict:
    usage = (data or {}).get("usage") or {}
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


async def _shiteru_stream(messages: list[dict], usage_out: dict | None):
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


async def _codebuddy_stream(messages: list[dict], usage_out: dict | None, meta_out: dict | None):
    session = await _session()
    async for piece in codebuddy.chat_stream(
        session,
        messages,
        max_tokens=ASK_MAX_TOKENS,
        temperature=ASK_TEMPERATURE,
        top_p=0.95,
        timeout=ASK_TIMEOUT,
        usage_out=usage_out,
        meta_out=meta_out,
    ):
        yield piece


async def chat_stream(messages: list[dict], usage_out: dict | None = None):
    provider = get_provider()
    order = [provider]
    other = "shiteru" if provider == "codebuddy" else "codebuddy"
    order.append(other)

    last_err: Exception | None = None
    for idx, name in enumerate(order):
        if name == "codebuddy" and not codebuddy_ready():
            if idx == 0:
                log.warning("Ask provider codebuddy has no accounts, trying next")
            continue
        if name == "shiteru" and not shiteru_ready():
            if idx == 0:
                log.warning("Ask provider shiteru has no SHITERU_KEY, trying next")
            continue

        meta: dict = {}
        emitted = False
        try:
            gen = (
                _codebuddy_stream(messages, usage_out, meta)
                if name == "codebuddy"
                else _shiteru_stream(messages, usage_out)
            )
            async for piece in gen:
                emitted = True
                yield piece
            log.info(
                "Ask stream success | provider=%s account=%s usage=%s",
                name, meta.get("account", "-"), usage_out or {},
            )
            return
        except Exception as e:
            last_err = e
            if emitted:
                log.error("Ask stream broke mid-output | provider=%s err=%r", name, e)
                raise
            log.warning("Ask provider failed, trying next | provider=%s err=%r", name, e)

    raise RuntimeError(f"No usable AI provider for /ask. Last error: {last_err}")


async def chat(messages: list[dict], usage_out: dict | None = None) -> str:
    parts: list[str] = []
    async for piece in chat_stream(messages, usage_out):
        parts.append(piece)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("AI provider returned an empty answer.")
    return text


def model_label() -> str:
    if get_provider() == "codebuddy":
        return _codebuddy_model()
    return ASK_MODEL
