import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from handlers.ship import add_user

async def user_collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    await asyncio.to_thread(add_user, int(chat.id), msg.from_user)