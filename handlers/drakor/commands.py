"""Command handler `/drakorid`."""
import asyncio
import html
import logging
import time
import uuid
from urllib.parse import quote_plus
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database.download_db import is_premium_user
from handlers.join import require_join_or_block

from .constants import BASE_URL, MAX_RESULTS, PREFIX
from .render import build_menu_content, build_results_content, menu_markup
from .rich import edit_rich, send_rich
from .scraper import http_get, parse_cards
from .state import DRAKOR_CACHE, purge_expired_cache

log = logging.getLogger(__name__)


async def drakorid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main command handler for /drakorid."""
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
        return await send_rich(
            bot=context.bot,
            chat_id=msg.chat.id,
            reply_to=msg.message_id,
            rich_html=r_html,
            plain_html=p_html,
            markup=menu_markup(),
        )

    log.info("Search start | site=drakorid q=%r chat_id=%s user_id=%s", query, update.effective_chat.id, user_id)
    status = await msg.reply_text(
        f"🔍 Searching <code>{html.escape(query)}</code>...",
        parse_mode="HTML",
    )

    started = time.monotonic()
    raw = await asyncio.to_thread(http_get, f"{BASE_URL}/cari.html?q={quote_plus(query)}")
    results = parse_cards(raw)[:MAX_RESULTS]
    log.info(
        "Search done | site=drakorid q=%r results=%d elapsed=%.1fs",
        query, len(results), time.monotonic() - started,
    )

    if not results:
        return await status.edit_text(
            f"❌ No results found for <code>{html.escape(query)}</code>.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu")]]),
            parse_mode="HTML",
        )

    session_id = uuid.uuid4().hex[:8]
    DRAKOR_CACHE[session_id] = {
        "title": "Drakor.id Search",
        "query": query,
        "results": results,
        "page": 0,
        "ts": time.time(),
        "user_id": user_id,
        "chat_id": update.effective_chat.id,
    }

    r_html, p_html, markup = build_results_content(session_id, DRAKOR_CACHE[session_id])
    await edit_rich(context.bot, status.chat.id, status.message_id, r_html, p_html, markup)


drakorid_cmd.__name__ = "drakorid_cmd"
