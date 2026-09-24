import html
import time
from telegram import Update, InputMediaVideo
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from utils.config import OWNER_ID, LOG_CHAT_ID
from . import state
from .auth import is_admin_or_owner
from database.asupan_db import save_asupan_groups, save_autodel_groups, is_asupan_enabled, is_autodel_enabled
from .keyboards import asupan_keyboard
from .cache import get_asupan_fast, warm_asupan_cache, warm_keyword_asupan_cache
from .jobs import reset_asupan_delete_job, clear_asupan_delete_job, should_use_autodel
from .constants import ASUPAN_COOLDOWN_SEC, log

async def asupann_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    bot = context.bot
    if not chat or chat.type == "private":
        return
    if not await is_admin_or_owner(update, context):
        return
    if not context.args:
        return await update.message.reply_text(
            "<b>📦 Asupan Command</b>\n\n"
            "• <code>/asupann enable</code>\n"
            "• <code>/asupann disable</code>\n"
            "• <code>/asupann status</code>\n"
            "• <code>/asupann list</code>",
            parse_mode="HTML",
        )
    sub = context.args[0].lower()
    if sub == "enable":
        state.ASUPAN_ENABLED_CHATS.add(chat.id)
        save_asupan_groups()
        return await update.message.reply_text(
            "Asupan has been <b>ENABLED</b> in this group.",
            parse_mode="HTML",
        )
    if sub == "disable":
        state.ASUPAN_ENABLED_CHATS.discard(chat.id)
        save_asupan_groups()
        return await update.message.reply_text(
            "Asupan has been <b>DISABLED</b> in this group.",
            parse_mode="HTML",
        )
    if sub == "status":
        if chat.id in state.ASUPAN_ENABLED_CHATS:
            return await update.message.reply_text(
                "Asupan status in this group: <b>ENABLED</b>",
                parse_mode="HTML",
            )
        return await update.message.reply_text(
            "Asupan status in this group: <b>DISABLED</b>",
            parse_mode="HTML",
        )
    if sub == "list":
        if user.id not in OWNER_ID:
            return
        if not state.ASUPAN_ENABLED_CHATS:
            return await update.message.reply_text("No groups have Asupan enabled yet.")
        lines = ["<b>Active Asupan Groups</b>\n"]
        for cid in state.ASUPAN_ENABLED_CHATS:
            try:
                c = await bot.get_chat(cid)
                title = c.title or c.username or "Unknown"
                lines.append(f"• {html.escape(title)}")
            except Exception:
                lines.append(f"• <code>{cid}</code>")
        return await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def autodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return
    if not await is_admin_or_owner(update, context):
        return
    if not context.args:
        return await update.message.reply_text(
            "<b>🗑 Auto Delete Asupan</b>\n\n"
            "• <code>/autodel enable</code>\n"
            "• <code>/autodel disable</code>\n"
            "• <code>/autodel status</code>\n"
            "• <code>/autodel list</code>",
            parse_mode="HTML",
        )
    arg = context.args[0].lower()
    if arg == "enable":
        state.AUTODEL_ENABLED_CHATS.add(chat.id)
        save_autodel_groups()
        return await update.message.reply_text(
            "Auto delete Asupan has been <b>ENABLED</b> in this group.",
            parse_mode="HTML",
        )
    if arg == "disable":
        state.AUTODEL_ENABLED_CHATS.discard(chat.id)
        save_autodel_groups()
        return await update.message.reply_text(
            "Auto delete Asupan has been <b>DISABLED</b> in this group.",
            parse_mode="HTML",
        )
    if arg == "status":
        status = "ENABLED" if is_autodel_enabled(chat.id) else "DISABLED"
        return await update.message.reply_text(
            f"Auto delete Asupan status: <b>{status}</b>",
            parse_mode="HTML",
        )
    if arg == "list":
        if user.id not in OWNER_ID:
            return
        if not state.AUTODEL_ENABLED_CHATS:
            return await update.message.reply_text("No groups have auto delete Asupan enabled.")
        lines = ["<b>Active Auto Delete Asupan Groups</b>\n"]
        for cid in state.AUTODEL_ENABLED_CHATS:
            try:
                c = await context.bot.get_chat(cid)
                name = c.title or c.username or "Unknown"
                lines.append(f"• {html.escape(name)}")
            except Exception:
                lines.append(f"• <code>{cid}</code>")
        return await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def asupan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if chat.type != "private":
        if not is_asupan_enabled(chat.id):
            return await update.message.reply_text(
                "🚫 Asupan feature is not available in this group.",
                parse_mode="HTML",
            )
    keyword = " ".join(context.args).strip() if context.args else None
    msg = await update.message.reply_text("😋 Searching asupan...")
    try:
        data = await get_asupan_fast(context.bot, keyword)
        sent = await chat.send_video(
            video=data["file_id"],
            reply_to_message_id=update.message.message_id,
            reply_markup=asupan_keyboard(user.id),
        )
        state.ASUPAN_MESSAGE_KEYWORD[sent.message_id] = keyword
        if should_use_autodel(chat):
            reset_asupan_delete_job(context, chat.id, sent.message_id, update.message.message_id)
        await msg.delete()
        context.application.create_task(warm_asupan_cache(context.bot))
        if keyword:
            context.application.create_task(warm_keyword_asupan_cache(context.bot, keyword))
    except Exception as e:
        await msg.edit_text(f"❌ Gagal: {e}")

async def asupan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    try:
        _, action, owner_id = q.data.split(":")
        owner_id = int(owner_id)
    except Exception:
        await q.answer("❌ Invalid callback", show_alert=True)
        return
    if user_id != owner_id:
        await q.answer("❌ Bukan asupan lu dongo!", show_alert=True)
        return
    if user_id not in OWNER_ID:
        now = time.time()
        last = state.ASUPAN_COOLDOWN.get(user_id, 0)
        if now - last < ASUPAN_COOLDOWN_SEC:
            await q.answer(
                f"Tunggu {ASUPAN_COOLDOWN_SEC} detik sebelum ganti asupan lagi.",
                show_alert=True,
            )
            return
        state.ASUPAN_COOLDOWN[user_id] = now
    await q.answer()
    try:
        msg_id = q.message.message_id
        keyword = state.ASUPAN_MESSAGE_KEYWORD.get(msg_id)
        clear_asupan_delete_job(msg_id)
        data = await get_asupan_fast(context.bot, keyword)
        await q.message.edit_media(
            media=InputMediaVideo(media=data["file_id"]),
            reply_markup=asupan_keyboard(owner_id),
        )
        reply_to = q.message.reply_to_message.message_id if q.message.reply_to_message else None
        if should_use_autodel(q.message.chat):
            reset_asupan_delete_job(context, q.message.chat_id, msg_id, reply_to)
        state.ASUPAN_MESSAGE_KEYWORD[msg_id] = keyword
        if keyword:
            context.application.create_task(warm_keyword_asupan_cache(context.bot, keyword))
        else:
            context.application.create_task(warm_asupan_cache(context.bot))
    except Exception:
        await q.answer("❌ Gagal ambil asupan", show_alert=True)

async def send_asupan_once(bot):
    if not LOG_CHAT_ID:
        log.warning("[ASUPAN STARTUP] Chat_id is empty")
        return
    try:
        data = await get_asupan_fast(bot)
        msg = await bot.send_video(
            chat_id=LOG_CHAT_ID,
            video=data["file_id"],
            disable_notification=True,
        )
        await msg.delete()
        log.info("[ASUPAN STARTUP] Warmup success")
    except Exception as e:
        log.warning(f"[ASUPAN STARTUP] Failed: {e}")