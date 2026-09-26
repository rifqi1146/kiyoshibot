import os
import time
import json
import logging
import asyncio
from telegram import InlineKeyboardMarkup,InlineKeyboardButton,Update
from telegram.ext import ContextTypes
from telegram.error import NetworkError,TimedOut,RetryAfter,BadRequest,Forbidden
from utils.config import SUPPORT_CHANNEL_ID,SUPPORT_CHANNEL_LINK

log=logging.getLogger(__name__)

_JOIN_CACHE={}          # user_id -> {"ts": monotonic_seconds}
_JOIN_CACHE_TTL=300     # status positif dianggap valid selama 5 menit
_JOIN_CACHE_PATH=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"data","join_cache.json")
_JOIN_CACHE_SAVE_DEBOUNCE=5.0
_RETRY_DELAYS=(0.6,1.5)          # jeda sebelum retry ke-2 dan ke-3
# BadRequest/Forbidden adalah error permanen (bot tidak boleh retry & bukan
# indikasi user sudah join). Perlu dicek lebih dulu karena di PTB keduanya
# mewarisi NetworkError.
_PERMANENT_ERRORS=(BadRequest,Forbidden)
_last_save=0.0


def _load_cache_from_disk():
    """Muat cache status positif dari disk agar tidak hilang saat restart."""
    try:
        if not os.path.exists(_JOIN_CACHE_PATH):
            return
        with open(_JOIN_CACHE_PATH,"r",encoding="utf-8") as fh:
            raw=json.load(fh)
        if not isinstance(raw,dict):
            return
        now=time.monotonic()
        loaded=0
        for key,entry in raw.items():
            try:
                uid=int(key)
                if isinstance(entry,dict):
                    ts=float(entry.get("ts") or 0)
                else:
                    ts=float(entry or 0)
            except (TypeError,ValueError):
                continue
            # Simpan sebagai timestamp epoch pada disk; konversi ke uptime-relative.
            age=max(0.0,time.time()-ts)
            if age>=_JOIN_CACHE_TTL:
                continue
            _JOIN_CACHE[uid]={"ts":now-age}
            loaded+=1
        if loaded:
            log.info("[JOIN CACHE] Restored %s verified user(s) from disk",loaded)
    except Exception as e:
        log.warning("[JOIN CACHE] Failed to load cache | err=%r",e)


def _save_cache_to_disk():
    """Persist cache ke disk (debounced) supaya tahan restart."""
    global _last_save
    now=time.monotonic()
    if now-_last_save<_JOIN_CACHE_SAVE_DEBOUNCE:
        return
    _last_save=now
    try:
        os.makedirs(os.path.dirname(_JOIN_CACHE_PATH),exist_ok=True)
        payload={}
        epoch=time.time()
        uptime=time.monotonic()
        for uid,entry in _JOIN_CACHE.items():
            ts=float((entry or {}).get("ts") or 0)
            payload[str(uid)]={"ts":round(epoch-(uptime-ts),3)}
        tmp=f"{_JOIN_CACHE_PATH}.tmp"
        with open(tmp,"w",encoding="utf-8") as fh:
            json.dump(payload,fh)
        os.replace(tmp,_JOIN_CACHE_PATH)
    except Exception as e:
        log.debug("[JOIN CACHE] Failed to persist cache | err=%r",e)


_load_cache_from_disk()


def _cache_positive(user_id:int,now:float):
    _JOIN_CACHE[int(user_id)]={"ts":now}
    _save_cache_to_disk()


async def _fetch_member_status(user_id:int,context:ContextTypes.DEFAULT_TYPE):
    """Panggil get_chat_member dengan retry singkat untuk error jaringan.

    Return (status_str, error) — status_str None berarti gagal total (transien).
    """
    last_err=None
    for attempt,sleep_for in enumerate((0.0,)+_RETRY_DELAYS):
        if sleep_for:
            await asyncio.sleep(sleep_for)
        try:
            member=await context.bot.get_chat_member(SUPPORT_CHANNEL_ID,user_id)
            return getattr(member,"status",None),None
        except RetryAfter as e:
            last_err=e
            wait=max(int(getattr(e,"retry_after",2) or 2),1)
            log.warning("[JOIN RETRY] Flood control | user_id=%s attempt=%s wait=%ss",user_id,attempt+1,wait)
            await asyncio.sleep(wait+0.5)
        except _PERMANENT_ERRORS as e:
            # Error permanen dari Telegram (mis. USER_NOT_FOUND, CHAT_ADMIN_REQUIRED).
            # Langsung hentikan loop: jangan retry dan jangan fail-open.
            return None,e
        except (NetworkError,TimedOut) as e:
            last_err=e
            log.warning("[JOIN RETRY] Transient network error | user_id=%s attempt=%s err=%r",user_id,attempt+1,e)
        except Exception as e:
            return None,e
    return None,last_err


async def is_joined_support_channel(user_id:int,context:ContextTypes.DEFAULT_TYPE)->bool:
    if not SUPPORT_CHANNEL_ID:
        return True
    now=time.monotonic()
    key=int(user_id)
    cached=_JOIN_CACHE.get(key)
    if cached and now-float(cached.get("ts") or 0)<_JOIN_CACHE_TTL:
        return True
    if cached:
        _JOIN_CACHE.pop(key,None)

    status,err=await _fetch_member_status(key,context)
    if status is not None:
        if status in ("member","administrator","creator"):
            _cache_positive(key,now)
            return True
        _JOIN_CACHE.pop(key,None)
        return False

    # Gagal total. Klasifikasi menentukan apakah user boleh lolos.
    if isinstance(err,_PERMANENT_ERRORS):
        # Telegram sendiri bilang user/channel tak bisa diverifikasi -> tetap blokir.
        log.warning("[JOIN CHECK ERROR] Permanent failure | user_id=%s err=%r",user_id,err)
        return False
    if isinstance(err,(NetworkError,TimedOut,RetryAfter)):
        # Gagal karena error transien (network/timeout/flood): JANGAN blokir user.
        # Fail-open agar user yang sudah join tidak dihukum oleh glitch jaringan
        # bot ke Telegram. Update masuk membuktikan bot masih terhubung.
        log.warning("[JOIN CHECK ERROR] Transient failure, allowing access | user_id=%s err=%r",user_id,err)
        return True

    # Error tak terduga lainnya: perlakukan sama seperti perilaku lama (blokir).
    log.warning("[JOIN CHECK ERROR] user_id=%s err=%r",user_id,err)
    return False


def join_required_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Join Support Channel",url=SUPPORT_CHANNEL_LINK)]
    ])


async def require_join_or_block(update:Update,context:ContextTypes.DEFAULT_TYPE)->bool:
    if update.callback_query:
        user=update.callback_query.from_user
        reply_target=update.callback_query.message
    elif update.message:
        user=update.message.from_user
        reply_target=update.message
    else:
        return False
    if not user:
        return False
    joined=await is_joined_support_channel(user.id,context)
    if joined:
        return True
    if not SUPPORT_CHANNEL_LINK:
        log.warning("SUPPORT_CHANNEL_ID is set but SUPPORT_CHANNEL_LINK is missing. Allowing access.")
        return True
    text="<b>To use the feature</b>\nyou must join the support channel first."
    try:
        if update.callback_query:
            await update.callback_query.answer("Please join the support channel first",show_alert=True)
        await reply_target.reply_text(text,reply_markup=join_required_keyboard(),parse_mode="HTML")
    except Exception as e:
        log.warning("[JOIN BLOCK ERROR] user_id=%s err=%r",user.id,e)
    return False
