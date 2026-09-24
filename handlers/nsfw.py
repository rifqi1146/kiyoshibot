import html

from telegram import Update
from telegram.ext import ContextTypes

from utils.config import OWNER_ID

from database.nsfw_db import (
    nsfw_db_init as nsfw_db_init,
    is_nsfw_allowed,
    set_nsfw,
    get_all_enabled,
)

async def is_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    if user.id in OWNER_ID:
        return True

    if chat.type not in ("group", "supergroup"):
        return False

    try:
        m = await context.bot.get_chat_member(chat.id, user.id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


async def nsfw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    arg = context.args[0].lower() if context.args else ""

    if arg == "list":
        if user.id not in OWNER_ID:
            return

        groups = get_all_enabled()

        if not groups:
            return await update.message.reply_text(
                "No NSFW-enabled groups.",
                parse_mode="HTML"
            )

        lines = ["<b>NSFW Enabled Groups</b>\n"]

        for gid in groups:
            try:
                c = await context.bot.get_chat(gid)
                title = html.escape(c.title or str(gid))
                lines.append(f"• {title}")
            except Exception:
                lines.append(f"• <code>{gid}</code>")

        return await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML"
        )

    if not await is_admin_or_owner(update, context):
        return

    if arg == "enable":
        set_nsfw(chat.id, True)
        return await update.message.reply_text(
            "NSFW <b>ENABLED</b> in this group.",
            parse_mode="HTML"
        )

    if arg == "disable":
        set_nsfw(chat.id, False)
        return await update.message.reply_text(
            "NSFW <b>DISABLED</b> in this group.",
            parse_mode="HTML"
        )

    if arg == "status":
        status = "ENABLED" if is_nsfw_allowed(chat.id, chat.type) else "DISABLED"
        return await update.message.reply_text(
            f"NSFW status in this group: <b>{status}</b>",
            parse_mode="HTML"
        )

    return await update.message.reply_text(
        "<b>NSFW Settings</b>\n\n"
        "<code>/nsfw enable</code>\n"
        "<code>/nsfw disable</code>\n"
        "<code>/nsfw status</code>\n"
        "<code>/nsfw list</code>",
        parse_mode="HTML"
    )