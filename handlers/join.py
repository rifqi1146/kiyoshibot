import time
import logging
import asyncio
from telegram import InlineKeyboardMarkup,InlineKeyboardButton,Update
from telegram.ext import ContextTypes
from telegram.error import NetworkError,TimedOut,RetryAfter,BadRequest,Forbidden
from utils.config import SUPPORT_CHANNEL_ID,SUPPORT_CHANNEL_LINK
from database.join_status_db import (
    set_member_status,
    get_member_status,
    get_updated_at,
    MEMBER_STATUSES,
)

log=logging.getLogger(__name__)

MEMBERSHIP_TTL=600          # verifikasi ulang status member setiap 10 menit
_RETRY_DELAYS=(0.6,1.5)     # jeda sebelum retry ke-2 dan ke-3
# BadRequest/Forbidden adalah error permanen. Wajib dicek lebih dulu karena di
# PTB keduanya mewarisi NetworkError.
_PERMANENT_ERRORS=(BadRequest,Forbidden)
_TRANSIENT_ERRORS=(NetworkError,TimedOut,RetryAfter)


# ---------------------------------------------------------------- helpers

def _is_member_status(status:str|None)->bool:
    return str(status or "").strip().lower() in MEMBER_STATUSES


def _is_stale(status:str)->bool:
    """Status selain left/kicked boleh dipakai tanpa verifikasi ulang."""
    st=str(status or "").strip().lower()
    return st in ("", "unknown")


# ------------------------------------------------- event-driven (realtime)

async def chat_member_update(update:Update,context:ContextTypes.DEFAULT_TYPE):
    """Simpan perubahan keanggotaan channel support secara realtime.

    Dipanggil Telegram setiap kali status member berubah (join/leave/kick),
    sehingga begitu user keluar dari channel, akses bot dicabut pada detik itu
    juga tanpa menunggu TTL cache.
    """
    if not SUPPORT_CHANNEL_ID:
        return
    cm=update.chat_member
    if not cm:
        return
    try:
        chat_id=int(getattr(getattr(cm,"chat",None),"id",0) or 0)
    except (TypeError,ValueError):
        chat_id=0
    if chat_id!=int(SUPPORT_CHANNEL_ID):
        return
    # Ambil objek member yang statusnya berubah (bukan pelaku aksinya).
    # Saat admin mengeluarkan user, from_user=admin sedangkan targetnya ada
    # di new_chat_member.user.
    member=getattr(cm,"new_chat_member",None)
    user=getattr(member,"user",None)
    user_id=getattr(user,"id",None)
    if not user_id:
        return
    new_status=getattr(member,"status",None)
    old_status=getattr(getattr(cm,"old_chat_member",None),"status",None)
    is_member=set_member_status(user_id,new_status)
    log.info(
        "[JOIN EVENT] chat=%s user_id=%s %s -> %s member=%s",
        chat_id,user_id,old_status,new_status,is_member,
    )


def register_join_handlers(app):
    """Daftarkan handler event keanggotaan. Idempotent."""
    from telegram.ext import ChatMemberHandler
    if getattr(app,"_join_handlers_registered",False):
        return
    app.add_handler(
        ChatMemberHandler(chat_member_update,ChatMemberHandler.CHAT_MEMBER),
        group=-100,
    )
    app._join_handlers_registered=True
    log.info("Join membership handlers registered")


# ------------------------------------------------------------ verifikasi

async def _fetch_member_status(user_id:int,context:ContextTypes.DEFAULT_TYPE):
    """Panggil get_chat_member dengan retry singkat untuk error jaringan.

    Return ``(status, error, transient)``. ``status`` None berarti gagal.
    """
    last_err=None
    transient=False
    for attempt,sleep_for in enumerate((0.0,)+_RETRY_DELAYS):
        if sleep_for:
            await asyncio.sleep(sleep_for)
        try:
            member=await context.bot.get_chat_member(SUPPORT_CHANNEL_ID,user_id)
            return getattr(member,"status",None),None,False
        except RetryAfter as e:
            last_err=e; transient=True
            wait=max(int(getattr(e,"retry_after",2) or 2),1)
            log.warning("[JOIN RETRY] Flood control | user_id=%s attempt=%s wait=%ss",user_id,attempt+1,wait)
            await asyncio.sleep(wait+0.5)
        except _PERMANENT_ERRORS as e:
            return None,e,False
        except (NetworkError,TimedOut) as e:
            last_err=e; transient=True
            log.warning("[JOIN RETRY] Transient network error | user_id=%s attempt=%s err=%r",user_id,attempt+1,e)
        except Exception as e:
            return None,e,False
    return None,last_err,transient


async def is_joined_support_channel(user_id:int,context:ContextTypes.DEFAULT_TYPE)->bool:
    """Cek keanggotaan channel support.

    Sumber utama adalah database yang di-update realtime oleh
    ``chat_member_update``. Baris ``left``/``kicked`` langsung memblokir user
    pada detik itu juga. Status member diverifikasi ulang setelah TTL tertentu.
    """
    if not SUPPORT_CHANNEL_ID:
        return True

    status,is_member=get_member_status(user_id)

    if status and not _is_stale(status):
        if is_member:
            if await _member_needs_recheck(user_id):
                await _recheck_member(user_id,context)
                status,is_member=get_member_status(user_id)
            return bool(is_member)
        # Terakhir diketahui bukan member: verifikasi ulang agar user yang
        # baru join langsung bisa (tanpa menunggu event).
        fetched,err,transient=await _fetch_member_status(user_id,context)
        if fetched is not None:
            return set_member_status(user_id,fetched)
        if transient:
            log.warning("[JOIN CHECK ERROR] Transient failure, allowing access | user_id=%s err=%r",user_id,err)
            return True
        log.warning("[JOIN CHECK ERROR] Permanent failure | user_id=%s err=%r",user_id,err)
        return False

    # Belum ada data / status belum diketahui -> tanya Telegram langsung.
    fetched,err,transient=await _fetch_member_status(user_id,context)
    if fetched is not None:
        return set_member_status(user_id,fetched)
    if transient:
        log.warning("[JOIN CHECK ERROR] Transient failure, allowing access | user_id=%s err=%r",user_id,err)
        return True
    log.warning("[JOIN CHECK ERROR] Permanent failure | user_id=%s err=%r",user_id,err)
    return False


async def _member_needs_recheck(user_id:int)->bool:
    updated_at=get_updated_at(user_id)
    if updated_at is None:
        return True
    return (time.time()-updated_at)>MEMBERSHIP_TTL


async def _recheck_member(user_id:int,context:ContextTypes.DEFAULT_TYPE):
    fetched,err,transient=await _fetch_member_status(user_id,context)
    if fetched is not None:
        set_member_status(user_id,fetched)
    elif not transient:
        log.warning("[JOIN CHECK ERROR] Permanent failure on recheck | user_id=%s err=%r",user_id,err)


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
