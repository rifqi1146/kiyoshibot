from telegram import Update
from telegram.ext import ContextTypes
from utils.config import OWNER_ID
from handlers.dl.mtproto_uploader import is_fasttelethon_enabled, is_fasttelethon_available, set_fasttelethon_enabled

def _status_text() -> str:
    enabled = is_fasttelethon_enabled()
    available = is_fasttelethon_available()
    return (
        "<b>FastTelethon</b>\n\n"
        f"Status: <b>{'Enabled' if enabled else 'Disabled'}</b>\n"
        f"Package: <b>{'Installed' if available else 'Not installed'}</b>\n\n"
        "<code>/fasttelethon enable</code>\n"
        "<code>/fasttelethon disable</code>"
    )

async def fasttelethon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if user.id not in OWNER_ID:
        return
    args = context.args or []
    if not args:
        return await msg.reply_text(_status_text(), parse_mode="HTML")
    action = str(args[0]).lower().strip()
    if action in ("enable", "on", "1", "true"):
        set_fasttelethon_enabled(True)
        return await msg.reply_text("<b>FastTelethon enabled</b>", parse_mode="HTML")
    if action in ("disable", "off", "0", "false"):
        set_fasttelethon_enabled(False)
        return await msg.reply_text("<b>FastTelethon disabled</b>", parse_mode="HTML")
    if action in ("status", "info"):
        return await msg.reply_text(_status_text(), parse_mode="HTML")
    return await msg.reply_text(
        "<b>Usage</b>\n\n"
        "<code>/fasttelethon</code>\n"
        "<code>/fasttelethon enable</code>\n"
        "<code>/fasttelethon disable</code>",
        parse_mode="HTML",
    )