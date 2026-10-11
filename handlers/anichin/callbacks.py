"""Callback query handler explorer Anichin (`ac:` callbacks).

FLOW CALLBACK
-------------
1. `menu` / `prompt` / `close` — layar statis.
2. `genres:<page>` / `az:<page>` — picker genre / huruf A-Z (cache 1 jam).
3. `schedule:<day_index>` — jadwal per hari.
4. `list:<key>:<page>` / `genre:<slug>:<page>` / `azl:<letter>:<page>` —
   listing; sesi menyimpan `origin` supaya tombol Back balik ke picker asal.
5. `nav` / `back` — pindah halaman / balik ke listing.
6. `view:<sid>:<idx>` — fetch detail: series -> episode picker, episode -> format.
7. `eps` / `pick` / `dl` / `epdl` — unduh lewat `_process_choice` supaya
   resolution picker Anichin muncul (engine `anichin`).
"""
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

from .constants import BASE_URL, BROWSE, LABEL, MAX_RESULTS, PREFIX
from .render import (
    build_az_content,
    build_detail_content,
    build_episode_content,
    build_episodes_content,
    build_format_choice_content,
    build_genres_content,
    build_menu_content,
    build_results_content,
    build_schedule_content,
    build_search_prompt_content,
    menu_markup,
)
from .rich import edit_rich
from .scraper import (
    fetch_az,
    fetch_az_letter,
    fetch_detail,
    fetch_genres,
    fetch_listing,
    fetch_schedule,
)
from .state import ANICHIN_CACHE, purge_expired_cache, purge_index_cache

log = logging.getLogger(__name__)

GATE_URL = f"{BASE_URL}/"


def _new_session(user_id: int, chat_id: int, title: str, results: list, query: str = "", origin: dict | None = None) -> str:
    purge_expired_cache()
    sid = uuid.uuid4().hex[:8]
    ANICHIN_CACHE[sid] = {
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


async def anichin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data or not q.data.startswith(f"{PREFIX}:"):
        return

    bot = getattr(context, "bot", None) or (q.get_bot() if hasattr(q, "get_bot") else None)
    chat_id = q.message.chat.id
    message_id = q.message.message_id

    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    # ---------------- layar statis ----------------
    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("Closed.")
        return await q.answer()

    if action == "menu":
        r_html, p_html = build_menu_content()
        await edit_rich(bot, chat_id, message_id, r_html, p_html, menu_markup())
        return await q.answer()

    if action == "prompt":
        r_html, p_html, markup = build_search_prompt_content()
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    # ---------------- picker ----------------
    if action == "genres":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await q.answer("Loading genres...")
        await asyncio.to_thread(fetch_genres)
        r_html, p_html, markup = build_genres_content(page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "az":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await q.answer("Loading A–Z list...")
        purge_index_cache()
        letters = await asyncio.to_thread(fetch_az)
        if not letters:
            return await q.answer("Failed to load A–Z list.", show_alert=True)
        r_html, p_html, markup = build_az_content(page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "schedule":
        day_index = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
        await q.answer("Loading schedule...")
        await asyncio.to_thread(fetch_schedule)
        r_html, p_html, markup = build_schedule_content(day_index)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    # ---------------- listing ----------------
    if action == "list":
        key = parts[2] if len(parts) > 2 else "list"
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        meta = BROWSE.get(key)
        if not meta:
            return await q.answer("Invalid listing.", show_alert=True)
        title, path, paginated = meta
        await q.answer(f"Loading {title}...")
        results = await asyncio.to_thread(fetch_listing, path, paginated, MAX_RESULTS)
        if not results:
            return await q.answer("No titles found.", show_alert=True)
        sid = _new_session(q.from_user.id, chat_id, f"{LABEL} — {title}", results)
        ANICHIN_CACHE[sid]["page"] = page
        r_html, p_html, markup = build_results_content(sid, ANICHIN_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "genre":
        slug = parts[2] if len(parts) > 2 else ""
        picker_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        if not slug:
            return await q.answer("Invalid genre.", show_alert=True)
        await q.answer(f"Loading {slug}...")
        results = await asyncio.to_thread(
            fetch_listing, f"/genres/{slug}", True, MAX_RESULTS
        )
        if not results:
            return await q.answer("No titles found in this genre.", show_alert=True)
        sid = _new_session(q.from_user.id, chat_id, f"{LABEL} — {slug.replace('-', ' ').title()}", results,
                           origin={"action": "genres", "page": picker_page})
        r_html, p_html, markup = build_results_content(sid, ANICHIN_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "azl":
        letter = parts[2] if len(parts) > 2 else ""
        picker_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        if not letter:
            return await q.answer("Invalid letter.", show_alert=True)
        await q.answer(f"Loading {letter}...")
        results = await asyncio.to_thread(fetch_az_letter, letter, MAX_RESULTS)
        if not results:
            return await q.answer("No titles for this letter.", show_alert=True)
        sid = _new_session(q.from_user.id, chat_id, f"{LABEL} — {letter}", results,
                           origin={"action": "az", "page": picker_page})
        r_html, p_html, markup = build_results_content(sid, ANICHIN_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    # ---------------- operasi berbasis sesi ----------------
    if len(parts) < 3:
        return await q.answer()

    session_id = parts[2]
    data = ANICHIN_CACHE.get(session_id)
    if not data:
        return await q.answer("Session expired. Please run /anichin again.", show_alert=True)

    if q.from_user.id != data.get("user_id"):
        return await q.answer("This session does not belong to you.", show_alert=True)

    if action == "nav":
        data["page"] = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
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
        await q.answer("Loading details...")
        detail = await asyncio.to_thread(fetch_detail, target["url"])
        detail["url"] = target["url"]
        if not detail.get("title"):
            detail["title"] = target.get("title") or LABEL
        detail["idx"] = idx
        data["details"] = detail

        if detail.get("kind") == "series":
            r_html, p_html, markup = build_detail_content(detail, session_id, idx)
        elif detail.get("kind") == "episode":
            r_html, p_html, markup = build_episode_content(detail, session_id, idx)
        else:
            return await q.answer("Unable to open this post.", show_alert=True)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "eps":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        detail = data.get("details") or {}
        if detail.get("idx") != idx or detail.get("kind") != "series" or not detail.get("episodes"):
            await q.answer("Loading episodes...")
            fetched = await asyncio.to_thread(fetch_detail, results[idx]["url"])
            fetched["url"] = results[idx]["url"]
            fetched["idx"] = idx
            data["details"] = fetched
            detail = fetched
        else:
            await q.answer()

        if detail.get("kind") != "series" or not detail.get("episodes"):
            return await q.answer("No episodes available for this title.", show_alert=True)

        r_html, p_html, markup = build_episodes_content(detail, session_id, idx, page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "pick":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        episode = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        results = data.get("results") or []
        if idx < 0 or idx >= len(results) or episode <= 0:
            return await q.answer("Invalid selection.", show_alert=True)
        detail = data.get("details") or {}
        title = detail.get("title") or results[idx]["title"]
        r_html, p_html, markup = build_format_choice_content(
            title, session_id=session_id, idx=idx, episode=episode, direct=False
        )
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action in ("dl", "epdl"):
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        detail = data.get("details") or {}
        if action == "dl":
            episode = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            fmt_key = parts[5] if len(parts) > 5 and parts[5] in ("video", "mp3") else "video"
            if episode <= 0:
                return await q.answer("Invalid episode.", show_alert=True)
            ep_items = [e for e in (detail.get("episodes") or []) if e.get("ep") == episode]
            dl_url = ep_items[0]["url"] if ep_items else ""
        else:
            fmt_key = parts[4] if len(parts) > 4 and parts[4] in ("video", "mp3") else "video"
            dl_url = results[idx]["url"]

        if not dl_url:
            return await q.answer("Invalid download link.", show_alert=True)

        ok, reason = _premium_link_allowed(dl_url, q.from_user.id, q.message.chat.id, q.message.chat.type)
        if not ok:
            return await q.answer("Access denied.", show_alert=True)

        await q.answer("Downloading...")
        ANICHIN_CACHE.pop(session_id, None)

        try:
            await q.edit_message_text(text=_metadata_status(dl_url), parse_mode="HTML")
        except Exception as e:
            log.debug("Failed to edit Anichin status | err=%r", e)

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


anichin_callback.__name__ = "anichin_callback"
