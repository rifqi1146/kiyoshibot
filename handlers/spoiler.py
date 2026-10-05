import logging

log = logging.getLogger(__name__)

from telegram import Update
from telegram.ext import ContextTypes

from utils.config import OWNER_ID
from database.spoiler_db import spoiler_db_init, is_spoiler_enabled, set_spoiler
from database.download_db import is_premium_required

# dipanggil di bot.py walaupun tidak ada handler khusus, biar DB siap
try:
    spoiler_db_init()
except Exception:
    pass

async def _is_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if user and user.id in OWNER_ID:
        return True
    if not chat or chat.type not in ("group", "supergroup"):
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def spoiler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return await update.message.reply_text(
            "This command is only for groups.",
            parse_mode="HTML",
        )

    arg = (context.args[0].lower() if context.args else "").strip()

    if arg == "status":
        status = "ENABLED" if is_spoiler_enabled(chat.id, chat.type) else "DISABLED"
        return await update.message.reply_text(
            f"Spoiler status in this group: <b>{status}</b>",
            parse_mode="HTML",
        )

    # enable/disable: butuh admin/owner
    if arg in ("enable", "disable"):
        if not await _is_admin_or_owner(update, context):
            return await update.message.reply_text(
                "<b>You are not an admin.</b>",
                parse_mode="HTML",
            )
        enabled = arg == "enable"
        set_spoiler(chat.id, enabled)
        log.info(
            "Spoiler setting changed | chat_id=%s user_id=%s enabled=%s",
            chat.id, getattr(update.effective_user, "id", None), enabled,
        )
        state = "ENABLED" if enabled else "DISABLED"
        return await update.message.reply_text(
            f"Spoiler <b>{state}</b> in this group.\n\n"
            f"This only affects premium domains when sending photos/videos.",
            parse_mode="HTML",
        )

    return await update.message.reply_text(
        "<b>Spoiler Settings</b>\n\n"
        "<code>/spoiler enable</code> — wrap premium media with spoiler\n"
        "<code>/spoiler disable</code> — send premium media normally\n"
        "<code>/spoiler status</code> — check current state",
        parse_mode="HTML",
    )
