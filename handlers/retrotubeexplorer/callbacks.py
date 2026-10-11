"""Callback query handler explorer RetroTube (`bq:` / `lq:` callbacks).

FLOW CALLBACK
-------------
1. `menu` / `close` — layar statis.
2. `cats:<page>` / `tags:<page>` — picker kategori / tag cloud (cache 1 jam).
3. `latest` / `viewed` / `cat:<slug>` / `tag:<slug>` — fetch listing lalu simpan
   sesi (`SESSION_CACHE[sid]`) dan render listing.
4. `nav:<sid>:<page>` / `back:<sid>` — pindah halaman / balik ke listing.
5. `info:<sid>:<idx>` — fetch detail post (judul, views, tanggal, tag) +
   tombol Download.
6. `dl:<sid>:<idx>` — unduh lewat `_start_dl_task` (RetroTube tidak punya
   resolution picker: `supports_resolution_picker()` False untuk host ini).

Format callback: `prefix:action:arg1:arg2` (4 segmen, sama seperti versi lama).
"""
import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from handlers.dl.router import (
    _metadata_status,
    _premium_link_allowed,
    _start_dl_task,
)

from .constants import MAX_RESULTS, SITES
from .render import (
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
    canonical_base,
    fetch_categories,
    fetch_detail,
    fetch_latest,
    fetch_paginated,
    fetch_tags,
)
from .state import SESSION_CACHE, purge_expired_cache

log = logging.getLogger(__name__)


def _new_session(site_key: str, user_id: int, chat_id: int, title: str, results: list, query: str = "", origin: dict | None = None) -> str:
    import uuid
    purge_expired_cache()
    sid = uuid.uuid4().hex[:8]
    SESSION_CACHE[sid] = {
        "site": site_key,
        "title": title,
        "query": query,
        "results": results,
        "page": 0,
        "ts": time.time(),
        "user_id": user_id,
        "chat_id": chat_id,
        # Dari mana listing dibuka (`{"action": "tags"/"cats", "page": N}`), dipakai
        # tombol Back supaya user balik ke daftar tag/kategori, bukan ke menu.
        "origin": origin or {},
    }
    return sid


def _handle_callback_for(site_key: str):
    site = SITES[site_key]
    label = site["label"]
    prefix = site["prefix"]

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q or not q.data:
            return

        parts = q.data.split(":")
        if len(parts) != 4 or parts[0] != prefix:
            return

        _, action, arg1, arg2 = parts
        bot = getattr(context, "bot", None) or (q.get_bot() if hasattr(q, "get_bot") else None)
        chat_id = q.message.chat.id
        message_id = q.message.message_id

        # ---------------- layar statis ----------------
        if action == "close":
            try:
                await q.message.delete()
            except Exception:
                await q.edit_message_text("Closed.")
            return await q.answer()

        if action == "menu":
            r_html, p_html = build_menu_content(label)
            await edit_rich(bot, chat_id, message_id, r_html, p_html, menu_markup(prefix))
            return await q.answer()

        if action == "prompt":
            r_html, p_html, markup = build_search_prompt_content(label, prefix)
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return await q.answer()

        # ---------------- picker ----------------
        if action == "cats":
            page = int(arg1) if arg1.isdigit() else 0
            await q.answer("Loading categories...")
            await asyncio.to_thread(fetch_categories, site)
            r_html, p_html, markup = build_categories_content(label, prefix, page)
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return

        if action == "tags":
            page = int(arg1) if arg1.isdigit() else 0
            await q.answer("Loading tags...")
            tags = await asyncio.to_thread(fetch_tags, site)
            if not tags:
                return await q.answer("Failed to load tags.", show_alert=True)
            r_html, p_html, markup = build_tags_content(label, prefix, page)
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return

        # ---------------- listing ----------------
        if action == "latest":
            await q.answer("Loading latest...")
            results = await asyncio.to_thread(fetch_latest, site, "latest", MAX_RESULTS)
            if not results:
                return await q.answer("No videos found.", show_alert=True)
            sid = _new_session(site_key, q.from_user.id, chat_id, f"{label} Latest", results)
            r_html, p_html, markup = build_results_content(label, prefix, sid, SESSION_CACHE[sid])
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return

        if action == "viewed":
            await q.answer("Loading most viewed...")
            results = await asyncio.to_thread(fetch_latest, site, "most-viewed", MAX_RESULTS)
            if not results:
                return await q.answer("No videos found.", show_alert=True)
            sid = _new_session(site_key, q.from_user.id, chat_id, f"{label} Most Viewed", results)
            r_html, p_html, markup = build_results_content(label, prefix, sid, SESSION_CACHE[sid])
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return

        if action == "cat":
            slug = arg1 or ""
            if not slug:
                return await q.answer("Invalid category.", show_alert=True)
            picker_page = int(arg2) if arg2.isdigit() else 0
            await q.answer(f"Loading {slug}...")
            results = await asyncio.to_thread(
                fetch_paginated, site, f"{canonical_base(site)}/category/{slug}/page/{{page}}/", MAX_RESULTS
            )
            if not results:
                return await q.answer("No videos found in this category.", show_alert=True)
            sid = _new_session(site_key, q.from_user.id, chat_id,
                               f"{label} — {slug.replace('-', ' ').title()}", results,
                               origin={"action": "cats", "page": picker_page})
            r_html, p_html, markup = build_results_content(label, prefix, sid, SESSION_CACHE[sid])
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return

        if action == "tag":
            slug = arg1 or ""
            if not slug:
                return await q.answer("Invalid tag.", show_alert=True)
            picker_page = int(arg2) if arg2.isdigit() else 0
            await q.answer(f"Loading {slug}...")
            results = await asyncio.to_thread(
                fetch_paginated, site, f"{canonical_base(site)}/tag/{slug}/page/{{page}}/", MAX_RESULTS
            )
            if not results:
                return await q.answer("No videos found for this tag.", show_alert=True)
            sid = _new_session(site_key, q.from_user.id, chat_id,
                               f"{label} — {slug.replace('-', ' ')}", results,
                               origin={"action": "tags", "page": picker_page})
            r_html, p_html, markup = build_results_content(label, prefix, sid, SESSION_CACHE[sid])
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return

        # ---------------- operasi berbasis sesi ----------------
        session_id = arg1
        data = SESSION_CACHE.get(session_id)
        if not data:
            return await q.answer("Session expired. Please run the command again.", show_alert=True)

        # Cache harus milik situs yang sama (tolak tombol silang antar situs).
        if data.get("site") != site_key:
            return await q.answer("Session expired. Please run the command again.", show_alert=True)

        if q.from_user.id != data.get("user_id"):
            return await q.answer("This session does not belong to you.", show_alert=True)

        if action == "nav":
            data["page"] = int(arg2) if arg2.isdigit() else 0
            r_html, p_html, markup = build_results_content(label, prefix, session_id, data)
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return await q.answer()

        if action == "back":
            r_html, p_html, markup = build_results_content(label, prefix, session_id, data)
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return await q.answer()

        if action == "info":
            idx = int(arg2) if arg2.isdigit() else -1
            results = data.get("results") or []
            if idx < 0 or idx >= len(results):
                return await q.answer("Invalid selection.", show_alert=True)

            target = results[idx]
            await q.answer("Loading details...")
            detail = await asyncio.to_thread(fetch_detail, site, target["url"])
            detail["url"] = target["url"]
            if not detail.get("title"):
                detail["title"] = target.get("title") or label
            data["details"] = detail

            r_html, p_html, markup = build_detail_content(label, prefix, detail, session_id, idx)
            await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
            return

        if action == "dl":
            idx = int(arg2) if arg2.isdigit() else -1
            results = data.get("results") or []
            if idx < 0 or idx >= len(results):
                return await q.answer("Invalid selection.", show_alert=True)

            url = results[idx]["url"]
            ok, reason = _premium_link_allowed(url, q.from_user.id, chat_id, q.message.chat.type)
            if not ok:
                return await q.answer("Access denied.", show_alert=True)

            await q.answer("Downloading...")
            SESSION_CACHE.pop(session_id, None)

            # Edit pesan listing/detail langsung jadi status download.
            try:
                await q.edit_message_text(text=_metadata_status(url), parse_mode="HTML")
            except Exception as e:
                log.debug("Failed to edit explorer message | err=%r", e)

            reply_to_id = q.message.reply_to_message.message_id if q.message.reply_to_message else None
            dl_data = {
                "url": url,
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
                fmt_key="video",
                status_ready=True,
            )

    _handler.__name__ = f"{site_key}_explorer_callback"
    return _handler


becekku_callback = _handle_callback_for("becekku")
lendirqu_callback = _handle_callback_for("lendirqu")
