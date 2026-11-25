import asyncio
import io
from utils.config import LOG_CHAT_ID
from .constants import (
    ASUPAN_PREFETCH_SIZE,
    ASUPAN_WARM_CONCURRENCY,
    ASUPAN_CACHE_MAX_BYTES,
    ASUPAN_KEYWORD_CACHE_MAX_KEYS,
    log,
)
from .fetcher import fetch_asupan_media, warm_default_pool
from . import tiktoksource
from . import state

STARTUP_SEND_ATTEMPTS = 3


def build_video_payload(data: dict) -> tuple[object, dict]:
    if data.get("file_id"):
        return data["file_id"], {}
    if data.get("bytes"):
        kw: dict = {"filename": "asupan.mp4", "supports_streaming": True}
        for key in ("width", "height", "duration"):
            val = int(data.get(key) or 0)
            if val > 0:
                kw[key] = val
        return io.BytesIO(data["bytes"]), kw
    return data.get("url"), {"supports_streaming": True}


def _item_bytes(item: dict) -> int:
    raw = item.get("bytes")
    return len(raw) if isinstance(raw, (bytes, bytearray)) else 0


def _total_cached_bytes() -> int:
    total = sum(_item_bytes(it) for it in state.ASUPAN_CACHE)
    for items in state.ASUPAN_KEYWORD_CACHE.values():
        total += sum(_item_bytes(it) for it in items)
    return total


def _prune_keyword_caches() -> None:
    fetching = getattr(state, "ASUPAN_KEYWORD_FETCHING", set())
    while len(state.ASUPAN_KEYWORD_CACHE) > ASUPAN_KEYWORD_CACHE_MAX_KEYS:
        victim = next((k for k in state.ASUPAN_KEYWORD_CACHE if k not in fetching), None)
        if victim is None:
            break
        state.ASUPAN_KEYWORD_CACHE.pop(victim, None)


def _ensure_cache_budget(needed: int, exclude_kw: str | None = None) -> bool:
    if _total_cached_bytes() + needed <= ASUPAN_CACHE_MAX_BYTES:
        return True
    fetching = getattr(state, "ASUPAN_KEYWORD_FETCHING", set())
    for k in list(state.ASUPAN_KEYWORD_CACHE):
        if k == exclude_kw or k in fetching:
            continue
        items = state.ASUPAN_KEYWORD_CACHE.get(k)
        while items and _total_cached_bytes() + needed > ASUPAN_CACHE_MAX_BYTES:
            items.pop(0)
        if not items:
            state.ASUPAN_KEYWORD_CACHE.pop(k, None)
        if _total_cached_bytes() + needed <= ASUPAN_CACHE_MAX_BYTES:
            return True
    return _total_cached_bytes() + needed <= ASUPAN_CACHE_MAX_BYTES


async def warm_keyword_asupan_cache(bot=None, keyword: str = ""):
    del bot
    kw = (keyword or "").lower().strip()
    if not kw:
        return

    if not hasattr(state, "ASUPAN_KEYWORD_FETCHING"):
        state.ASUPAN_KEYWORD_FETCHING = set()

    if kw in state.ASUPAN_KEYWORD_FETCHING:
        return

    cache = state.ASUPAN_KEYWORD_CACHE.setdefault(kw, [])
    _prune_keyword_caches()
    if len(cache) >= ASUPAN_PREFETCH_SIZE:
        return

    state.ASUPAN_KEYWORD_FETCHING.add(kw)
    try:
        while len(cache) < ASUPAN_PREFETCH_SIZE:
            need = min(ASUPAN_WARM_CONCURRENCY, ASUPAN_PREFETCH_SIZE - len(cache))
            results = await asyncio.gather(
                *(fetch_asupan_media(kw) for _ in range(need)),
                return_exceptions=True,
            )
            added = 0
            for res in results:
                if isinstance(res, Exception):
                    log.warning("[ASUPAN KEYWORD PREFETCH] %s: %s", kw, res)
                    continue
                if not isinstance(res, dict):
                    continue
                if len(cache) >= ASUPAN_PREFETCH_SIZE:
                    break
                if not _ensure_cache_budget(_item_bytes(res), exclude_kw=kw):
                    break
                cache.append(res)
                added += 1
            if added == 0:
                break
        if cache:
            log.info("[ASUPAN] cache warmed | keyword=%r count=%d", kw, len(cache))
    finally:
        state.ASUPAN_KEYWORD_FETCHING.discard(kw)


async def warm_asupan_cache(bot=None):
    del bot
    if getattr(state, "ASUPAN_FETCHING", False):
        return
    state.ASUPAN_FETCHING = True
    try:
        while len(state.ASUPAN_CACHE) < ASUPAN_PREFETCH_SIZE:
            need = min(ASUPAN_WARM_CONCURRENCY, ASUPAN_PREFETCH_SIZE - len(state.ASUPAN_CACHE))
            results = await asyncio.gather(
                *(fetch_asupan_media(None) for _ in range(need)),
                return_exceptions=True,
            )
            added = 0
            for res in results:
                if isinstance(res, Exception):
                    log.warning("[ASUPAN PREFETCH] %s", res)
                    continue
                if not isinstance(res, dict):
                    continue
                if len(state.ASUPAN_CACHE) >= ASUPAN_PREFETCH_SIZE:
                    break
                if not _ensure_cache_budget(_item_bytes(res)):
                    break
                state.ASUPAN_CACHE.append(res)
                added += 1
            if added == 0:
                break
        if state.ASUPAN_CACHE:
            log.info("[ASUPAN] cache warmed | count=%d", len(state.ASUPAN_CACHE))
    finally:
        state.ASUPAN_FETCHING = False

async def warm_startup_asupan(bot):
    await tiktoksource.warmup()
    await warm_default_pool()
    if LOG_CHAT_ID:
        last_err = None
        for attempt in range(1, STARTUP_SEND_ATTEMPTS + 1):
            try:
                data = await fetch_asupan_media(None)
                media_arg, media_kw = build_video_payload(data)
                msg = await bot.send_video(
                    chat_id=LOG_CHAT_ID,
                    video=media_arg,
                    disable_notification=True,
                    **media_kw,
                )
                file_id = msg.video.file_id if msg.video else None
                try:
                    await msg.delete()
                except Exception:
                    pass
                if file_id:
                    state.ASUPAN_CACHE.append(
                        {
                            "file_id": file_id,
                            "source": data.get("source", "scraper"),
                            "keyword": data.get("keyword"),
                            "id": data.get("id"),
                        }
                    )
                last_err = None
                break
            except Exception as e:
                last_err = e
                log.warning("[ASUPAN STARTUP] Send attempt %d/%d failed: %r", attempt, STARTUP_SEND_ATTEMPTS, e)
        if last_err is not None:
            raise last_err


async def get_asupan_fast(bot=None, keyword: str | None = None):
    del bot
    if keyword is None:
        if state.ASUPAN_CACHE:
            return state.ASUPAN_CACHE.pop(0)
        return await fetch_asupan_media(None)

    kw = keyword.lower().strip()
    cache = state.ASUPAN_KEYWORD_CACHE.get(kw)
    if cache:
        return cache.pop(0)
    return await fetch_asupan_media(kw)
