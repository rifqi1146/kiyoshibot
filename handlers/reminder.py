import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

WIB_OFFSET = 7


def parse_time_wib(time_str: str):
    if not re.match(r"^\d{2}\.\d{2}$", time_str):
        return None

    h, m = map(int, time_str.split("."))
    if h > 23 or m > 59:
        return None

    now_utc = datetime.utcnow()
    now_wib = now_utc + timedelta(hours=WIB_OFFSET)

    target = now_wib.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now_wib:
        target += timedelta(days=1)

    return target


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data

    kwargs = {}
    thread_id = data.get("thread_id")
    if thread_id:
        kwargs["message_thread_id"] = thread_id

    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=data["text"],
        parse_mode="HTML",
        **kwargs,
    )


async def reminder_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    jobs = context.application.job_queue.get_jobs_by_name(q.data)

    if not jobs:
        await q.answer("Reminder is no longer active", show_alert=True)
        try:
            await q.message.edit_text("Reminder is no longer active")
        except Exception:
            pass
        return

    job = jobs[0]
    owner_id = job.data["user_id"]

    if q.from_user.id != owner_id:
        await q.answer("This is not your reminder, idiot", show_alert=True)
        return

    await q.answer("Reminder cancelled")

    job.schedule_removal()
    try:
        await q.message.edit_text("Reminder cancelled")
    except Exception:
        pass


async def reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if len(context.args) < 2:
        return await msg.reply_text(
            "Wrong format\n\n"
            "Example:\n"
            "/reminder 18.30 play ml @user1"
        )

    time_str = context.args[0]
    target_time = parse_time_wib(time_str)

    if not target_time:
        return await msg.reply_text("Time format must be HH.MM (WIB)")

    text = " ".join(context.args[1:])
    thread_id = msg.message_thread_id

    delay = (target_time - (datetime.utcnow() + timedelta(hours=WIB_OFFSET))).total_seconds()
    job_name = f"reminder:{chat.id}:{msg.id}"

    reminder_text = (
        f"⏰ <b>REMINDER</b>\n\n"
        f"{text}\n\n"
        f"<i>Time: {time_str} WIB</i>"
    )

    context.application.job_queue.run_once(
        reminder_job,
        when=delay,
        name=job_name,
        data={
            "chat_id": chat.id,
            "thread_id": thread_id,
            "user_id": user.id,
            "text": reminder_text,
        },
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "❌ Cancel Reminder",
            callback_data=job_name
        )]
    ])

    await msg.reply_text(
        f"Reminder set for <b>{time_str} WIB</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )