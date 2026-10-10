"""Callback query handler untuk inline navigation Drakor.id (`dk:` callbacks)."""
import asyncio
import logging
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.dl.router import _premium_link_allowed, _process_choice
from handlers.dl.state import DL_CACHE

from .constants import BASE_URL, LABEL, MAX_RESULTS, PREFIX
from .render import (
    build_categories_content,
    build_detail_content,
    build_download_choice_content,
    build_episodes_content,
    build_menu_content,
    build_results_content,
    menu_markup,
)
from .rich import edit_rich
from .scraper import (
    fetch_categories,
    fetch_detail,
    fetch_paginated,
    slug_from_url,
)
from .state import DRAKOR_CACHE

log = logging.getLogger(__name__)


async def drakorid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler for Drakor.id inline navigation."""
    q = update.callback_query
    if not q or not q.data or not q.data.startswith(f"{PREFIX}:"):
        return

    bot = getattr(context, "bot", None) or (q.get_bot() if hasattr(q, "get_bot") else None)
    chat_id = q.message.chat.id
    message_id = q.message.message_id

    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("Closed.")
        return await q.answer()

    if action == "prompt" and len(parts) > 2 and parts[2] == "search":
        rich_html = (
            "<h1>🔍 Search Drakor.id</h1>"
            "<p>To search, type your command with a title keyword:</p>"
            "<pre><code>/drakorid &lt;title&gt;</code></pre>"
            "<hr/>"
            "<aside>💡 Example: <code>/drakorid queen</code></aside>"
        )
        plain_html = (
            "<b>🔍 Search Drakor.id</b>\n\n"
            "To search, send your command with a keyword:\n"
            "<code>/drakorid &lt;title&gt;</code>\n\n"
            "Example:\n"
            "<code>/drakorid queen</code>"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Menu", callback_data=f"{PREFIX}:menu")]
        ])
        await edit_rich(bot, chat_id, message_id, rich_html, plain_html, markup)
        return await q.answer()

    if action == "menu":
        r_html, p_html = build_menu_content()
        await edit_rich(bot, chat_id, message_id, r_html, p_html, menu_markup())
        return await q.answer()

    if action == "cats":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await q.answer("Loading categories...")
        await asyncio.to_thread(fetch_categories)
        r_html, p_html, markup = build_categories_content(page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "latest":
        await q.answer("Loading latest...")
        results = await asyncio.to_thread(fetch_paginated, f"{BASE_URL}/list/{{page}}", MAX_RESULTS)
        if not results:
            return await q.answer("No dramas found.", show_alert=True)

        session_id = uuid.uuid4().hex[:8]
        DRAKOR_CACHE[session_id] = {
            "title": "Drakor.id Latest",
            "query": "",
            "results": results,
            "page": 0,
            "ts": time.time(),
            "user_id": q.from_user.id,
            "chat_id": q.message.chat.id,
        }
        r_html, p_html, markup = build_results_content(session_id, DRAKOR_CACHE[session_id])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "cat":
        slug = parts[2] if len(parts) > 2 else ""
        if not slug:
            return await q.answer("Invalid category.", show_alert=True)

        await q.answer(f"Loading {slug}...")
        results = await asyncio.to_thread(fetch_paginated, f"{BASE_URL}/kategori/{slug}/{{page}}", MAX_RESULTS)
        if not results:
            return await q.answer("No dramas found in this category.", show_alert=True)

        category_name = slug.replace("-", " ").title()
        session_id = uuid.uuid4().hex[:8]
        DRAKOR_CACHE[session_id] = {
            "title": f"Drakor.id {category_name}",
            "query": "",
            "results": results,
            "page": 0,
            "ts": time.time(),
            "user_id": q.from_user.id,
            "chat_id": q.message.chat.id,
        }
        r_html, p_html, markup = build_results_content(session_id, DRAKOR_CACHE[session_id])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    # Operations requiring session_id
    if len(parts) < 3:
        return await q.answer()

    session_id = parts[2]
    data = DRAKOR_CACHE.get(session_id)
    if not data:
        return await q.answer("Session expired. Please run /drakorid again.", show_alert=True)

    if q.from_user.id != data.get("user_id"):
        return await q.answer("This session does not belong to you.", show_alert=True)

    if action == "nav":
        target_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        data["page"] = target_page
        r_html, p_html, markup = build_results_content(session_id, data)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "back":
        r_html, p_html, markup = build_results_content(session_id, data)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "view":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        target = results[idx]
        await q.answer("Loading drama details...")
        details = await asyncio.to_thread(fetch_detail, target["url"])
        if not details:
            details = {
                "title": target.get("title") or "Drama Details",
                "poster": target.get("thumbnail") or "",
                "synopsis": "No details available.",
                "url": target.get("url"),
                "episodes": 0,
            }
        details["idx"] = idx
        data["details"] = details

        r_html, p_html, markup = build_detail_content(details, session_id, idx)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "eps":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        details = data.get("details") or {}
        if details.get("idx") != idx or not details.get("episodes"):
            await q.answer("Loading episodes...")
            fetched = await asyncio.to_thread(fetch_detail, results[idx]["url"])
            fetched["idx"] = idx
            data["details"] = fetched
            details = fetched
        else:
            await q.answer()

        total_eps = int(details.get("episodes") or 0)
        if total_eps <= 0:
            return await q.answer("No episodes available for this title.", show_alert=True)

        r_html, p_html, markup = build_episodes_content(
            details.get("title") or results[idx]["title"], total_eps, session_id, idx, page
        )
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "pick":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        episode = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        results = data.get("results") or []
        if idx < 0 or idx >= len(results) or episode <= 0:
            return await q.answer("Invalid selection.", show_alert=True)

        title = results[idx]["title"]
        r_html, p_html, markup = build_download_choice_content(title, session_id, idx, episode)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "dl":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        episode = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        fmt_key = parts[5] if len(parts) > 5 and parts[5] in ("video", "mp3") else "video"
        results = data.get("results") or []
        if idx < 0 or idx >= len(results) or episode <= 0:
            return await q.answer("Invalid selection.", show_alert=True)

        slug = slug_from_url(results[idx]["url"])
        if not slug:
            return await q.answer("Invalid drama URL.", show_alert=True)

        dl_url = f"{BASE_URL}/nonton/{slug}/{episode}"
        ok, reason = _premium_link_allowed(dl_url, q.from_user.id, q.message.chat.id, q.message.chat.type)
        if not ok:
            return await q.answer("Access denied.", show_alert=True)

        await q.answer("Downloading...")

        try:
            await q.edit_message_text(
                text=f"<b>Scraping {LABEL} metadata...</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            log.debug("Failed to edit Drakor.id status | err=%r", e)

        reply_to_id = q.message.reply_to_message.message_id if q.message.reply_to_message else None
        dl_data = {
            "url": dl_url,
            "user": q.from_user.id,
            "chat_id": q.message.chat.id,
            "chat_type": q.message.chat.type,
            "reply_to": reply_to_id,
            "message_thread_id": getattr(q.message, "message_thread_id", None),
            "ts": time.time(),
            "msg_date": time.time(),
        }

        dl_id = uuid.uuid4().hex[:8]
        DL_CACHE[dl_id] = dl_data
        return await _process_choice(
            context=context,
            message=q.message,
            dl_id=dl_id,
            data=dl_data,
            choice=fmt_key,
            user_id=q.from_user.id,
            status_ready=True,
        )


drakorid_callback.__name__ = "drakorid_callback"
