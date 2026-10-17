from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from typing import Any, Iterable

import aiohttp

from . import codebuddy_store as store

logger = logging.getLogger(__name__)

BASE_HOST = "https://www.workbuddy.ai"

CHAT_URL = os.getenv(
    "CODEBUDDY_CHAT_URL", f"{BASE_HOST}/v2/chat/completions"
).strip()
REFRESH_URL = os.getenv(
    "CODEBUDDY_REFRESH_URL", "https://www.codebuddy.ai/v2/plugin/auth/token/refresh"
).strip()
DEFAULT_MODEL = os.getenv("CODEBUDDY_MODEL", "deepseek-v4.1-flash").strip()

CLIENT_VERSION = "5.8.4"
CLI_VERSION = "2.161.0"
PLATFORM_NAME = "WorkBuddy"
PRODUCT_NAME = "workbuddy-ai"

CHAT_USER_AGENT = f"{PLATFORM_NAME}/{CLIENT_VERSION} {PRODUCT_NAME}/{CLIENT_VERSION} CLI/{CLI_VERSION}"
REFRESH_USER_AGENT = f"IDE/2.63.2 CodeBuddy/2.63.2"

REFRESH_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": REFRESH_USER_AGENT,
    "X-Requested-With": "XMLHttpRequest",
    "X-Domain": "www.codebuddy.ai",
    "X-Auth-Refresh-Source": "plugin",
    "X-Product": "SaaS",
}

CHAT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "User-Agent": CHAT_USER_AGENT,
    "X-Product": "SaaS",
    "X-IDE-Type": PLATFORM_NAME,
    "X-IDE-Name": PLATFORM_NAME,
    "X-IDE-Version": CLIENT_VERSION,
    "X-Private-Data": "false",
    "X-Agent-Intent": "chat",
    "X-Agent-Purpose": "chat",
    "X-Agent-Type": "cli",
    "X-Domain": "www.workbuddy.ai",
    "x-requested-with": "XMLHttpRequest",
    "x-codebuddy-request": "1",
}

_EPOCH_UUID_MS = 1_588_598_400_000


class CodeBuddyError(RuntimeError):
    pass


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def configured() -> bool:
    return len(store.all_accounts()) > 0


def account_count() -> int:
    return len(store.all_accounts())


def account_summaries() -> list[dict]:
    return store.summary()


def current_account_index() -> int:
    accounts = store.all_accounts()
    if not accounts:
        return 0
    return store.peek_cursor() % len(accounts)


def _model() -> str:
    return _env("CODEBUDDY_MODEL") or DEFAULT_MODEL


def _uuid7() -> str:
    ms = int(time.time() * 1000)
    rand = secrets.token_bytes(10)
    value = ((ms - _EPOCH_UUID_MS) & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (rand[0] & 0x0F) << 72
    value |= int.from_bytes(rand[1:7], "big") << 28
    value |= 0x2 << 62
    value |= int.from_bytes(rand[7:10], "big")
    return str(uuid.UUID(int=value))


def _hex32() -> str:
    return secrets.token_hex(16)


def _conversation_headers() -> dict[str, str]:
    request_id = _hex32()
    message_id = _hex32()
    trace_id = _hex32()
    span_id = _hex32()[:16]
    return {
        "X-Conversation-ID": _uuid7(),
        "X-Conversation-Request-ID": request_id,
        "X-Root-Request-ID": request_id,
        "X-Conversation-Message-ID": message_id,
        "X-Request-ID": message_id,
        "X-Trace-ID": trace_id,
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "b3": f"{trace_id}-{span_id}-1",
        "X-B3-TraceId": trace_id,
        "X-B3-SpanId": span_id,
        "X-B3-Sampled": "1",
    }


def _request_headers(token: str) -> dict[str, str]:
    headers = dict(CHAT_HEADERS)
    headers.update(_conversation_headers())
    headers["Authorization"] = f"Bearer {token}"
    return headers


_TOKEN_LOCK = asyncio.Lock()
_CACHE: dict[str, dict[str, Any]] = {}


def reset_token_cache() -> None:
    _CACHE.clear()


async def _refresh_access_token(
    session: aiohttp.ClientSession, refresh_token: str, timeout: int
) -> dict:
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


async def _bearer_token_for(
    session: aiohttp.ClientSession, account: dict, timeout: int
) -> str:
    key = str(account.get("key") or "")
    static = str(account.get("access_token") or "")
    refresh = str(account.get("refresh_token") or "")

    if refresh:
        async with _TOKEN_LOCK:
            now = time.time()
            cached = _CACHE.get(key) or {}
            token = cached.get("access_token")
            exp = float(cached.get("expires_at") or 0)
            if token and now < exp - 60:
                return token

            tokens = await _refresh_access_token(session, refresh, timeout)
            new_at = tokens["access_token"]
            new_rt = tokens["refresh_token"]
            exp_at = now + float(tokens["expires_in"])
            _CACHE[key] = {
                "access_token": new_at,
                "refresh_token": new_rt,
                "expires_at": exp_at,
            }
            store.update_tokens(key, refresh_token=new_rt, access_token=new_at)
            logger.info("CodeBuddy token di-refresh | account=%s", account.get("label"))
            return new_at

    if static:
        return static
    raise CodeBuddyError(f"akun {account.get('label')} tidak memiliki token")


def _normalise_messages(messages: list[dict]) -> list[dict]:
    has_system = any(
        isinstance(m, dict) and m.get("role") in ("system", "developer")
        for m in messages
    )
    out: list[dict] = []
    if not has_system:
        out.append({"role": "system", "content": " "})

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


def _candidate_accounts() -> list[dict]:
    accounts = store.all_accounts()
    if not accounts:
        raise CodeBuddyError("Belum ada akun CodeBuddy tersimpan. Tambah via /cbacc add.")
    start = store.peek_cursor() % len(accounts)
    return accounts[start:] + accounts[:start]


async def bootstrap_account(account: dict) -> dict:
    key = str(account.get("key") or "")
    refresh = str(account.get("refresh_token") or "")
    if not refresh:
        return account

    timeout = 30
    async with aiohttp.ClientSession() as session:
        tokens = await _refresh_access_token(session, refresh, timeout)

    new_at = tokens["access_token"]
    new_rt = tokens["refresh_token"]
    store.update_tokens(key, refresh_token=new_rt, access_token=new_at)
    for acc in store.all_accounts():
        if acc.get("key") == key:
            return acc
    return account


def _classify_error(status: int, text: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return ""
    code = payload.get("code")
    msg = str(payload.get("msg") or "")
    if code == 11128:
        if "conversation" in msg.lower():
            return "channel_verification"
        return "system_prompt"
    if code == 11101:
        return "content_blocks"
    if code in (401, 403) or str(code).startswith("11-"):
        return "auth"
    return ""


async def _iter_sse_account(
    session: aiohttp.ClientSession,
    account: dict,
    body: dict,
    timeout: int,
    usage_out: dict | None = None,
) -> tuple[Iterable[str], dict]:
    token = await _bearer_token_for(session, account, timeout)
    headers = _request_headers(token)

    r = await session.post(
        CHAT_URL,
        headers=headers,
        json=body,
        timeout=aiohttp.ClientTimeout(total=timeout),
    )
    if r.status != 200:
        raw = await r.text()
        r.close()
        kind = _classify_error(r.status, raw)
        hint = ""
        if kind == "channel_verification":
            hint = " [header sesi ditolak gateway]"
        elif kind == "system_prompt":
            hint = " [pesan pertama harus system]"
        elif kind == "content_blocks":
            hint = " [content user harus typed blocks]"
        raise CodeBuddyError(
            f"chat HTTP {r.status} (account={account.get('label')}){hint}: {raw[:300]}"
        )

    async def _gen():
        try:
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
        finally:
            r.close()

    return _gen(), account


async def chat_stream(
    session: aiohttp.ClientSession,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.9,
    top_p: float = 0.95,
    timeout: int = 60,
    usage_out: dict | None = None,
    meta_out: dict | None = None,
):
    body = _build_body(messages, max_tokens, temperature, top_p)
    accounts = _candidate_accounts()
    last_err: Exception | None = None

    for idx, acc in enumerate(accounts):
        emitted = False
        try:
            generator, used_acc = await _iter_sse_account(
                session, acc, body, timeout, usage_out
            )
            async for piece in generator:
                emitted = True
                yield piece
            store.rotate_cursor(len(accounts))
            if meta_out is not None:
                meta_out["account"] = acc.get("label") or acc.get("email") or acc.get("key") or "unknown"
                meta_out["account_key"] = acc.get("key") or ""
                meta_out["attempts"] = idx + 1
            return
        except Exception as e:
            last_err = e
            if emitted:
                logger.warning(
                    "CodeBuddy stream putus setelah output | account=%s error=%r",
                    acc.get("label"), e,
                )
                raise
            logger.warning(
                "CodeBuddy akun gagal | account=%s error=%r (mencoba akun berikutnya: %d/%d)",
                acc.get("label"), e, idx + 1, len(accounts)
            )

    raise CodeBuddyError(f"Semua akun CodeBuddy ({len(accounts)}) gagal. Terakhir: {last_err}")


async def chat(
    session: aiohttp.ClientSession,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.9,
    top_p: float = 0.95,
    timeout: int = 60,
    meta_out: dict | None = None,
) -> tuple[str, dict]:
    usage: dict = {}
    parts: list[str] = []
    async for piece in chat_stream(
        session, messages, max_tokens, temperature, top_p, timeout, usage, meta_out=meta_out
    ):
        parts.append(piece)
    text = "".join(parts)
    if not text:
        raise CodeBuddyError("stream CodeBuddy kosong")
    return text, usage
