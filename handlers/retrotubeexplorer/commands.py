"""Command handler explorer RetroTube: `/becekku` (`bq:`) dan `/lendirqu` (`lq:`).

FLOW COMMAND
------------
1. Gate premium + NSFW via `_premium_link_allowed(site["gate_url"], ...)`.
2. Tanpa argumen -> menu Explorer (Rich Message).
3. Dengan argumen -> search `/?s=<q>` (maks `MAX_RESULTS`), tampil sebagai
   listing Rich Message + tombol angka (pola sama dengan search command lain).
"""
import asyncio
import html
import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.dl.router import _premium_link_allowed, _premium_link_block_text
from handlers.join import require_join_or_block

from .constants import MAX_RESULTS, SITES
from .render import build_menu_content, build_results_content, menu_markup
from .rich import edit_rich, send_rich
from .scraper import search
from .state import SESSION_CACHE, purge_expired_cache

log = logging.getLogger(__name__)


def make_site_cmd(site_key: str):
    site = SITES[site_key]
    label = site["label"]
    prefix = site["prefix"]

    async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await require_join_or_block(update, context):
            return

        msg = update.message
        if not msg or not update.effective_user:
            return

        chat = update.effective_chat
        user_id = update.effective_user.id

        ok, reason = _premium_link_allowed(site["gate_url"], user_id, chat.id, chat.type)
        if not ok:
            return await msg.reply_text(_premium_link_block_text("single", reason), parse_mode="HTML")

        query = " ".join(context.args).strip() if context.args else ""
        purge_expired_cache()

        if not query:
            r_html, p_html = build_menu_content(label)
            return await send_rich(
                bot=context.bot,
                chat_id=msg.chat.id,
                reply_to=msg.message_id,
                rich_html=r_html,
                plain_html=p_html,
                markup=menu_markup(prefix),
            )

        log.info(
            "Search start | site=%s q=%r chat_id=%s user_id=%s",
            site_key, query, chat.id, user_id,
        )
        status = await msg.reply_text(
            f"🔍 Searching <code>{html.escape(query)}</code>...", parse_mode="HTML"
        )

        started = time.monotonic()
        results = await asyncio.to_thread(search, site, query, MAX_RESULTS)
        log.info(
            "Search done | site=%s q=%r results=%d elapsed=%.1fs",
            site_key, query, len(results), time.monotonic() - started,
        )
        if not results:
            return await status.edit_text(
                f"❌ No results for <code>{html.escape(query)}</code>.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data=f"{prefix}:menu:0:0")]]),
                parse_mode="HTML",
            )

        session_id = uuid.uuid4().hex[:8]
        SESSION_CACHE[session_id] = {
            "site": site_key,
            "title": f"{label} Search",
            "query": query,
            "results": results,
            "page": 0,
            "ts": time.time(),
            "user_id": user_id,
            "chat_id": chat.id,
        }

        r_html, p_html, markup = build_results_content(label, prefix, session_id, SESSION_CACHE[session_id])
        await edit_rich(context.bot, status.chat.id, status.message_id, r_html, p_html, markup)

    site_cmd.__name__ = f"{site_key}_cmd"
    return site_cmd
