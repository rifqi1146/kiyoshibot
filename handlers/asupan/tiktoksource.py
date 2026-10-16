
from __future__ import annotations

import os
import time
import asyncio
import logging

log = logging.getLogger(__name__)

ENABLED = os.getenv("ASUPAN_TIKTOKAPI", "1").strip().lower() not in ("0", "false", "no")

_INTERVAL = float(os.getenv("ASUPAN_TIKTOKAPI_INTERVAL", "2.0"))

_GATE = asyncio.Lock()
_LAST_CALL = 0.0
_AVAILABLE: bool | None = None


def configured() -> bool:
    return ENABLED


def _search_sync(keyword: str) -> list[dict]:
    from .tiktokapi import search_videos as _vendor_search

    payload = _vendor_search(keyword)
    out = []
    for v in (payload.get("videos") or []):
        vid = v.get("id")
        if not vid:
            continue
        author = v.get("author") or {}
        out.append({
            "id": vid,
            "link": v.get("link") or "",
            "unique_id": author.get("username") or "_",
        })
    return out


async def _gated_search(keyword: str) -> list[dict]:
    global _LAST_CALL
    async with _GATE:
        wait = _INTERVAL - (time.monotonic() - _LAST_CALL)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CALL = time.monotonic()
        return await asyncio.to_thread(_search_sync, keyword)


async def search_videos(keyword: str) -> list[dict]:
    if not configured():
        return []
    try:
        return await _gated_search(keyword)
    except Exception as e:
        log.warning("[ASUPAN] tiktokapi search gagal | keyword=%r err=%r", keyword, e)
        return []


async def warmup() -> bool:
    global _AVAILABLE
    if not configured():
        return False
    try:
        from . import tiktokapi
    except Exception as e:
        log.warning("[ASUPAN] tiktokapi tidak bisa diimpor | err=%r", e)
        _AVAILABLE = False
        return False
    _AVAILABLE = True
    return True
