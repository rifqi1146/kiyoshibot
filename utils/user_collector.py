import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from handlers.ship import add_user

async def user_collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type == "private" or int(chat.id) > 0:
        return
    user = msg.from_user
    if not user or getattr(user, "is_bot", False):
        return
    await asyncio.to_thread(add_user, int(chat.id), user)