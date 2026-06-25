import uuid
from telegram import Update
from telegram.ext import ContextTypes
from database.share_db import save_share, get_share

async def share_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    if not msg.reply_to_message:
        return await msg.reply_text("Please reply to a photo/video message that you want to share using the /share command.")

    target_msg = msg.reply_to_message

    has_media = target_msg.photo or target_msg.video or target_msg.document or target_msg.audio or target_msg.animation
    if not has_media:
        return await msg.reply_text("The replied message doesn't contain any media.")

    share_id = uuid.uuid4().hex[:8]
    
    save_share(share_id, target_msg.chat.id, target_msg.message_id)

    bot_username = context.bot.username
    if not bot_username:
        me = await context.bot.get_me()
        bot_username = me.username
        
    share_link = f"https://t.me/{bot_username}?start=share_{share_id}"

    await msg.reply_text(
        f"🔗 <b>Media successfully!</b>\n\n"
        f"Share this link to view:\n<code>{share_link}</code>",
        parse_mode="HTML"
    )

