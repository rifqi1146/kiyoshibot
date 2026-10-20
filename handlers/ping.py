import time
from telegram import Update
from telegram.ext import ContextTypes

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = time.perf_counter()
    msg = await update.message.reply_text("Pong...")
    end = time.perf_counter()
    latency = int((end - start) * 1000)
    await msg.edit_text(
        f"<b>Pong!</b>\nLatency: <code>{latency} ms</code>",
        parse_mode="HTML"
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    from handlers.vps import account_cmd, firewall

    cleared = firewall.cancel_pending(int(msg.chat_id), int(user.id))
    cleared = account_cmd.cancel_pending(int(user.id)) or cleared
    if cleared:
        await msg.reply_text("<b>Cancelled.</b>", parse_mode="HTML")
    else:
        await msg.reply_text("<b>Nothing to cancel.</b>", parse_mode="HTML")
