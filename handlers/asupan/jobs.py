from telegram.ext import ContextTypes
from .constants import ASUPAN_AUTO_DELETE_SEC, log
from . import state
from database.asupan_db import is_autodel_enabled


async def _expire_asupan_notice(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        await context.bot.delete_message(
            job.data["chat_id"],
            job.data["message_id"],
        )
    except Exception:
        pass


async def _expire_asupan_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    asupan_msg_id = job.data["asupan_msg_id"]
    reply_to = job.data["reply_to"]

    if state.ASUPAN_DELETE_JOBS.get((chat_id, asupan_msg_id)) is not job:
        return

    try:
        await context.bot.delete_message(chat_id, asupan_msg_id)

        msg = await context.bot.send_message(
            chat_id,
            "⏳ <b>Asupan Closed</b>\n\n"
            "No activity detected for <b>5 minutes</b>.\n"
            "This asupan session has been automatically closed 🍜\n\n",
            reply_to_message_id=reply_to,
            parse_mode="HTML",
        )

        context.application.job_queue.run_once(
            _expire_asupan_notice,
            15,
            data={
                "chat_id": chat_id,
                "message_id": msg.message_id,
            },
        )
    except Exception:
        log.exception("[ASUPAN EXPIRE] Error")

    state.ASUPAN_DELETE_JOBS.pop((chat_id, asupan_msg_id), None)
    state.ASUPAN_MESSAGE_KEYWORD.pop((chat_id, asupan_msg_id), None)


def reset_asupan_delete_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int, asupan_msg_id: int, reply_to: int | None):
    old_job = state.ASUPAN_DELETE_JOBS.pop((chat_id, asupan_msg_id), None)
    if old_job:
        old_job.schedule_removal()

    job = context.application.job_queue.run_once(
        _expire_asupan_job,
        ASUPAN_AUTO_DELETE_SEC,
        data={
            "chat_id": chat_id,
            "asupan_msg_id": asupan_msg_id,
            "reply_to": reply_to,
        },
    )
    state.ASUPAN_DELETE_JOBS[(chat_id, asupan_msg_id)] = job


def clear_asupan_delete_job(chat_id: int, asupan_msg_id: int):
    old_job = state.ASUPAN_DELETE_JOBS.get((chat_id, asupan_msg_id))
    if old_job:
        old_job.schedule_removal()
        state.ASUPAN_DELETE_JOBS.pop((chat_id, asupan_msg_id), None)


def should_use_autodel(chat) -> bool:
    return chat.type != "private" and is_autodel_enabled(chat.id)