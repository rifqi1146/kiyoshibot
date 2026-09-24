from telegram import Update
from telegram.ext import ContextTypes
from utils.config import OWNER_ID


async def reply_del_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    if not msg or not msg.reply_to_message:
        return

    if user.id not in OWNER_ID:
        return

    if msg.text.strip().lower() != "del":
        return

    target = msg.reply_to_message

    if not target.from_user or not target.from_user.is_bot:
        return

    try:
        await target.delete()
    except Exception:
        pass