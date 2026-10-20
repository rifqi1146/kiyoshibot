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

MEMBERSHIP_TTL=120
_RETRY_DELAYS=(0.6,1.5)
_PERMANENT_ERRORS=(BadRequest,Forbidden)
_TRANSIENT_ERRORS=(NetworkError,TimedOut,RetryAfter)


def _is_member_status(status:str|None)->bool:
    return str(status or "").strip().lower() in MEMBER_STATUSES


def _is_stale(status:str)->bool:
    st=str(status or "").strip().lower()
    return st in ("", "unknown")


async def chat_member_update(update:Update,context:ContextTypes.DEFAULT_TYPE):
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
    from telegram.ext import ChatMemberHandler, MessageHandler, filters
    if getattr(app,"_join_handlers_registered",False):
        return
    app.add_handler(
        ChatMemberHandler(chat_member_update,ChatMemberHandler.CHAT_MEMBER),
        group=-100,
    )
    app.add_handler(
        MessageHandler(filters.COMMAND, command_join_gate),
        group=-101,
    )
    app._join_handlers_registered=True
    log.info("Join membership handlers registered")


async def _fetch_member_status(user_id:int,context:ContextTypes.DEFAULT_TYPE):
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


def _is_bot_privilege_error(err)->bool:
    text=str(err or "").lower()
    return any(
        marker in text
        for marker in (
            "chat_admin_required",
            "not enough rights",
            "bot is not a member",
            "bot was kicked",
            "chat not found",
            "forbidden",
        )
    )


async def is_joined_support_channel(user_id:int,context:ContextTypes.DEFAULT_TYPE)->bool:
    if not SUPPORT_CHANNEL_ID:
        return True

    status,is_member=get_member_status(user_id)
    fresh_cached=bool(status) and not _is_stale(status) and not await _member_needs_recheck(user_id)

    if fresh_cached and is_member:
        return True

    fetched,err,transient=await _fetch_member_status(user_id,context)

    if fetched is not None:
        return set_member_status(user_id,fetched)

    if _is_bot_privilege_error(err):
        log.error(
            "[JOIN CHECK BROKEN] Bot cannot read membership in the support channel. "
            "Promote the bot to administrator, otherwise the gate silently fails open "
            "for everyone | user_id=%s err=%r",
            user_id,err,
        )
        return True

    if fresh_cached:
        return bool(is_member)

    if transient:
        log.warning("[JOIN CHECK ERROR] Transient failure, falling back to cached state | user_id=%s err=%r",user_id,err)
        return bool(is_member)

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


async def require_join_or_block(
    update:Update,
    context:ContextTypes.DEFAULT_TYPE,
    silent:bool=False,
)->bool:
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
    if silent:
        log.info("[JOIN SILENT BLOCK] user_id=%s tidak join, handler pasif -> tolak diam-diam",user.id)
        return False
    if not SUPPORT_CHANNEL_LINK:
        log.warning("SUPPORT_CHANNEL_ID is set but SUPPORT_CHANNEL_LINK is missing. Allowing access.")
        return True
    text="<b>To use the feature</b>\nyou must join the support channel first."
    try:
        if update.callback_query:
            await update.callback_query.answer("Please join the support channel first",show_alert=True)
            chat_type=getattr(getattr(reply_target,"chat",None),"type","")
            if chat_type=="private":
                await reply_target.reply_text(text,reply_markup=join_required_keyboard(),parse_mode="HTML")
        else:
            await reply_target.reply_text(text,reply_markup=join_required_keyboard(),parse_mode="HTML")
    except Exception as e:
        log.warning("[JOIN BLOCK ERROR] user_id=%s err=%r",user.id,e)
    return False


OPEN_COMMANDS = {
    "start",
    "help",
    "menu",
    "ping",
    "buycaca",
    "cacasaldo",
    "cacatopup",
}

PRIVILEGED_COMMANDS = {
    "addcredit", "addawsaccount", "addsudo", "autobackup", "backup", "blacklist",
    "broadcast", "buycaca", "cacaadmin", "cbacc", "cookies", "delawsaccount",
    "freecaca", "getmodel", "helpowner", "premium", "providercaca", "reload",
    "reboot", "restart", "restore", "rmsudo", "setmodel", "setsearch",
    "sudolist", "stats", "thinking", "update", "uploadengine",
    "vpsadmin", "vpsgrant", "vpsinfo", "vpslist", "vpsprice", "vpsrenew",
    "vpsstats", "vpsterminate",
    "ban", "unban", "kick", "mute", "unmute", "warn", "unwarn", "promote",
    "demote", "tag", "untag", "moderation", "nsfw", "spoiler", "settings",
    "wlc", "autodel", "speedtest",
}


def _command_name(update: Update) -> str:
    msg = update.effective_message
    text = (getattr(msg, "text", None) or "").strip()
    if not text.startswith("/"):
        return ""
    head = text.split()[0][1:]
    head = head.split("@", 1)[0]
    return head.lower()


def _is_exempt_command(name: str) -> bool:
    if not name:
        return True
    if name in OPEN_COMMANDS or name in PRIVILEGED_COMMANDS:
        return True
    if name.startswith("vps"):
        return True
    return False


async def command_join_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.ext import ApplicationHandlerStop

    name = _command_name(update)
    if not name:
        return
    if _is_exempt_command(name):
        return

    user = update.effective_user
    if not user:
        return
    try:
        from utils.config import OWNER_ID

        if int(user.id) in OWNER_ID:
            return
    except Exception:
        pass

    joined = await is_joined_support_channel(user.id, context)
    if joined:
        return

    msg = update.effective_message
    text = "<b>To use the bot</b>\nyou must join the support channel first."
    try:
        if msg:
            await msg.reply_text(text, reply_markup=join_required_keyboard(), parse_mode="HTML")
    except Exception as e:
        log.warning("[JOIN GATE ERROR] user_id=%s err=%r", user.id, e)
    raise ApplicationHandlerStop
