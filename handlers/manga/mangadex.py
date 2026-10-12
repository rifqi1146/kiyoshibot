"""MangaDex API client + builder UI + callback handler.

Menangani fitur MangaDex:
- Search manga & paginasi (`msearch_`)
- Detail manga & daftar chapter (`detailmanga_`)
- Reader chapter & navigasi halaman (`readmanga_`, `switchch_`, `nav_`)
- Unduh PDF (`pdfdex_`)
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .constants import MANGADEX_API, UPLOADS_URL
from .http import _escape, fetch_image_bytes, fetch_json, safe_render_page
from .pdf import send_pdf_task
from .state import _move_message_lock, _set_message_lock, _udata_get, _udata_set

log = logging.getLogger(__name__)


async def get_chapter_context(chapter_id: str, context):
    cache_key = f"ctx_{chapter_id}"
    cached = _udata_get(context, cache_key)
    if cached is not None:
        return cached
    ch_data = await fetch_json(f"{MANGADEX_API}/chapter/{chapter_id}?includes[]=manga")
    if not ch_data or "data" not in ch_data or "attributes" not in ch_data["data"]:
        return None, None, "Unknown", "??", "??"
    manga = next((rel for rel in ch_data["data"].get("relationships", []) if rel.get("type") == "manga"), None)
    title = manga["attributes"]["title"].get("en", manga["attributes"]["title"].get("ja-ro", "Unknown Title")) if manga and "attributes" in manga and "title" in manga["attributes"] else "Unknown"
    ch_num = ch_data["data"]["attributes"].get("chapter") or "Oneshot"
    lang = ch_data["data"]["attributes"].get("translatedLanguage", "??").upper()
    manga_id = manga["id"] if manga else None
    prev_id, next_id = None, None
    if manga_id:
        feed_data = await fetch_json(
            f"{MANGADEX_API}/manga/{manga_id}/feed",
            params={"translatedLanguage[]": [lang.lower()], "limit": 500, "order[chapter]": "asc"}
        )
        if feed_data and feed_data.get("data"):
            chapters = feed_data["data"]
            for i, ch in enumerate(chapters):
                if ch["id"] == chapter_id:
                    if i > 0:
                        prev_id = chapters[i - 1]["id"]
                    if i < len(chapters) - 1:
                        next_id = chapters[i + 1]["id"]
                    break
    res = (prev_id, next_id, title, ch_num, lang)
    _udata_set(context, cache_key, res)
    return res


def get_nav_keyboard(chapter_id: str, current_idx: int, total_pages: int, prev_ch: str = None, next_ch: str = None):
    page_nav = []
    if current_idx > 0:
        page_nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nav_{chapter_id}_{current_idx - 1}"))
    page_nav.append(InlineKeyboardButton(f"📄 {current_idx + 1}/{total_pages}", callback_data="ignore"))
    if current_idx < total_pages - 1:
        page_nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"nav_{chapter_id}_{current_idx + 1}"))
    chapter_nav = []
    if prev_ch:
        chapter_nav.append(InlineKeyboardButton("⏪ Ch Prev", callback_data=f"switchch_{prev_ch}"))
    chapter_nav.append(InlineKeyboardButton("❌ Close", callback_data="close_manga"))
    if next_ch:
        chapter_nav.append(InlineKeyboardButton("Ch Next ⏩", callback_data=f"switchch_{next_ch}"))
    return InlineKeyboardMarkup([page_nav, chapter_nav])


async def build_search_list(query: str, offset: int, context):
    search_url = f"{MANGADEX_API}/manga"
    params = {"title": query, "limit": 5, "offset": offset, "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"]}
    data = await fetch_json(search_url, params)
    if not data or not data.get("data"):
        return None, None
    keyboard = []
    for manga in data["data"]:
        title = manga["attributes"]["title"].get("en", manga["attributes"]["title"].get("ja-ro", "Unknown"))
        _udata_set(context, "last_manga_query", query)
        keyboard.append([InlineKeyboardButton(f"📖 {title[:35]}", callback_data=f"detailmanga_{manga['id']}_0")])
    nav_buttons = []
    if offset > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"msearch_{offset - 5}"))
    if data["total"] > offset + 5:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"msearch_{offset + 5}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_manga")])
    return f"🔍 MangaDex results: <b>{_escape(query)}</b> ({(offset // 5) + 1})", InlineKeyboardMarkup(keyboard)


async def handle_mangadex_callback(query, context, data: str):
    if data.startswith("msearch_"):
        offset = int(data.split("_")[1])
        q = _udata_get(context, "last_manga_query", "")
        if not q:
            return await query.answer("❌ Search session expired.", show_alert=True)
        text, markup = await build_search_list(q, offset, context)
        if text:
            if query.message.photo:
                await query.message.delete()
                sent = await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    message_thread_id=query.message.message_thread_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
                _move_message_lock(query.message.chat_id, query.message.message_id, sent.chat_id, sent.message_id, fallback_user_id=query.from_user.id)
            else:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

    elif data.startswith("detailmanga_"):
        parts = data.split("_")
        manga_id = parts[1]
        offset = int(parts[2]) if len(parts) > 2 else 0
        await query.answer("Loading chapter list...")
        m_data = await fetch_json(f"{MANGADEX_API}/manga/{manga_id}?includes[]=cover_art")
        if not m_data:
            return await query.answer("❌ Server error.")
        manga = m_data["data"]
        title = manga["attributes"]["title"].get("en", manga["attributes"]["title"].get("ja-ro", "Unknown"))
        desc = manga["attributes"]["description"].get("en", "No description available.")[:250] + "..."
        cover_file = next((rel["attributes"]["fileName"] for rel in manga["relationships"] if rel["type"] == "cover_art"), None)
        cover_url = f"{UPLOADS_URL}/covers/{manga_id}/{cover_file}" if cover_file else None
        feed = await fetch_json(f"{MANGADEX_API}/manga/{manga_id}/feed", {
            "translatedLanguage[]": ["en", "id"],
            "limit": 10,
            "offset": offset,
            "order[chapter]": "desc"
        })
        keyboard = []
        if feed and feed.get("data"):
            for ch in feed["data"]:
                ch_num = ch["attributes"]["chapter"] or "Oneshot"
                lang = ch["attributes"]["translatedLanguage"].upper()
                keyboard.append([
                    InlineKeyboardButton(f"📖 Ch.{ch_num} [{lang}]", callback_data=f"readmanga_{ch['id']}"),
                    InlineKeyboardButton("⬇️ PDF", callback_data=f"pdfdex_{ch['id']}")
                ])
        nav_buttons = []
        if offset > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Newer", callback_data=f"detailmanga_{manga_id}_{offset - 10}"))
        if feed and feed.get("total", 0) > offset + 10:
            nav_buttons.append(InlineKeyboardButton("Older ➡️", callback_data=f"detailmanga_{manga_id}_{offset + 10}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔙 Return to search", callback_data="msearch_0")])
        keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_manga")])
        caption = f"📚 <b>{_escape(title)}</b>\n\n📝 {_escape(desc)}"
        if query.message.photo:
            await query.edit_message_caption(caption=caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
            _set_message_lock(query.message.chat_id, query.message.message_id, query.from_user.id)
        else:
            if cover_url:
                await query.message.delete()
                sent = await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    message_thread_id=query.message.message_thread_id,
                    photo=cover_url,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                _move_message_lock(query.message.chat_id, query.message.message_id, sent.chat_id, sent.message_id, fallback_user_id=query.from_user.id)
            else:
                await query.edit_message_text(caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
                _set_message_lock(query.message.chat_id, query.message.message_id, query.from_user.id)

    elif data.startswith("readmanga_") or data.startswith("switchch_"):
        await query.answer("Downloading page... ⏳")
        chapter_id = data.split("_")[1]
        server_data = await fetch_json(f"{MANGADEX_API}/at-home/server/{chapter_id}")
        chapter_data = server_data.get("chapter", {}) if server_data else {}
        if not server_data or not chapter_data.get("data"):
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Failed to load chapter."
            )
        prev_ch, next_ch, m_title, m_ch, m_lang = await get_chapter_context(chapter_id, context)
        base_url = server_data.get("baseUrl", "")
        chapter_hash = server_data.get("chapter", {}).get("hash", "")
        urls = [f"{base_url}/data/{chapter_hash}/{p}" for p in server_data.get("chapter", {}).get("data", [])]
        _udata_set(context, f"manga_{chapter_id}", urls)
        keyboard = get_nav_keyboard(chapter_id, 0, len(urls), prev_ch, next_ch)
        caption_text = f"📖 <b>{_escape(m_title)}</b> | Ch:{_escape(m_ch)} | 🌐 {_escape(m_lang)}"
        img_bytes = await fetch_image_bytes(urls[0])
        if not img_bytes:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Failed to download image."
            )
        is_edit = data.startswith("switchch_") or hasattr(query, "edit_message_media")
        await safe_render_page(query, context, img_bytes, caption_text, keyboard, is_edit, owner_id=query.from_user.id)

    elif data.startswith("nav_"):
        parts = data.split("_")
        chapter_id = parts[1]
        page_idx = int(parts[2])
        urls = _udata_get(context, f"manga_{chapter_id}")
        if not urls:
            return await query.answer("❌ Session expired.", show_alert=True)
        prev_ch, next_ch, m_title, m_ch, m_lang = _udata_get(context, f"ctx_{chapter_id}", (None, None, "Unknown", "?", "?"))
        await query.answer()
        img_bytes = await fetch_image_bytes(urls[page_idx])
        if img_bytes:
            keyboard = get_nav_keyboard(chapter_id, page_idx, len(urls), prev_ch, next_ch)
            caption_text = f"📖 <b>{_escape(m_title)}</b> | Ch:{_escape(m_ch)} | 🌐 {_escape(m_lang)}"
            await safe_render_page(query, context, img_bytes, caption_text, keyboard, True, owner_id=query.from_user.id)

    elif data.startswith("pdfdex_"):
        await query.answer("Preparing PDF...")
        chapter_id = data.split("_")[1]
        server_data = await fetch_json(f"{MANGADEX_API}/at-home/server/{chapter_id}")
        if not server_data or not server_data.get("chapter", {}).get("data"):
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Failed to load chapter data for PDF."
            )
        _, _, m_title, m_ch, m_lang = await get_chapter_context(chapter_id, context)
        base_url = server_data.get("baseUrl", "")
        chapter_hash = server_data.get("chapter", {}).get("hash", "")
        urls = [f"{base_url}/data/{chapter_hash}/{p}" for p in server_data.get("chapter", {}).get("data", [])]
        title = f"{m_title} - Ch {m_ch} [{m_lang}]"
        asyncio.create_task(send_pdf_task(query.message.chat_id, query.message.message_thread_id, context, title, urls, "https://mangadex.org/", "dex"))
