"""Callback query handler untuk inline navigation Nekopoi (`nk:` callbacks).

FLOW CALLBACK
-------------
1. `nk:menu` / `nk:prompt:search` / `nk:close` — layar statis.
2. `nk:latest` / `nk:cat:<slug>` / `nk:genre:<slug>` — fetch listing lalu simpan
   sesi (`NEKOPOI_CACHE[sid]`) dan render listing.
3. `nk:cats:<page>` / `nk:genres:<page>` / `nk:az:<kind>:<page>` — picker.
4. `nk:azl:<kind>:<letter>:<page>` — daftar series satu huruf (dari cache A-Z).
5. `nk:schedule` — jadwal rilis (informasi saja).
6. `nk:view:<sid>:<idx>` — fetch detail:
   - `kind="series"` -> detail + tombol Download -> episode picker.
   - `kind="episode"` -> langsung format picker (Video/MP3).
7. `nk:eps` / `nk:pick` / `nk:dl` (series) dan `nk:epdl` (post langsung) —
   unduh lewat `_process_choice` supaya resolution picker Nekopoi muncul
   (JANGAN `_start_dl_task` langsung: itu diam-diam memilih resolusi tertinggi).
"""
import asyncio
import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.dl.router import (
    _metadata_status,
    _premium_link_allowed,
    _process_choice,
)
from handlers.dl.state import DL_CACHE

from .constants import BASE_URL, LABEL, MAX_RESULTS, PREFIX
from .render import (
    build_az_letters_content,
    build_categories_content,
    build_detail_content,
    build_episodes_content,
    build_format_choice_content,
    build_genres_content,
    build_menu_content,
    build_results_content,
    build_schedule_content,
    menu_markup,
)
from .rich import edit_rich
from .scraper import (
    fetch_az,
    fetch_detail,
    fetch_genres,
    fetch_latest,
    fetch_paginated,
    fetch_schedule,
)
from .state import NEKOPOI_CACHE, purge_az_cache, purge_expired_cache

log = logging.getLogger(__name__)

GATE_URL = f"{BASE_URL}/"


def _new_session(user_id: int, chat_id: int, title: str, results: list, query: str = "") -> str:
    purge_expired_cache()
    sid = uuid.uuid4().hex[:8]
    NEKOPOI_CACHE[sid] = {
        "title": title,
        "query": query,
        "results": results,
        "page": 0,
        "ts": time.time(),
        "user_id": user_id,
        "chat_id": chat_id,
    }
    return sid


async def nekopoi_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if action == "prompt" and len(parts) > 2 and parts[2] == "search":
        r_html = (
            "<h1>🔍 Search Nekopoi</h1>"
            "<p>To search, type your command with a keyword:</p>"
            "<pre><code>/nekopoi &lt;keyword&gt;</code></pre>"
            "<hr/>"
            "<aside>💡 Example: <code>/nekopoi elaina</code></aside>"
        )
        p_html = (
            "<b>🔍 Search Nekopoi</b>\n\n"
            "To search, send your command with a keyword:\n"
            "<code>/nekopoi &lt;keyword&gt;</code>\n\n"
            "Example:\n<code>/nekopoi elaina</code>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Back to Menu", callback_data=f"{PREFIX}:menu")]])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    # ---------------- picker ----------------
    if action == "cats":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        r_html, p_html, markup = build_categories_content(page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "genres":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await q.answer("Loading genres...")
        await asyncio.to_thread(fetch_genres)
        r_html, p_html, markup = build_genres_content(page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "az":
        kind = parts[2] if len(parts) > 2 and parts[2] in ("hentai", "jav") else "hentai"
        await q.answer("Loading A–Z list...")
        purge_az_cache()
        az = await asyncio.to_thread(fetch_az, kind)
        if not az.get("letters"):
            return await q.answer("Failed to load A–Z list.", show_alert=True)
        r_html, p_html, markup = build_az_letters_content(kind, az)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "azl":
        kind = parts[2] if len(parts) > 2 and parts[2] in ("hentai", "jav") else "hentai"
        letter = parts[3] if len(parts) > 3 else "#"
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        await q.answer("Loading titles...")
        az = await asyncio.to_thread(fetch_az, kind)
        items = (az.get("letters") or {}).get(letter) or []
        if not items:
            return await q.answer("No titles for this letter.", show_alert=True)
        results = [{"title": it["title"], "url": it["url"], "thumbnail": "", "genres": "", "desc": "",
                    "type": "series" if f"/{kind}/" in it["url"] else "post"} for it in items[:MAX_RESULTS]]
        label = "Hentai List" if kind == "hentai" else "JAV List"
        sid = _new_session(q.from_user.id, chat_id, f"{label} — {letter}", results)
        NEKOPOI_CACHE[sid]["page"] = page
        r_html, p_html, markup = build_results_content(sid, NEKOPOI_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "schedule":
        await q.answer("Loading schedule...")
        months = await asyncio.to_thread(fetch_schedule)
        r_html, p_html, markup = build_schedule_content(months)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    # ---------------- listing ----------------
    if action == "latest":
        await q.answer("Loading latest...")
        results = await asyncio.to_thread(fetch_latest)
        if not results:
            return await q.answer("No episodes found.", show_alert=True)
        sid = _new_session(q.from_user.id, chat_id, f"{LABEL} Latest Episodes", results)
        r_html, p_html, markup = build_results_content(sid, NEKOPOI_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "cat":
        slug = parts[2] if len(parts) > 2 else ""
        if not slug:
            return await q.answer("Invalid category.", show_alert=True)
        await q.answer(f"Loading {slug}...")
        results = await asyncio.to_thread(
            fetch_paginated, f"{BASE_URL}/category/{slug}/page/{{page}}/", MAX_RESULTS
        )
        if not results:
            return await q.answer("No titles found in this category.", show_alert=True)
        sid = _new_session(q.from_user.id, chat_id, f"{LABEL} — {slug.replace('-', ' ').title()}", results)
        r_html, p_html, markup = build_results_content(sid, NEKOPOI_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "genre":
        slug = parts[2] if len(parts) > 2 else ""
        if not slug:
            return await q.answer("Invalid genre.", show_alert=True)
        await q.answer(f"Loading {slug}...")
        results = await asyncio.to_thread(
            fetch_paginated, f"{BASE_URL}/genres/{slug}/page/{{page}}/", MAX_RESULTS
        )
        if not results:
            return await q.answer("No titles found in this genre.", show_alert=True)
        sid = _new_session(q.from_user.id, chat_id, f"{LABEL} — {slug.replace('-', ' ').title()}", results)
        r_html, p_html, markup = build_results_content(sid, NEKOPOI_CACHE[sid])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    # ---------------- operasi berbasis sesi ----------------
    if len(parts) < 3:
        return await q.answer()

    session_id = parts[2]
    data = NEKOPOI_CACHE.get(session_id)
    if not data:
        return await q.answer("Session expired. Please run /nekopoi again.", show_alert=True)

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
            detail["title"] = target.get("title") or "Nekopoi"
        detail["idx"] = idx
        data["details"] = detail

        if detail.get("kind") == "series":
            r_html, p_html, markup = build_detail_content(detail, session_id, idx)
        elif detail.get("kind") == "episode":
            r_html, p_html, markup = build_format_choice_content(
                detail["title"], session_id=session_id, idx=idx, direct=True
            )
        else:
            return await q.answer("Unable to open this post (no downloadable stream).", show_alert=True)
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
        NEKOPOI_CACHE.pop(session_id, None)

        try:
            await q.edit_message_text(text=_metadata_status(dl_url), parse_mode="HTML")
        except Exception as e:
            log.debug("Failed to edit Nekopoi status | err=%r", e)

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


nekopoi_callback.__name__ = "nekopoi_callback"
