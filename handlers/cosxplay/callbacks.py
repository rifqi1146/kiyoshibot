import asyncio
import logging
import time
import uuid

from telegram import Update
from telegram.ext import ContextTypes

from handlers.dl.router import (
    _metadata_status,
    _premium_link_allowed,
    _process_choice,
)
from handlers.dl.state import DL_CACHE
from utils.menu_lock import guard_callback_owner

from .constants import LABEL, MAX_RESULTS, PREFIX
from .render import (
    build_actors_content,
    build_categories_content,
    build_detail_content,
    build_menu_content,
    build_results_content,
    build_search_prompt_content,
    build_tags_content,
    menu_markup,
)
from .rich import edit_rich
from .scraper import (
    fetch_actor,
    fetch_actors,
    fetch_categories,
    fetch_category,
    fetch_detail,
    fetch_latest,
    fetch_tag,
    fetch_tags,
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


async def cosxplay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if action == "tags":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await q.answer("Loading tags...")
        tags = await asyncio.to_thread(fetch_tags)
        if not tags:
            return await q.answer("Failed to load tags.", show_alert=True)
        r_html, p_html, markup = build_tags_content(LABEL, PREFIX, page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "actors":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await q.answer("Loading models...")
        actors = await asyncio.to_thread(fetch_actors)
        if not actors:
            return await q.answer("Failed to load models.", show_alert=True)
        r_html, p_html, markup = build_actors_content(LABEL, PREFIX, page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "latest":
        await q.answer("Loading latest...")
        results = await asyncio.to_thread(fetch_latest, MAX_RESULTS)
        if not results:
            return await q.answer("No videos found.", show_alert=True)
        sid = _new_session(q.from_user.id, chat_id, f"{LABEL} Latest", results)
        r_html, p_html, markup = build_results_content(LABEL, PREFIX, sid, SESSION_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "cat":
        cat_path = parts[2] if len(parts) > 2 else ""
        if not cat_path:
            return await q.answer("Invalid category.", show_alert=True)
        picker_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        display = cat_path.rsplit("-", 1)[-1].replace("-", " ")
        await q.answer(f"Loading {display}...")
        results = await asyncio.to_thread(fetch_category, cat_path, MAX_RESULTS)
        if not results:
            return await q.answer("No videos found in this category.", show_alert=True)
        sid = _new_session(
            q.from_user.id, chat_id,
            f"{LABEL} — {display.title()}", results,
            origin={"action": "cats", "page": picker_page},
        )
        r_html, p_html, markup = build_results_content(LABEL, PREFIX, sid, SESSION_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "tag":
        slug = parts[2] if len(parts) > 2 else ""
        if not slug:
            return await q.answer("Invalid tag.", show_alert=True)
        picker_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await q.answer(f"Loading {slug}...")
        results = await asyncio.to_thread(fetch_tag, slug, MAX_RESULTS)
        if not results:
            return await q.answer("No videos found for this tag.", show_alert=True)
        sid = _new_session(
            q.from_user.id, chat_id,
            f"{LABEL} — {slug.replace('-', ' ')}", results,
            origin={"action": "tags", "page": picker_page},
        )
        r_html, p_html, markup = build_results_content(LABEL, PREFIX, sid, SESSION_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "actor":
        slug = parts[2] if len(parts) > 2 else ""
        if not slug:
            return await q.answer("Invalid model.", show_alert=True)
        picker_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        display = slug.split("-", 1)[-1].replace("-", " ")
        await q.answer(f"Loading {display}...")
        results = await asyncio.to_thread(fetch_actor, slug, MAX_RESULTS)
        if not results:
            return await q.answer("No videos found for this model.", show_alert=True)
        sid = _new_session(
            q.from_user.id, chat_id,
            f"{LABEL} — {display.title()}", results,
            origin={"action": "actors", "page": picker_page},
        )
        r_html, p_html, markup = build_results_content(LABEL, PREFIX, sid, SESSION_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if len(parts) < 3:
        return await q.answer()

    session_id = parts[2]
    data = SESSION_CACHE.get(session_id)
    if not data:
        return await q.answer("Session expired. Please run /cosxplay again.", show_alert=True)

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
            log.debug("Failed to edit CosXplay status | err=%r", e)

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


cosxplay_callback.__name__ = "cosxplay_callback"
