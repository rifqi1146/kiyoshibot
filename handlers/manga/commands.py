"""Command handler `/manga <source> <query/code>`."""
import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import premium
from database.nsfw_db import is_nsfw_allowed
from handlers.join import require_join_or_block

from .constants import NH_API_URL, NH_HEADERS
from .http import _escape, enforce_telegram_photo_limits, fetch_image_bytes, fetch_json
from .maid import search_maid
from .mangadex import build_search_list
from .nhentai import build_nh_detail_ui, build_nh_search_list, get_nh_cover_url
from .state import _move_message_lock, _set_message_lock

log = logging.getLogger(__name__)


def _is_nsfw_enabled(chat_id: int, chat_type: str) -> bool:
    return is_nsfw_allowed(chat_id, chat_type)


async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not _is_nsfw_enabled(chat.id, chat.type):
        return await msg.reply_text("❌ NSFW is not enabled in this group.")

    if not context.args or len(context.args) < 2:
        help_txt = (
            "⚠️ <b>Invalid format!</b>\n\n"
            "Use: <code>/manga &lt;source&gt; &lt;title/code&gt;</code>\n\n"
            "📚 <b>Sources:</b> <code>dex</code> | <code>maid</code> | <code>nh</code>\n"
            "💡 <b>Examples:</b>\n"
            "<code>/manga dex Haimiya-senpai</code>\n"
            "<code>/manga maid Osananajimi wo Onnanoko</code>\n"
            "<code>/manga nh Zenless Zone Zero Or 642563</code>\n"
        )
        return await msg.reply_text(help_txt, parse_mode="HTML")

    source = context.args[0].lower()
    full_query = " ".join(context.args[1:])
    user = update.effective_user

    if source in ["nh", "nhentai"] and (not user or not premium.check(user.id)):
        return await update.message.reply_text("❌ NH manga is only available for premium users.")

    status = await msg.reply_text(
        f"🔍 Processing <code>{_escape(full_query)}</code> in <b>{_escape(source.upper())}</b>...",
        parse_mode="HTML"
    )
    _set_message_lock(status.chat_id, status.message_id, update.effective_user.id)

    if source in ["dex", "mangadex"]:
        text, markup = await build_search_list(full_query, 0, context)
        if text:
            await status.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await status.edit_text("❌ Manga not found in MangaDex.", parse_mode="HTML")

    elif source in ["maid", "maidmanga"]:
        text, markup = await search_maid(full_query, context)
        if markup:
            await status.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await status.edit_text(text, parse_mode="HTML")

    elif source in ["nh", "nhentai"]:
        if full_query.isdigit():
            data = await fetch_json(f"{NH_API_URL}/galleries/{full_query}", custom_headers=NH_HEADERS)
            if not data or "error" in data or "title" not in data:
                return await status.edit_text("❌ Doujin not found (invalid code).", parse_mode="HTML")
            text, markup = build_nh_detail_ui(data)
            cover_url = get_nh_cover_url(data)
            cover_bytes = await fetch_image_bytes(cover_url, referer="https://nhentai.net/")
            if cover_bytes:
                await status.delete()
                img_safe = await asyncio.to_thread(enforce_telegram_photo_limits, cover_bytes)
                sent = await context.bot.send_photo(
                    status.chat_id,
                    photo=img_safe,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
                _move_message_lock(status.chat_id, status.message_id, sent.chat_id, sent.message_id, fallback_user_id=update.effective_user.id)
            else:
                await status.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            text, markup = await build_nh_search_list(full_query, 1, context)
            if text:
                await status.edit_text(text, parse_mode="HTML", reply_markup=markup)
            else:
                await status.edit_text("❌ Doujin not found.", parse_mode="HTML")

    else:
        await status.edit_text(
            "❌ <b>Unknown source.</b>\nChoose <code>dex</code>, <code>maid</code>, or <code>nh</code>.",
            parse_mode="HTML"
        )
