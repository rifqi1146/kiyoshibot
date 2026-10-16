import asyncio
import logging
import time
import uuid

from telegram import Update
from telegram.ext import ContextTypes

from handlers.dl.router import (
    _metadata_status,
    _premium_link_allowed,
    _start_dl_task,
)
from utils.menu_lock import guard_callback_owner

from .constants import LABEL, MAX_RESULTS, PREFIX
from .render import (
    build_categories_content,
    build_detail_content,
    build_menu_content,
    build_results_content,
    build_search_prompt_content,
    menu_markup,
)
from .rich import edit_rich
from .scraper import (
    fetch_categories,
    fetch_detail,
    fetch_genre,
    fetch_section,
    search,
)
from .state import SESSION_CACHE, purge_expired_cache

log = logging.getLogger(__name__)


def _new_session(user_id: int, chat_id: int, title: str, results: list, query: str = "", origin: dict | None = None) -> str:
    purge_expired_cache()
    sid = uuid.uuid4().hex[:8]
    SESSION_CACHE[sid] = {
        "title": title,
        "query": query,
        "results": results,
        "page": 0,
        "ts": time.time(),
        "user_id": user_id,
        "chat_id": chat_id,
        "origin": origin or {},
    }
    return sid


async def simontok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data or not q.data.startswith(f"{PREFIX}:"):
        return

    if not await guard_callback_owner(q):
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

    if action == "menu":
        r_html, p_html = build_menu_content(LABEL)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, menu_markup(PREFIX))
        return await q.answer()

    if action == "prompt":
        r_html, p_html, markup = build_search_prompt_content(LABEL, PREFIX)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "cats":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await q.answer("Loading categories...")
        await asyncio.to_thread(fetch_categories)
        r_html, p_html, markup = build_categories_content(LABEL, PREFIX, page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "sec":
        slug = parts[2] if len(parts) > 2 else ""
        if slug not in ("terbaru", "trending", "vip", "indo", "jav", "semi", "hentai"):
            return await q.answer("Invalid section.", show_alert=True)
        await q.answer("Loading...")
        results = await asyncio.to_thread(fetch_section, slug, MAX_RESULTS)
        if not results:
            return await q.answer("No videos found.", show_alert=True)
        titles = {
            "terbaru": "Latest Videos", "trending": "Trending Videos", "vip": "VIP 18+",
            "indo": "Bokep Indo", "jav": "JAV Sub Indo", "semi": "Film Semi", "hentai": "Hentai",
        }
        sid = _new_session(q.from_user.id, chat_id, f"{LABEL} — {titles.get(slug, slug)}", results)
        r_html, p_html, markup = build_results_content(LABEL, PREFIX, sid, SESSION_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "cat":
        slug = parts[2] if len(parts) > 2 else ""
        if not slug:
            return await q.answer("Invalid category.", show_alert=True)
        picker_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await q.answer(f"Loading {slug}...")
        results = await asyncio.to_thread(fetch_genre, slug, MAX_RESULTS)
        if not results:
            return await q.answer("No videos found in this category.", show_alert=True)
        sid = _new_session(
            q.from_user.id, chat_id,
            f"{LABEL} — {slug.replace('-', ' ').title()}", results,
            origin={"action": "cats", "page": picker_page},
        )
        r_html, p_html, markup = build_results_content(LABEL, PREFIX, sid, SESSION_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if len(parts) < 3:
        return await q.answer()

    session_id = parts[2]
    data = SESSION_CACHE.get(session_id)
    if not data:
        return await q.answer("Session expired. Please run /simontok again.", show_alert=True)

    if q.from_user.id != data.get("user_id"):
        return await q.answer("This session does not belong to you.", show_alert=True)

    if action == "nav":
        data["page"] = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        r_html, p_html, markup = build_results_content(LABEL, PREFIX, session_id, data)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "back":
        r_html, p_html, markup = build_results_content(LABEL, PREFIX, session_id, data)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "info":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        target = results[idx]
        await q.answer("Loading details...")
        detail = await asyncio.to_thread(fetch_detail, target["url"])
        detail["url"] = target["url"]
        if not detail.get("title"):
            detail["title"] = target.get("title") or LABEL
        data["details"] = detail

        r_html, p_html, markup = build_detail_content(LABEL, PREFIX, detail, session_id, idx)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "dl":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        fmt_key = parts[4] if len(parts) > 4 and parts[4] in ("video", "mp3") else "video"
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        dl_url = results[idx]["url"]
        ok, reason = _premium_link_allowed(dl_url, q.from_user.id, chat_id, q.message.chat.type)
        if not ok:
            return await q.answer("Access denied.", show_alert=True)

        await q.answer("Downloading...")
        SESSION_CACHE.pop(session_id, None)

        try:
            await q.edit_message_text(text=_metadata_status(dl_url), parse_mode="HTML")
        except Exception as e:
            log.debug("Failed to edit Simontok status | err=%r", e)

        reply_to_id = q.message.reply_to_message.message_id if q.message.reply_to_message else None
        dl_data = {
            "url": dl_url,
            "user": q.from_user.id,
            "chat_id": chat_id,
            "chat_type": q.message.chat.type,
            "reply_to": reply_to_id,
            "message_thread_id": getattr(q.message, "message_thread_id", None),
            "ts": time.time(),
            "msg_date": time.time(),
        }

        return await _start_dl_task(
            context=context,
            message=q.message,
            data=dl_data,
            fmt_key=fmt_key,
            status_ready=True,
        )


simontok_callback.__name__ = "simontok_callback"
