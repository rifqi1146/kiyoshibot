"""Command handler `/moviebox`."""
import asyncio
import html
import logging
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database.download_db import is_premium_user
from handlers.join import require_join_or_block
from utils.menu_lock import message_id_from_send, register_message_owner

from .constants import MAX_RESULTS, PREFIX
from .render import build_menu_content, build_results_content, menu_markup
from .rich import edit_rich, send_rich
from .scraper import search
from .state import MOVIEBOX_CACHE, purge_expired_cache

log = logging.getLogger(__name__)


async def moviebox_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main command handler for /moviebox."""
    if not await require_join_or_block(update, context):
        return

    msg = update.message
    if not msg or not update.effective_user:
        return

    user_id = update.effective_user.id
    if not is_premium_user(user_id):
        return await msg.reply_text(
            "<b>Premium Required</b>\n\nThis command is restricted to <b>Premium Users</b> only.",
            parse_mode="HTML",
        )

    query = " ".join(context.args).strip() if context.args else ""
    purge_expired_cache()

    if not query:
        r_html, p_html = build_menu_content()
        sent = await send_rich(
            bot=context.bot,
            chat_id=msg.chat.id,
            reply_to=msg.message_id,
            rich_html=r_html,
            plain_html=p_html,
            markup=menu_markup(),
        )
        register_message_owner(msg.chat.id, message_id_from_send(sent), user_id)
        return sent

    log.info("Search start | site=moviebox q=%r chat_id=%s user_id=%s", query, update.effective_chat.id, user_id)
    status = await msg.reply_text(
        f"🔍 Searching <code>{html.escape(query)}</code>...",
        parse_mode="HTML",
    )
    register_message_owner(msg.chat.id, status.message_id, user_id)

    started = time.monotonic()
    results = await asyncio.to_thread(search, query)
    log.info(
        "Search done | site=moviebox q=%r results=%d elapsed=%.1fs",
        query, len(results), time.monotonic() - started,
    )

    if not results:
        return await status.edit_text(
            f"❌ No results found for <code>{html.escape(query)}</code>.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu")]]),
            parse_mode="HTML",
        )

    session_id = uuid.uuid4().hex[:8]
    MOVIEBOX_CACHE[session_id] = {
        "title": "MovieBox Search",
        "query": query,
        "results": results[:MAX_RESULTS],
        "page": 0,
        "ts": time.time(),
        "user_id": user_id,
        "chat_id": update.effective_chat.id,
    }

    r_html, p_html, markup = build_results_content(session_id, MOVIEBOX_CACHE[session_id])
    await edit_rich(context.bot, status.chat.id, status.message_id, r_html, p_html, markup)


moviebox_cmd.__name__ = "moviebox_cmd"
