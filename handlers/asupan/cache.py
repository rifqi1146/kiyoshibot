import asyncio
from utils.config import LOG_CHAT_ID
from .constants import ASUPAN_PREFETCH_SIZE, log
from .fetcher import fetch_asupan_tikwm
from . import state

# Berapa kali coba video lain kalau Telegram gagal mengambil URL-nya.
SEND_ATTEMPTS = 5


async def _send_next_asupan(bot, keyword):
    """Ambil 1 video asupan & kirim lewat URL (Telegram yang fetch).

    Kalau Telegram gagal mengambil URL-nya, video itu DIBUANG dan langsung coba
    video berikutnya dari pool keyword yang sama. Tidak ada download ke VPS.
    """
    if not LOG_CHAT_ID:
        raise RuntimeError("LOG_CHAT_ID kosong")

    last_err = None
    for attempt in range(1, SEND_ATTEMPTS + 1):
        url = await fetch_asupan_tikwm(keyword)
        try:
            return await bot.send_video(
                chat_id=LOG_CHAT_ID,
                video=url,
                disable_notification=True,
            )
        except Exception as e:
            last_err = e
            log.warning(
                "[ASUPAN] Send-by-URL failed (%r), skipping video & trying next (%d/%d)",
                e, attempt, SEND_ATTEMPTS,
            )

    raise last_err or RuntimeError("Failed to send asupan")


async def warm_keyword_asupan_cache(bot, keyword: str):
    kw = keyword.lower().strip()

    if not hasattr(state, "ASUPAN_KEYWORD_FETCHING"):
        state.ASUPAN_KEYWORD_FETCHING = set()

    if kw in state.ASUPAN_KEYWORD_FETCHING or not LOG_CHAT_ID:
        return

    cache = state.ASUPAN_KEYWORD_CACHE.setdefault(kw, [])
    if len(cache) >= ASUPAN_PREFETCH_SIZE:
        return

    state.ASUPAN_KEYWORD_FETCHING.add(kw)
    try:
        while len(cache) < ASUPAN_PREFETCH_SIZE:
            if getattr(state, "ASUPAN_ACTIVE_USERS", 0) > 0:
                log.info("[ASUPAN KEYWORD PREFETCH] skipping warm %r, active user present", kw)
                break
            try:
                msg = await _send_next_asupan(bot, kw)
                cache.append({"file_id": msg.video.file_id})
                await msg.delete()
                await asyncio.sleep(1.1)
            except Exception as e:
                log.warning(f"[ASUPAN KEYWORD PREFETCH] {kw}: {e}")
                break  # Kalo limit/error, break loop biar gak spamming API
    finally:
        state.ASUPAN_KEYWORD_FETCHING.discard(kw)


async def warm_asupan_cache(bot):
    if getattr(state, "ASUPAN_FETCHING", False) or not LOG_CHAT_ID:
        return
    state.ASUPAN_FETCHING = True
    try:
        while len(state.ASUPAN_CACHE) < ASUPAN_PREFETCH_SIZE:
            if getattr(state, "ASUPAN_ACTIVE_USERS", 0) > 0:
                log.info("[ASUPAN PREFETCH] skipping default warm, active user present")
                break
            try:
                msg = await _send_next_asupan(bot, None)
                state.ASUPAN_CACHE.append({"file_id": msg.video.file_id})
                await msg.delete()
                await asyncio.sleep(1.1)
            except Exception as e:
                log.warning(f"[ASUPAN PREFETCH] {e}")
                break
    finally:
        state.ASUPAN_FETCHING = False


async def get_asupan_fast(bot, keyword: str | None = None):
    state.ASUPAN_ACTIVE_USERS = getattr(state, "ASUPAN_ACTIVE_USERS", 0) + 1
    try:
        if keyword is None:
            if state.ASUPAN_CACHE:
                return state.ASUPAN_CACHE.pop(0)
            msg = await _send_next_asupan(bot, None)
            file_id = msg.video.file_id
            await msg.delete()
            return {"file_id": file_id}

        kw = keyword.lower().strip()
        cache = state.ASUPAN_KEYWORD_CACHE.get(kw)
        if cache:
            return cache.pop(0)

        msg = await _send_next_asupan(bot, kw)
        file_id = msg.video.file_id
        await msg.delete()
        return {"file_id": file_id}
    finally:
        state.ASUPAN_ACTIVE_USERS -= 1
