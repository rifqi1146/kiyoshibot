from telegram import Update
from telegram.ext import ContextTypes

from handlers.welcome import start_verify_pm
from database.share_db import get_share

# start command
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if context.args:
        arg = context.args[0]
        if arg.startswith("verify_"):
            return await start_verify_pm(update, context)
        if arg.startswith("share_"):
            share_id = arg.replace("share_", "")
            data = get_share(share_id)
            if data:
                target_chat_id, target_message_id = data
                try:
                    await context.bot.copy_message(
                        chat_id=update.effective_chat.id,
                        from_chat_id=target_chat_id,
                        message_id=target_message_id
                    )
                except Exception as e:
                    await msg.reply_text("Oops, this media has been deleted or is no longer accessible.")
            else:
                await msg.reply_text("This share link is invalid or has expired.")
            return
            
    user = update.effective_user
    name = (user.first_name or "").strip() or "there"
    text = (
        f"👋 Hello {name}!\n\n"
        "Type /help to see the menu."
    )
    await msg.reply_text(text)
