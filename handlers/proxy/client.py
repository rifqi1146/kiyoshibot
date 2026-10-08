"""Bot-side client untuk proxy OpenCode (`handlers/proxy/opencode_v1.py`).

Proxy berjalan sebagai HTTP server lokal (default 127.0.0.1:20130) dengan
endpoint OpenAI-compatible:
  - GET  /v1/models
  - POST /v1/chat/completions
Tanpa API key (Bearer public).

Modul ini menyediakan:
  - proxy_models()      -> list[str] id model gratis dari upstream
  - proxy_chat()        -> non-stream, balikin teks jawaban
  - proxy_chat_stream() -> async generator delta teks
  - get_model/set_model -> model aktif yang dipakai /ask (persist di JSON)
"""
import json
import logging
import os

import aiohttp

from utils.http import get_http_session

log = logging.getLogger(__name__)

PROXY_BASE = os.getenv("OPENCODE_PROXY_BASE", "http://127.0.0.1:20130").rstrip("/")
MODEL_FILE = os.path.join("data", "opencode_model.json")
SETTINGS_FILE = os.path.join("data", "opencode_settings.json")
DEFAULT_MODEL = os.getenv("OPENCODE_MODEL", "").strip()
# Thinking reasoning model reasoning: biaya token + latency. Default off
# supaya respons cepat; owner bisa nyalakan via /thinking on.
_DEFAULT_THINKING = os.getenv("OPENCODE_THINKING", "off").strip().lower() not in (
    "", "0", "false", "no", "off", "disabled",
)


def _read_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("Failed to read proxy settings | err=%r", e)
        return {}


def _write_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, SETTINGS_FILE)


def is_thinking_enabled() -> bool:
    """Apakah reasoning/thinking model aktif. Default OFF (respons cepat)."""
    val = _read_settings().get("thinking")
    if isinstance(val, bool):
        return val
    return _DEFAULT_THINKING


def set_thinking(enabled: bool) -> None:
    data = _read_settings()
    data["thinking"] = bool(enabled)
    _write_settings(data)


_ENGINES = ("firecrawl", "jina")
_DEPTHS = ("fast", "content")


def get_search_engine() -> str:
    """Engine search aktif: 'firecrawl' (default) atau 'jina'."""
    val = str(_read_settings().get("search_engine") or "").strip().lower()
    if val in _ENGINES:
        return val
    env = str(os.getenv("SEARCH_ENGINE", "firecrawl")).strip().lower()
    return env if env in _ENGINES else "firecrawl"


def set_search_engine(engine: str) -> None:
    engine = (engine or "").strip().lower()
    if engine not in _ENGINES:
        raise ValueError(f"unknown engine: {engine}")
    data = _read_settings()
    data["search_engine"] = engine
    _write_settings(data)


def get_search_depth() -> str:
    """Kedalaman Firecrawl: 'fast' (SERP polos, default) atau 'content'."""
    val = str(_read_settings().get("search_depth") or "").strip().lower()
    if val in _DEPTHS:
        return val
    env = str(os.getenv("SEARCH_DEPTH", "fast")).strip().lower()
    return env if env in _DEPTHS else "fast"


def set_search_depth(depth: str) -> None:
    depth = (depth or "").strip().lower()
    if depth not in _DEPTHS:
        raise ValueError(f"unknown depth: {depth}")
    data = _read_settings()
    data["search_depth"] = depth
    _write_settings(data)


def get_model() -> str:
    """Model aktif (dari file) atau default env, atau string kosong."""
    try:
        with open(MODEL_FILE, "r", encoding="utf-8") as f:
            saved = (json.load(f).get("model") or "").strip()
            if saved:
                return saved
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Failed to read proxy model | err=%r", e)
    return DEFAULT_MODEL


def set_model(model: str) -> None:
    model = (model or "").strip()
    if not model:
        raise ValueError("model kosong")
    os.makedirs(os.path.dirname(MODEL_FILE) or ".", exist_ok=True)
    tmp = MODEL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"model": model}, f)
    os.replace(tmp, MODEL_FILE)


async def proxy_models(timeout: int = 30) -> list[str]:
    session = await get_http_session()
    async with session.get(
        f"{PROXY_BASE}/v1/models",
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        if resp.status != 200:
            body = (await resp.text())[:300]
            raise RuntimeError(f"proxy /v1/models {resp.status}: {body}")
        data = await resp.json()
    return [m.get("id") for m in (data.get("data") or []) if m.get("id")]


async def proxy_health(timeout: int = 5) -> bool:
    session = await get_http_session()
    try:
        async with session.get(
            f"{PROXY_BASE}/health",
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _payload(messages: list[dict], model: str, stream: bool, tools: list[dict] | None = None) -> dict:
    body: dict = {"model": model, "messages": messages, "stream": stream}
    if tools:
        body["tools"] = tools
        body.setdefault("tool_choice", "auto")
    # Thinking: ketika OFF, kirim thinking=disabled (terverifikasi menghapus
    # reasoning di upstream). Provider yang menolak field ini akan gagal ->
    # caller melakukan retry tanpa field (lihat _post_chat).
    if not is_thinking_enabled():
        body["thinking"] = {"type": "disabled"}
    return body


async def _resolve_model(model: str | None) -> str:
    use_model = (model or get_model()).strip()
    if use_model:
        return use_model
    models = await proxy_models()
    if not models:
        raise RuntimeError("Proxy returned no models.")
    return models[0]


async def proxy_chat_raw(
    messages: list[dict],
    model: str | None = None,
    timeout: int = 180,
    tools: list[dict] | None = None,
) -> tuple[dict, str]:
    """Non-stream, kembalikan (message mentah, model dipakai).

    message mentah berbentuk OpenAI chat: {"role","content","tool_calls",...}.
    Dipakai jalur agentic (ask.py) yang butuh tool_calls.
    """
    use_model = await _resolve_model(model)
    payload = _payload(messages, use_model, False, tools)
    session = await get_http_session()
    async with session.post(
        f"{PROXY_BASE}/v1/chat/completions",
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        data = await resp.json(content_type=None)
        # Jika upstream menolak parameter thinking (mis. model non-reasoning),
        # retry sekali tanpa field itu.
        if resp.status == 400 and "thinking" in payload:
            payload.pop("thinking", None)
            async with session.post(
                f"{PROXY_BASE}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as retry_resp:
                data = await retry_resp.json(content_type=None)
                resp = retry_resp
        if resp.status != 200:
            err = (data or {}).get("error") if isinstance(data, dict) else None
            msg = (err or {}).get("message") if isinstance(err, dict) else str(err or data)
            raise RuntimeError(f"proxy {resp.status}: {msg}")

    try:
        message = data["choices"][0]["message"]
        assert isinstance(message, dict)
        return message, use_model
    except (KeyError, IndexError, TypeError, AssertionError, AttributeError):
        raise RuntimeError(f"Unexpected proxy response: {str(data)[:300]}")


async def proxy_chat(messages: list[dict], model: str | None = None, timeout: int = 180) -> str:
    """Non-stream chat completion. Balikin teks jawaban model."""
    message, _ = await proxy_chat_raw(messages, model=model, timeout=timeout)
    return (message.get("content") or "").strip()


async def proxy_chat_stream(messages: list[dict], model: str | None = None, timeout: int = 300):
    """Async generator delta teks dari SSE proxy. Raise RuntimeError saat error."""
    use_model = await _resolve_model(model)

    session = await get_http_session()
    payload = _payload(messages, use_model, True)
    async with session.post(
        f"{PROXY_BASE}/v1/chat/completions",
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        # Retry sekali tanpa parameter thinking kalau ditolak (model non-reasoning).
        if resp.status == 400 and "thinking" in payload:
            resp.close()
            payload.pop("thinking", None)
            resp = await session.post(
                f"{PROXY_BASE}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            )
        if resp.status != 200:
            body = (await resp.text())[:300]
            resp.release()
            raise RuntimeError(f"proxy {resp.status}: {body}")
        buffer = b""
        async for raw in resp.content.iter_any():
            buffer += raw
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == b"[DONE]":
                    continue
                try:
                    obj = json.loads(chunk.decode("utf-8"))
                except ValueError:
                    continue
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text
