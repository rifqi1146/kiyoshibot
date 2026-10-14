from telegram import Update
from telegram.ext import ContextTypes
import logging

from .caca_prompt import PERSONAS
from database import premium
from database import caca_db
from utils import caca_memory

logger = logging.getLogger(__name__)


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return
    user_id = user.id
    if not premium.check(user_id):
        return await msg.reply_text(
            "❌ Persona mode is available for premium users only.\n"
            "Non-premium users are not allowed to change it 😤"
        )
    if not context.args:
        cur = caca_db.get_mode(user_id)
        return await msg.reply_text(
            f"🎭 Current mode: <b>{cur}</b>\n\n"
            "Available modes:\n"
            "• default\n"
            "• bokep\n"
            "• sarkas\n"
            "• toxic\n"
            "• yandere\n"
            "• cabul\n"
            "• masokis\n"
            "• loli",
            parse_mode="HTML"
        )
    mode = context.args[0].lower()
    if mode not in PERSONAS:
        return await msg.reply_text(
            f"❌ Unknown mode: <b>{mode}</b>\n\n"
            "Available modes:\n"
            "• default\n"
            "• bokep\n"
            "• sarkas\n"
            "• toxic\n"
            "• yandere\n"
            "• cabul\n"
            "• masokis\n"
            "• loli",
            parse_mode="HTML",
        )
    try:
        await caca_db.set_mode(user_id, mode)
    except Exception as e:
        logger.exception("Caca set_mode failed | user_id=%s mode=%s err=%r", user_id, mode, e)
        return await msg.reply_text(
            "❌ Failed to save persona. Try again in a moment.",
        )
    try:
        await caca_memory.clear(user_id)
    except Exception as e:
        # Memori gagal dibersihkan bukan alasan gagal ganti persona: mode sudah
        # tersimpan. Cukup dicatat supaya konteks lama tidak menghalangi.
        logger.warning("Caca memory clear failed after mode change | user_id=%s err=%r", user_id, e)
    return await msg.reply_text(
        f"🎭 Persona changed to <b>{mode}</b> ✨",
        parse_mode="HTML"
    )