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
from .cache import (
    build_video_payload,
    get_asupan_fast,
    warm_asupan_cache,
    warm_keyword_asupan_cache,
    warm_startup_asupan,
)
from .jobs import reset_asupan_delete_job, clear_asupan_delete_job, should_use_autodel
from .constants import ASUPAN_COOLDOWN_SEC, ASUPAN_MESSAGE_KEYWORD_MAX, ASUPAN_REFILL_THRESHOLD, log

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
        sent = None
        last_err = None
        for _ in range(3):
            try:
                data = await get_asupan_fast(context.bot, keyword)
                media_arg, media_kw = build_video_payload(data)
                sent = await chat.send_video(
                    video=media_arg,
                    reply_to_message_id=update.message.message_id,
                    reply_markup=asupan_keyboard(user.id),
                    **media_kw,
                )
                break
            except Exception as e:
                last_err = e
        if sent is None:
            raise last_err or RuntimeError("Failed to send asupan")
        log.info(
            "[ASUPAN] source=%s | keyword=%r id=%s",
            data.get("source", "scraper"),
            data.get("keyword") or keyword,
            data.get("id"),
        )
        state.ASUPAN_MESSAGE_KEYWORD[(chat.id, sent.message_id)] = keyword
        while len(state.ASUPAN_MESSAGE_KEYWORD) > ASUPAN_MESSAGE_KEYWORD_MAX:
            oldest = next(iter(state.ASUPAN_MESSAGE_KEYWORD))
            state.ASUPAN_MESSAGE_KEYWORD.pop(oldest, None)
        if should_use_autodel(chat):
            reset_asupan_delete_job(context, chat.id, sent.message_id, update.message.message_id)
        await msg.delete()
        if keyword:
            kw_cache = state.ASUPAN_KEYWORD_CACHE.get(keyword.lower().strip()) or []
            if len(kw_cache) <= ASUPAN_REFILL_THRESHOLD:
                context.application.create_task(warm_keyword_asupan_cache(context.bot, keyword))
        elif len(state.ASUPAN_CACHE) <= ASUPAN_REFILL_THRESHOLD:
            context.application.create_task(warm_asupan_cache(context.bot))
    except Exception as e:
        await msg.edit_text(f"❌ Failed: {e}")

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
        await q.answer("❌ This is not your asupan, idiot!", show_alert=True)
        return
    msg_id = q.message.message_id
    msg_key = (q.message.chat_id, msg_id)
    if not hasattr(state, "ASUPAN_INFLIGHT"):
        state.ASUPAN_INFLIGHT = set()
    if msg_key in state.ASUPAN_INFLIGHT:
        try:
            await q.answer("⏳ Loading next asupan...", show_alert=False)
        except Exception:
            pass
        return
    if user_id not in OWNER_ID:
        now = time.time()
        last = state.ASUPAN_COOLDOWN.get(user_id, 0)
        if now - last < ASUPAN_COOLDOWN_SEC:
            await q.answer(
                f"Wait {ASUPAN_COOLDOWN_SEC}s before switching asupan again.",
                show_alert=True,
            )
            return
        if len(state.ASUPAN_COOLDOWN) > 512:
            cutoff = now - ASUPAN_COOLDOWN_SEC * 12
            for uid in [k for k, ts in state.ASUPAN_COOLDOWN.items() if ts < cutoff]:
                state.ASUPAN_COOLDOWN.pop(uid, None)
        state.ASUPAN_COOLDOWN[user_id] = now
    state.ASUPAN_INFLIGHT.add(msg_key)
    reply_to = q.message.reply_to_message.message_id if q.message.reply_to_message else None
    try:
        keyword = state.ASUPAN_MESSAGE_KEYWORD.get(msg_key)
        clear_asupan_delete_job(q.message.chat_id, msg_id)
        last_err = None
        edited = False
        data = None
        for _ in range(3):
            try:
                data = await get_asupan_fast(context.bot, keyword)
                media_arg, media_kw = build_video_payload(data)
                await q.message.edit_media(
                    media=InputMediaVideo(media=media_arg, **media_kw),
                    reply_markup=asupan_keyboard(owner_id),
                )
                edited = True
                break
            except Exception as e:
                last_err = e
        if not edited or data is None:
            raise last_err or RuntimeError("Failed to edit asupan media")
        log.info(
            "[ASUPAN] source=%s | keyword=%r id=%s",
            data.get("source", "scraper"),
            data.get("keyword") or keyword,
            data.get("id"),
        )
        try:
            await q.answer()
        except Exception:
            pass
        if should_use_autodel(q.message.chat):
            reset_asupan_delete_job(context, q.message.chat_id, msg_id, reply_to)
        state.ASUPAN_MESSAGE_KEYWORD[msg_key] = keyword
        if keyword:
            kw_cache = state.ASUPAN_KEYWORD_CACHE.get(keyword.lower().strip()) or []
            if len(kw_cache) <= ASUPAN_REFILL_THRESHOLD:
                context.application.create_task(warm_keyword_asupan_cache(context.bot, keyword))
        elif len(state.ASUPAN_CACHE) <= ASUPAN_REFILL_THRESHOLD:
            context.application.create_task(warm_asupan_cache(context.bot))
    except Exception:
        if should_use_autodel(q.message.chat):
            reset_asupan_delete_job(context, q.message.chat_id, msg_id, reply_to)
        try:
            await q.answer("❌ Failed to fetch asupan", show_alert=True)
        except Exception:
            pass
    finally:
        state.ASUPAN_INFLIGHT.discard(msg_key)

async def send_asupan_once(bot):
    if not LOG_CHAT_ID:
        log.warning("[ASUPAN STARTUP] Chat_id is empty")
        return
    try:
        await warm_startup_asupan(bot)
        log.info("[ASUPAN STARTUP] Warmup success | cache=%s", len(state.ASUPAN_CACHE))
    except Exception as e:
        log.warning(f"[ASUPAN STARTUP] Failed: {e}")