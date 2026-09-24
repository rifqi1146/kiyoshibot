from telegram import Update
from telegram.ext import MessageHandler, ChatMemberHandler, TypeHandler, filters

from handlers.blacklist import blacklist_message_gate
from handlers.caca import meta_query
from handlers.collector import collect_chat
from handlers.delete import reply_del_handler
from handlers.dl.router import auto_dl_detect
from handlers.prefix_dollar import dollar_router
from handlers.susunkata import susunkata_answer_handler
from handlers.welcome import welcome_handler, welcome_chat_member_handler
from utils.caca_memory import get_last_message_id as meta_db_get_last_message_id
from utils.caca_memory import has_last_message_id as meta_db_has_last_message_id
from utils.logger import log_commands
from utils.stop_draft import stopped_generation_handler
from utils.user_collector import user_collector
from handlers.gemini import ai_cmd
from utils.gemini_memory import get_last_message_id as ai_db_get_last_message_id
from utils.gemini_memory import has_last_message_id as ai_db_has_last_message_id
from handlers.groq import groq_query
from utils.groq_memory import get_last_message_id as groq_db_get_last_message_id
from utils.groq_memory import has_last_message_id as groq_db_has_last_message_id
from handlers.asahotak import asahotak_answer_handler

AI_REPLY_FILTER = (
    filters.REPLY
    & (
        filters.TEXT
        | filters.PHOTO
        | filters.Document.IMAGE
        | filters.Sticker.STATIC
    )
    & ~filters.COMMAND
)

async def ai_reply_router(update, context):
    msg = update.message
    if not msg or not msg.reply_to_message or not msg.from_user:
        return
    user_id = msg.from_user.id
    reply_mid = msg.reply_to_message.message_id

    groq_mid = await groq_db_get_last_message_id(user_id)
    if groq_mid == reply_mid:
        return await groq_query(update, context)

    meta_mid = await meta_db_get_last_message_id(user_id)
    if meta_mid == reply_mid:
        return await meta_query(update, context)

    ai_mid = await ai_db_get_last_message_id(user_id)
    if ai_mid == reply_mid:
        return await ai_cmd(update, context)

    if await groq_db_has_last_message_id(reply_mid):
        return await msg.reply_text(
            "😒 Lu siapa?\n"
            "Gue belum ngobrol sama lu.\n"
            "Ketik /groq dulu.",
            parse_mode="HTML"
        )

    if await ai_db_has_last_message_id(reply_mid):
        return await msg.reply_text(
            "😒 Lu siapa?\n"
            "Gue belum ngobrol sama lu.\n"
            "Ketik /ask dulu.",
            parse_mode="HTML"
        )

    if await meta_db_has_last_message_id(reply_mid):
        return await msg.reply_text(
            "😒 Lu siapa?\n"
            "Gue belum ngobrol sama lu.\n"
            "Ketik /caca dulu.",
            parse_mode="HTML"
        )
    return

def register_messages(app):
    app.add_handler(
        MessageHandler(filters.ALL, blacklist_message_gate),
        group=-100,
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, auto_dl_detect),
        group=-90,
    )

    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, asahotak_answer_handler),
        group=-4,
    )
    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, susunkata_answer_handler),
        group=-3,
    )
    app.add_handler(
        MessageHandler(AI_REPLY_FILTER, ai_reply_router),
        group=-2,
    )
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_handler),
        group=2,
    )
    app.add_handler(
        ChatMemberHandler(welcome_chat_member_handler, ChatMemberHandler.CHAT_MEMBER),
        group=2,
    )
    app.add_handler(
        MessageHandler(filters.TEXT & filters.REPLY, reply_del_handler),
        group=3,
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, dollar_router),
        group=4,
    )
    app.add_handler(
        MessageHandler(filters.ALL, collect_chat, block=False),
        group=50,
    )
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, user_collector, block=False),
        group=51,
    )
    app.add_handler(
        MessageHandler(filters.ALL, log_commands, block=False),
        group=100,
    )
    app.add_handler(
        TypeHandler(Update, stopped_generation_handler),
        group=101,
    )
