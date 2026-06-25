import html
import logging
from telegram import Update
from telegram.ext import ContextTypes
from utils.config import LOG_CHAT_ID
from handlers.commands import COMMAND_HANDLERS
from handlers.dl.constants import AUTO_DOWNLOAD_DOMAINS

log = logging.getLogger(__name__)

def _is_image_document(msg) -> bool:
    doc = getattr(msg, "document", None)
    return bool(doc and str(doc.mime_type or "").lower().startswith("image/"))

def _is_loggable_reply_media(msg) -> bool:
    return bool(msg.photo or msg.sticker or _is_image_document(msg))

def _media_label(msg) -> str:
    if msg.photo:
        return "Photo"
    if msg.sticker:
        return "Sticker"
    if _is_image_document(msg):
        return "Image Document"
    return "Media"

async def _copy_or_forward_message(bot, from_chat_id: int, message_id: int):
    try:
        return await bot.forward_message(
            chat_id=LOG_CHAT_ID,
            from_chat_id=from_chat_id,
            message_id=message_id,
            disable_notification=True,
        )
    except Exception as e:
        log.warning("Forward log message failed, trying copy | chat_id=%s message_id=%s err=%r", from_chat_id, message_id, e)
    try:
        return await bot.copy_message(
            chat_id=LOG_CHAT_ID,
            from_chat_id=from_chat_id,
            message_id=message_id,
            disable_notification=True,
        )
    except Exception as e:
        log.warning("Copy log message failed | chat_id=%s message_id=%s err=%r", from_chat_id, message_id, e)
    return None

async def log_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not LOG_CHAT_ID or not msg or not msg.from_user:
        return

    user = msg.from_user
    name = user.full_name or user.first_name or "Unknown"
    username = f"@{user.username}" if user.username else "-"
    uid = user.id

    chat = update.effective_chat
    if not chat:
        return

    chat_type = str(chat.type or "unknown").upper()
    chat_name = chat.title or chat.full_name or "Private"
    text = (msg.text or msg.caption or "").strip()
    
    is_command = text.startswith("/") or text.startswith("$")
    has_dl_link = any(domain in text.lower() for domain in AUTO_DOWNLOAD_DOMAINS)
    
    should_forward = False
    forward_chat_id = None
    forward_message_id = None

    if is_command:
        cmd = text[1:].split()[0].split("@")[0].lower()
        if cmd not in [c[0] for c in COMMAND_HANDLERS]:
            return
        title = "<b>Command Log</b>"
        content = f"<code>{html.escape(text)}</code>"

        replied = msg.reply_to_message
        if replied and _is_loggable_reply_media(replied):
            should_forward = True
            forward_chat_id = chat.id
            forward_message_id = replied.message_id
            content += f"\n<i>Replied media: {html.escape(_media_label(replied))} forwarded below</i>"

    elif msg.reply_to_message and getattr(msg.reply_to_message.from_user, "id", None) == context.bot.id:
        title = "<b>Reply Log</b>"
        if _is_loggable_reply_media(msg):
            should_forward = True
            forward_chat_id = chat.id
            forward_message_id = msg.message_id
            content = f"<i>{html.escape(_media_label(msg))} forwarded below</i>"
        else:
            content = html.escape(text) if text else "<i>(non-text message)</i>"
            
    elif has_dl_link:
        title = "<b>Auto-Download Link Detected</b>"
        content = f"<code>{html.escape(text)}</code>"
        
    else:
        return

    log_text = (
        f"{title}\n"
        f"<b>Name</b> : {html.escape(name)}\n"
        f"<b>Username</b> : <code>{html.escape(username)}</code>\n"
        f"<b>User ID</b> : <code>{uid}</code>\n"
        f"<b>Chat</b> : {chat_type} | {html.escape(chat_name)}\n"
        f"<b>Chat ID</b> : <code>{chat.id}</code>\n"
        f"<b>Message ID</b> : <code>{msg.message_id}</code>\n"
        f"<b>Message</b> : {content}"
    )

    try:
        await context.bot.send_message(
            chat_id=LOG_CHAT_ID,
            text=log_text,
            parse_mode="HTML",
            disable_notification=True,
        )
        if should_forward and forward_chat_id and forward_message_id:
            await _copy_or_forward_message(context.bot, forward_chat_id, forward_message_id)
    except Exception:
        log.exception("Failed to send log message")
