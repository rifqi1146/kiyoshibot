from __future__ import annotations

import os
import time
import asyncio
import logging

from . import dtksearch

log = logging.getLogger(__name__)

ENABLED = os.getenv("ASUPAN_TIKTOKAPI", "1").strip().lower() not in ("0", "false", "no")
VENDOR_ENABLED = os.getenv("ASUPAN_TIKTOKAPI_VENDOR", "0").strip().lower() not in ("0", "false", "no")

_INTERVAL = float(os.getenv("ASUPAN_TIKTOKAPI_INTERVAL", "1.5"))

_GATE = asyncio.Lock()
_LAST_CALL = 0.0
_AVAILABLE: bool | None = None


def configured() -> bool:
    return ENABLED and (dtksearch.configured() or VENDOR_ENABLED)


def _map_vendor(payload: dict) -> list[dict]:
    out = []
    for v in payload.get("videos") or []:
        vid = v.get("id")
        if not vid:
            continue
        author = v.get("author") or {}
        out.append(
            {
                "id": str(vid),
                "link": v.get("link") or "",
                "unique_id": author.get("username") or "_",
            }
        )
    return out


def _vendor_search_sync(keyword: str) -> list[dict]:
    from .tiktokapi import search_videos as _vendor_search

    return _map_vendor(_vendor_search(keyword))


async def _gated(factory) -> list[dict]:
    global _LAST_CALL
    async with _GATE:
        wait = _INTERVAL - (time.monotonic() - _LAST_CALL)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CALL = time.monotonic()
        return await factory()


async def search_videos(keyword: str) -> list[dict]:
    if not configured():
        return []

    if dtksearch.configured():
        try:
            items = await _gated(lambda: dtksearch.search_videos(keyword))
            if items:
                return items
            log.info("[ASUPAN] dtk search empty | keyword=%r", keyword)
        except Exception as e:
            log.warning("[ASUPAN] dtk search failed | keyword=%r err=%r", keyword, e)

    if not VENDOR_ENABLED:
        return []

    try:
        items = await _gated(lambda: asyncio.to_thread(_vendor_search_sync, keyword))
        if items:
            log.info("[ASUPAN] vendor tiktokapi used | keyword=%r", keyword)
            return items
        log.info("[ASUPAN] vendor tiktokapi empty | keyword=%r", keyword)
    except Exception as e:
        log.warning("[ASUPAN] vendor tiktokapi failed | keyword=%r err=%r", keyword, e)
    return []


async def warmup() -> bool:
    global _AVAILABLE
    if not configured():
        return False
    if dtksearch.configured():
        _AVAILABLE = await dtksearch.warmup()
        return bool(_AVAILABLE)
    _AVAILABLE = True
    return True


async def close() -> None:
    await dtksearch.close()
