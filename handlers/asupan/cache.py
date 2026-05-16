import asyncio
from utils.config import LOG_CHAT_ID
from .constants import ASUPAN_PREFETCH_SIZE, log
from .fetcher import fetch_asupan_tikwm
from . import state


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
            try:
                url = await fetch_asupan_tikwm(kw)
                msg = await bot.send_video(
                    chat_id=LOG_CHAT_ID,
                    video=url,
                    disable_notification=True,
                )
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
            try:
                url = await fetch_asupan_tikwm(None)

                msg = await bot.send_video(
                    chat_id=LOG_CHAT_ID,
                    video=url,
                    disable_notification=True,
                )
                state.ASUPAN_CACHE.append({"file_id": msg.video.file_id})
                await msg.delete()
                await asyncio.sleep(1.1)
            except Exception as e:
                log.warning(f"[ASUPAN PREFETCH] {e}")
                break
    finally:
        state.ASUPAN_FETCHING = False

async def get_asupan_fast(bot, keyword: str | None = None):
    if keyword is None:
        if state.ASUPAN_CACHE:
            return state.ASUPAN_CACHE.pop(0)
        url = await fetch_asupan_tikwm(None)
        msg = await bot.send_video(
            chat_id=LOG_CHAT_ID,
            video=url,
            disable_notification=True,
        )
        file_id = msg.video.file_id
        await msg.delete()
        return {"file_id": file_id}
        
    kw = keyword.lower().strip()
    cache = state.ASUPAN_KEYWORD_CACHE.get(kw)
    if cache:
        return cache.pop(0)
        
    url = await fetch_asupan_tikwm(kw)
    msg = await bot.send_video(
        chat_id=LOG_CHAT_ID,
        video=url,
        disable_notification=True,
    )
    file_id = msg.video.file_id
    await msg.delete()
    return {"file_id": file_id}
