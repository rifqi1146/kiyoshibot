
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Iterable

import aiohttp

logger = logging.getLogger(__name__)

CHAT_URL = os.getenv(
    "CODEBUDDY_CHAT_URL", "https://www.codebuddy.ai/v2/chat/completions"
).strip()
REFRESH_URL = os.getenv(
    "CODEBUDDY_REFRESH_URL", "https://www.codebuddy.ai/v2/plugin/auth/token/refresh"
).strip()
DEFAULT_MODEL = os.getenv("CODEBUDDY_MODEL", "deepseek-v4.1-flash").strip()
DEFAULT_SYSTEM_PROMPT = "You are CodeBuddy Code."

REFRESH_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "IDE/2.63.2 CodeBuddy/2.63.2",
    "X-Requested-With": "XMLHttpRequest",
    "X-Domain": "www.codebuddy.ai",
    "X-Auth-Refresh-Source": "plugin",
    "X-Product": "SaaS",
}

CHAT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "User-Agent": "IDE/2.108.1 CodeBuddy/2.108.1",
    "X-Product": "SaaS",
    "X-IDE-Type": "IDE",
    "X-IDE-Name": "IDE",
    "x-requested-with": "XMLHttpRequest",
    "x-codebuddy-request": "1",
}


class CodeBuddyError(RuntimeError):
    pass


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def configured() -> bool:
    return bool(_env("CODEBUDDY_REFRESH_TOKEN") or _env("CODEBUDDY_ACCESS_TOKEN"))


def _model() -> str:
    return _env("CODEBUDDY_MODEL") or DEFAULT_MODEL


def reset_token_cache() -> None:
    _TOKEN_CACHE["access_token"] = None
    _TOKEN_CACHE["expires_at"] = 0.0
    _TOKEN_CACHE["refresh_token"] = None


_TOKEN_LOCK = asyncio.Lock()
_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "expires_at": 0.0, "refresh_token": None}


def _seed_refresh_token() -> str:
    return _TOKEN_CACHE.get("refresh_token") or _env("CODEBUDDY_REFRESH_TOKEN")


async def _refresh_access_token(session: aiohttp.ClientSession, refresh_token: str, timeout: int) -> dict:
    headers = dict(REFRESH_HEADERS)
    headers["X-Refresh-Token"] = refresh_token
    async with session.post(
        REFRESH_URL,
        headers=headers,
        data="{}",
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as r:
        text = await r.text()
        if r.status != 200:
            raise CodeBuddyError(f"refresh HTTP {r.status}: {text[:300]}")
        try:
            payload = json.loads(text)
        except Exception:
            raise CodeBuddyError(f"refresh balas bukan JSON: {text[:200]}")

    if payload.get("code") != 0 or not (payload.get("data") or {}).get("accessToken"):
        raise CodeBuddyError(
            f"refresh ditolak (code={payload.get('code')}): {payload.get('msg')}"
        )
    data = payload["data"]
    return {
        "access_token": data["accessToken"],
        "refresh_token": data.get("refreshToken") or refresh_token,
        "expires_in": data.get("expiresIn") or 3600,
    }


async def _bearer_token(session: aiohttp.ClientSession, timeout: int) -> str:
    static = _env("CODEBUDDY_ACCESS_TOKEN")
    refresh = _seed_refresh_token()

    if refresh:
        async with _TOKEN_LOCK:
            now = time.time()
            cached = _TOKEN_CACHE.get("access_token")
            if cached and now < (_TOKEN_CACHE.get("expires_at") or 0) - 60:
                return cached
            tokens = await _refresh_access_token(session, refresh, timeout)
            _TOKEN_CACHE["access_token"] = tokens["access_token"]
            _TOKEN_CACHE["refresh_token"] = tokens["refresh_token"]
            _TOKEN_CACHE["expires_at"] = now + float(tokens["expires_in"])
            logger.info("CodeBuddy token di-refresh | expires_in=%s", tokens["expires_in"])
            return tokens["access_token"]

    if static:
        return static
    raise CodeBuddyError("tidak ada CODEBUDDY_REFRESH_TOKEN / CODEBUDDY_ACCESS_TOKEN di environment")


def _normalise_messages(messages: list[dict]) -> list[dict]:
    has_system = any(
        isinstance(m, dict) and m.get("role") in ("system", "developer")
        for m in messages
    )
    out: list[dict] = []
    if not has_system:
        out.append({"role": "system", "content": DEFAULT_SYSTEM_PROMPT})

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, str):
            out.append({**msg, "content": [{"type": "text", "text": content}]})
        else:
            out.append(dict(msg))
    return out


def _build_body(messages: list[dict], max_tokens: int, temperature: float, top_p: float) -> dict:
    body: dict[str, Any] = {
        "model": _model(),
        "stream": True,
        "messages": _normalise_messages(messages),
        "reasoning_effort": "none",
    }
    if max_tokens and max_tokens > 0:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    return body


async def _iter_sse(
    session: aiohttp.ClientSession,
    body: dict,
    timeout: int,
    usage_out: dict | None = None,
) -> Iterable[str]:
    token = await _bearer_token(session, timeout)
    headers = dict(CHAT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"

    async with session.post(
        CHAT_URL,
        headers=headers,
        json=body,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as r:
        if r.status != 200:
            raw = await r.text()
            raise CodeBuddyError(f"chat HTTP {r.status}: {raw[:400]}")

        buffer = b""
        async for chunk in r.content.iter_any():
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == b"[DONE]":
                    continue
                try:
                    obj = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue

                if usage_out is not None and obj.get("usage"):
                    usage_out.update(obj["usage"])

                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    yield piece


async def chat_stream(
    session: aiohttp.ClientSession,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.9,
    top_p: float = 0.95,
    timeout: int = 60,
    usage_out: dict | None = None,
):
    body = _build_body(messages, max_tokens, temperature, top_p)
    async for piece in _iter_sse(session, body, timeout, usage_out):
        yield piece


async def chat(
    session: aiohttp.ClientSession,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.9,
    top_p: float = 0.95,
    timeout: int = 60,
) -> tuple[str, dict]:
    usage: dict = {}
    parts: list[str] = []
    async for piece in chat_stream(
        session, messages, max_tokens, temperature, top_p, timeout, usage
    ):
        parts.append(piece)
    text = "".join(parts)
    if not text:
        raise CodeBuddyError("stream CodeBuddy kosong")
    return text, usage
