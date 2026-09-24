from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from utils.config import OWNER_ID


def helpowner_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Close", callback_data="helpowner:close")]
    ])


async def helpowner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    if user.id not in OWNER_ID:
        return

    text = (
        "<b>Owner Commands</b>\n"
        "<i>Administrative & system-level controls</i>\n\n"
    
        "<b>System Management</b>\n"
        "• <code>/autodel</code> — Manage auto delete asupan settings.\n"
        "• <code>/broadcast</code> — Announcement.\n"
        "• <code>/blacklist</code> — Manage blacklisted users.\n"
        "• <code>/fasttelethon</code> — Enable/disable FastTelethon upload.\n"
        "• <code>/reload</code> — Hot reload handlers, utils, database, and RAG.\n"
        "• <code>/restart</code> — Restart the bot.\n"
        "• <code>/speedtest</code> — Run server speed test.\n"
        "• <code>/update</code> — Update system Bot.\n"
        "• <code>/uploadengine</code> — Switch Telethon/PyroFork uploader.\n"
        "• <code>/wlc</code> — Configure welcome message.\n\n"
    
        "<b>Access & Billing</b>\n"
        "• <code>/addsudo</code> — Add sudo user.\n"
        "• <code>/cookies</code> — Update cookies via Telegram.\n"
        "• <code>/premium</code> — Manage premium users.\n"
        "• <code>/rmsudo</code> — Remove sudo user.\n"
        "• <code>/sudolist</code> — List sudo users.\n\n"
    
        "<b>Asupan Management</b>\n"
        "• <code>/asupann</code> — Manage asupan.\n\n"
    
        "<b>Backup System</b>\n"
        "• <code>/autobackup</code> — Enable/disable auto backup.\n"
        "• <code>/backup</code> — Create data backup.\n"
        "• <code>/restore</code> — Restore from backup file.\n\n"
    
        "<b>Caca Settings</b>\n"
        "• <code>/cacaa</code> — Enable/disable/list/status.\n"
    )

    await msg.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=helpowner_keyboard()
    )


async def helpowner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    if q.data != "helpowner:close":
        return

    try:
        await q.message.delete()
    except Exception:
        pass

