from telegram import Update
from telegram.ext import ContextTypes
import logging

from .caca_prompt import PERSONAS
from database import caca_db
from database import caca_billing_db
from utils.config import OWNER_ID
from utils import caca_memory

logger = logging.getLogger(__name__)


def _mode_allowed(user_id: int) -> bool:
    if int(user_id) in OWNER_ID:
        return True
    try:
        if caca_billing_db.is_freecaca_enabled():
            return True
    except Exception as e:
        logger.warning("freecaca check failed in /mode | err=%r", e)
    try:
        return caca_billing_db.has_active_credit(user_id, min_credits=1)
    except Exception as e:
        logger.warning("Credit check failed in /mode | err=%r", e)
        return False


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return
    user_id = user.id
    if not _mode_allowed(user_id):
        return await msg.reply_text(
            "Hai sayang... buat ganti persona, kamu perlu credit Caca yang masih aktif dulu ya \U0001f97a\n\n"
            "Top up pakai /cacatopup, atau cek saldo dengan /cacasaldo."
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
        logger.warning("Caca memory clear failed after mode change | user_id=%s err=%r", user_id, e)
    return await msg.reply_text(
        f"🎭 Persona changed to <b>{mode}</b> ✨",
        parse_mode="HTML"
    )