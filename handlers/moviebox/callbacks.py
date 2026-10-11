"""Callback query handler for MovieBox Explorer UI (`mb:` callbacks).

Callback map (4 segments):
  menu:0:0                     -> main menu
  prompt:0:0                   -> search instruction screen
  sections:<page>:0            -> all-sections browser
  sec:<key>:<page>             -> open one section (trending/home/filter)
  popular:0:0                  -> curated quick-search titles
  popsearch:<index>            -> search one curated title
  nav:<sid>:<page>             -> results pagination
  back:<sid>:0                 -> back to results
  view:<sid>:<idx>             -> detail view
  seasons:<sid>:<idx>:<sidx>:<page>       -> episode picker
  dlchoice:<sid>:<idx>:<season>:<episode> -> format picker
  dl:<sid>:<idx>:<season>:<episode>:<fmt> -> start download
  close:0:0                    -> delete message
"""
import asyncio
import logging
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.dl.router import _premium_link_allowed, _process_choice
from handlers.dl.state import DL_CACHE

from .constants import BASE_URL, LABEL, MAX_RESULTS, PER_PAGE, POPULAR_TITLES, PREFIX, SECTIONS
from .render import (
    build_all_sections_content,
    build_detail_content,
    build_download_choice_content,
    build_episodes_content,
    build_menu_content,
    build_popular_content,
    build_results_content,
    menu_markup,
)
from .rich import edit_rich
from .scraper import fetch_detail, fetch_section_items, search
from .state import MOVIEBOX_CACHE

log = logging.getLogger(__name__)


def _make_session(data: dict, results: list[dict]) -> str:
    session_id = uuid.uuid4().hex[:8]
    MOVIEBOX_CACHE[session_id] = data
    return session_id


async def moviebox_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler for MovieBox inline navigation."""
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
            "<h1>🔍 Search MovieBox</h1>"
            "<p>To search for movies or TV series, send your command:</p>"
            "<pre><code>/moviebox &lt;title&gt;</code></pre>"
            "<hr/>"
            "<aside>💡 Examples: <code>/moviebox avatar</code> or <code>/moviebox stranger things</code></aside>"
        )
        plain_html = (
            "<b>🔍 Search MovieBox</b>\n\n"
            "To search for movies or TV series, send:\n"
            "<code>/moviebox &lt;title&gt;</code>\n\n"
            "Examples:\n"
            "• <code>/moviebox avatar</code>\n"
            "• <code>/moviebox stranger things</code>"
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

    if action == "sections":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        r_html, p_html, markup = build_all_sections_content(page)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "popular":
        r_html, p_html, markup = build_popular_content()
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "popsearch":
        idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        if idx < 0 or idx >= len(POPULAR_TITLES):
            return await q.answer("Invalid title.", show_alert=True)
        query = POPULAR_TITLES[idx]
        await q.answer(f"Searching {query}...")
        results = await asyncio.to_thread(search, query)
        if not results:
            return await q.answer(f"No results for {query}.", show_alert=True)

        session_id = _make_session(
            {
                "title": f"{LABEL} Search: {query}",
                "query": query,
                "results": results[:MAX_RESULTS],
                "page": 0,
                "ts": time.time(),
                "user_id": q.from_user.id,
                "chat_id": q.message.chat.id,
            },
            results,
        )
        r_html, p_html, markup = build_results_content(session_id, MOVIEBOX_CACHE[session_id])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "sec":
        key = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        if key not in SECTIONS:
            return await q.answer("Unknown section.", show_alert=True)

        label = SECTIONS[key][0]
        await q.answer(f"Loading {label}...")
        results = await asyncio.to_thread(fetch_section_items, key, 1, 50)
        if not results:
            return await q.answer(f"No titles found in {label}.", show_alert=True)

        session_id = _make_session(
            {
                "title": f"{LABEL} {label}",
                "query": "",
                "results": results[:MAX_RESULTS],
                "page": 0,
                "ts": time.time(),
                "user_id": q.from_user.id,
                "chat_id": q.message.chat.id,
                "section": key,
                "api_page": 1,
                "has_more": len(results) >= 50,
            },
            results,
        )
        r_html, p_html, markup = build_results_content(session_id, MOVIEBOX_CACHE[session_id])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    # Operations requiring session_id
    if len(parts) < 3:
        return await q.answer()

    session_id = parts[2]
    data = MOVIEBOX_CACHE.get(session_id)
    if not data:
        return await q.answer("Session expired. Please run /moviebox again.", show_alert=True)

    if q.from_user.id != data.get("user_id"):
        return await q.answer("This session does not belong to you.", show_alert=True)

    if action == "nav":
        target_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        results = data.get("results") or []
        max_page = max(1, (len(results) + PER_PAGE - 1) // PER_PAGE)
        # Section browsing: lazily fetch the next API page when the user
        # paginates past the currently loaded chunk.
        section = data.get("section")
        if section and target_page >= max_page and data.get("has_more"):
            await q.answer("Loading more...")
            api_page = int(data.get("api_page") or 1) + 1
            more = await asyncio.to_thread(fetch_section_items, section, api_page, 50)
            if more:
                seen = {(x.get("subject_id") or x.get("slug") or x.get("title")) for x in results}
                added = 0
                for it in more:
                    key = it.get("subject_id") or it.get("slug") or it.get("title")
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(it)
                    added += 1
                data["results"] = results[:MAX_RESULTS]
                data["api_page"] = api_page
                data["has_more"] = (len(more) >= 50) and (len(data["results"]) < MAX_RESULTS)
            else:
                data["has_more"] = False
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
        await q.answer("Loading details...")
        slug = target.get("slug") or target.get("subject_id") or ""
        details = await asyncio.to_thread(fetch_detail, slug)
        if not details:
            details = {
                "title": target.get("title") or "Title",
                "raw_title": target.get("title") or "Title",
                "thumbnail": target.get("thumbnail") or "",
                "description": "No details available.",
                "url": target.get("url") or "",
                "is_tv": target.get("is_tv", False),
                "type": target.get("type", "Movie"),
                "seasons": [],
            }
        details["idx"] = idx
        data["details"] = details

        r_html, p_html, markup = build_detail_content(details, session_id, idx)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "seasons":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        s_idx = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        page = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        details = data.get("details") or {}
        if details.get("idx") != idx:
            slug = results[idx].get("slug") or results[idx].get("subject_id") or ""
            details = await asyncio.to_thread(fetch_detail, slug)
            details["idx"] = idx
            data["details"] = details

        seasons = details.get("seasons") or []
        r_html, p_html, markup = build_episodes_content(
            details.get("title") or results[idx]["title"],
            seasons,
            session_id,
            idx,
            s_idx,
            page,
        )
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "dlchoice":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        season = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        episode = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        details = data.get("details") or {}
        is_tv = details.get("is_tv", False)
        title = details.get("title") or results[idx]["title"]
        r_html, p_html, markup = build_download_choice_content(
            title, session_id, idx, season, episode, is_tv
        )
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "dl":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        season = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        episode = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        fmt_key = parts[6] if len(parts) > 6 and parts[6] in ("video", "mp3") else "video"
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        details = data.get("details") or {}
        slug = details.get("slug") or results[idx].get("slug") or ""
        sid = details.get("subject_id") or results[idx].get("subject_id") or ""
        is_tv = details.get("is_tv", False)

        dl_url = (
            f"{BASE_URL}/movies/{slug}?id={sid}"
            f"&type={'/tv/detail' if is_tv else '/movie/detail'}"
            f"&detailSe={season}&detailEp={episode}&lang=en"
        )
        ok, reason = _premium_link_allowed(dl_url, q.from_user.id, q.message.chat.id, q.message.chat.type)
        if not ok:
            return await q.answer("Access denied.", show_alert=True)

        await q.answer("Loading video resolutions...")

        try:
            await q.edit_message_text(
                text=f"<b>Scraping {LABEL} metadata...</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            log.debug("Failed to edit MovieBox status | err=%r", e)

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
            "season": season,
            "episode": episode,
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


moviebox_callback.__name__ = "moviebox_callback"
