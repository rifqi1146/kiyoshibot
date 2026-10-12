"""Maid-Manga scraper, UI builder, dan callback handler.

Menangani fitur Maid-Manga (`maid.my.id`):
- Search (`/manga maid <query>`)
- Detail series & daftar chapter (`maiddet_`)
- Reader chapter & navigasi halaman (`maidread_`, `maidnav_`)
- Unduh PDF (`pdfmaid_`)
"""
import asyncio
import hashlib
import logging
import urllib.parse

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .constants import MAID_URL
from .http import _escape, fetch_html, fetch_image_bytes, safe_render_page
from .pdf import send_pdf_task
from .state import _move_message_lock, _set_message_lock, _udata_get, _udata_set

log = logging.getLogger(__name__)


async def search_maid(full_query: str, context):
    """Search Maid-Manga -> (text, markup) atau (error_text, None)."""
    url = f"{MAID_URL}/?s={urllib.parse.quote(full_query)}"
    html_doc = await fetch_html(url)
    if not html_doc:
        return "❌ Failed to contact the Maid-Manga server.", None

    soup = BeautifulSoup(html_doc, "html.parser")
    keyboard = []
    hasil_unik = set()
    items = soup.select('a[href*="/manga/"], a[href*="/komik/"]')
    for link_tag in items:
        href = link_tag.get("href", "")
        title = link_tag.get("title") or link_tag.text.strip()
        title = " ".join(title.split())
        if not title or href in hasil_unik:
            continue
        hasil_unik.add(href)
        path = href.replace(MAID_URL, "")
        short_id = hashlib.md5(path.encode()).hexdigest()[:8]
        _udata_set(context, f"maid_map_{short_id}", path)
        keyboard.append([InlineKeyboardButton(f"📖 {title[:35]}", callback_data=f"maiddet_{short_id}")])
        if len(keyboard) >= 5:
            break

    if not keyboard:
        return "❌ Manga not found in Maid-Manga.", None

    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_manga")])
    text = f"🔍 <b>Maid-Manga:</b> <code>{_escape(full_query)}</code>"
    return text, InlineKeyboardMarkup(keyboard)


async def _parse_maid_chapter(url_path: str, context):
    """Ambil daftar gambar halaman dari halaman chapter Maid-Manga."""
    target_url = f"{MAID_URL}{url_path}" if url_path.startswith("/") else f"{MAID_URL}/{url_path}"
    html_doc = await fetch_html(target_url)
    if not html_doc:
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    img_tags = soup.select("#readerarea img, .reader-area img, .chapter-image img, .mangareader img")
    urls = []
    for img in img_tags:
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if src and src.startswith("http"):
            urls.append(src)
    return urls or None


async def handle_maid_callback(query, context, data: str):
    if data.startswith("maiddet_"):
        parts = data.split("_")
        short_id = parts[1]
        offset = int(parts[2]) if len(parts) > 2 else 0
        path = _udata_get(context, f"maid_map_{short_id}")
        if not path:
            return await query.answer("❌ Session expired.", show_alert=True)
        await query.answer(f"Loading chapters {offset + 1}-{offset + 5}...")
        html_doc = await fetch_html(f"{MAID_URL}{path}")
        if not html_doc:
            return await query.answer("❌ Failed to load page.")
        soup = BeautifulSoup(html_doc, "html.parser")
        title_tag = soup.select_one(".series-title h2, .series-titlex h2")
        title = title_tag.text.strip() if title_tag else "Unknown Title"
        desc_tag = soup.select_one(".series-synops")
        desc = desc_tag.text.strip() if desc_tag else "No description available."
        cover_tag = soup.select_one(".series-thumb img")
        cover_url = cover_tag.get("src") if cover_tag else None
        if cover_url:
            if cover_url.startswith("//"):
                cover_url = "https:" + cover_url
            elif cover_url.startswith("/"):
                cover_url = MAID_URL + cover_url
            elif cover_url.startswith("data:"):
                cover_url = None
        all_chapters = soup.select(".series-chapterlist li a")
        total_ch = len(all_chapters)
        keyboard = []
        for i, ch in enumerate(all_chapters):
            ch_url = ch.get("href", "")
            ch_path = ch_url.replace(MAID_URL, "")
            ch_span = ch.find("span")
            if ch_span:
                date_span = ch_span.find("span", class_="date")
                if date_span:
                    date_span.extract()
                ch_num = ch_span.text.strip().replace("Chapter ", "").replace("chapter ", "").strip()
            else:
                ch_num = "?"
            ch_sid = hashlib.md5(ch_path.encode()).hexdigest()[:8]
            _udata_set(context, f"maid_map_{ch_sid}", ch_path)
            n_sid = hashlib.md5(all_chapters[i - 1].get("href").replace(MAID_URL, "").encode()).hexdigest()[:8] if i > 0 else None
            p_sid = hashlib.md5(all_chapters[i + 1].get("href").replace(MAID_URL, "").encode()).hexdigest()[:8] if i < total_ch - 1 else None
            _udata_set(context, f"maid_ctx_{ch_sid}", {"next_ch": n_sid, "prev_ch": p_sid, "title": title, "ch_num": ch_num})
            if offset <= i < offset + 5:
                keyboard.append([
                    InlineKeyboardButton(f"📖 Ch. {ch_num}", callback_data=f"maidread_{ch_sid}"),
                    InlineKeyboardButton("⬇️ PDF", callback_data=f"pdfmaid_{ch_sid}")
                ])
        list_nav = []
        if offset > 0:
            list_nav.append(InlineKeyboardButton("⬅️ Newer", callback_data=f"maiddet_{short_id}_{offset - 5}"))
        if offset + 5 < total_ch:
            list_nav.append(InlineKeyboardButton("Older ➡️", callback_data=f"maiddet_{short_id}_{offset + 5}"))
        if list_nav:
            keyboard.append(list_nav)
        keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_manga")])
        caption = (
            f"📚 <b>{_escape(title)}</b>\n\n"
            f"📝 {_escape(desc[:300])}...\n\n"
            f"🗂 <i>Chapters {offset + 1}-{min(offset + 5, total_ch)} of {total_ch}</i>"
        )
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

    elif data.startswith("maidread_"):
        await query.answer("Opening chapter... ⏳")
        short_id = data.split("_")[1]
        path = _udata_get(context, f"maid_map_{short_id}")
        if not path:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Session expired."
            )
        ctx = _udata_get(context, f"maid_ctx_{short_id}", {})
        manga_title = ctx.get("title", "Manga Maid")
        ch_num = ctx.get("ch_num", "?")
        urls = await _parse_maid_chapter(path, context)
        if not urls:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ No pages found."
            )
        _udata_set(context, f"maid_imgs_{short_id}", urls)
        img_bytes = await fetch_image_bytes(urls[0], referer=MAID_URL)
        if not img_bytes:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Download error."
            )
        nav = [
            InlineKeyboardButton(f"📄 1/{len(urls)}", callback_data="ignore"),
            InlineKeyboardButton("Next ➡️", callback_data=f"maidnav_{short_id}_1")
        ]
        ch_nav = []
        if ctx.get("prev_ch"):
            ch_nav.append(InlineKeyboardButton("⏪ Ch Prev", callback_data=f"maidread_{ctx['prev_ch']}"))
        ch_nav.append(InlineKeyboardButton("❌ Close", callback_data="close_manga"))
        if ctx.get("next_ch"):
            ch_nav.append(InlineKeyboardButton("Ch Next ⏩", callback_data=f"maidread_{ctx['next_ch']}"))
        keyboard = InlineKeyboardMarkup([nav, ch_nav])
        caption = f"📖 <b>{_escape(manga_title)}</b> | Ch:{_escape(ch_num)}"
        is_edit = hasattr(query, "edit_message_media")
        await safe_render_page(query, context, img_bytes, caption, keyboard, is_edit, owner_id=query.from_user.id)

    elif data.startswith("maidnav_"):
        parts = data.split("_")
        short_id = parts[1]
        page_idx = int(parts[2])
        urls = _udata_get(context, f"maid_imgs_{short_id}")
        if not urls:
            return await query.answer("❌ Reader session expired.", show_alert=True)
        ctx = _udata_get(context, f"maid_ctx_{short_id}", {})
        await query.answer()
        img_bytes = await fetch_image_bytes(urls[page_idx], referer=MAID_URL)
        if img_bytes:
            nav = []
            if page_idx > 0:
                nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"maidnav_{short_id}_{page_idx - 1}"))
            nav.append(InlineKeyboardButton(f"📄 {page_idx + 1}/{len(urls)}", callback_data="ignore"))
            if page_idx < len(urls) - 1:
                nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"maidnav_{short_id}_{page_idx + 1}"))
            ch_nav = []
            if ctx.get("prev_ch"):
                ch_nav.append(InlineKeyboardButton("⏪ Ch Prev", callback_data=f"maidread_{ctx['prev_ch']}"))
            ch_nav.append(InlineKeyboardButton("❌ Close", callback_data="close_manga"))
            if ctx.get("next_ch"):
                ch_nav.append(InlineKeyboardButton("Ch Next ⏩", callback_data=f"maidread_{ctx['next_ch']}"))
            caption = f"📖 <b>{_escape(ctx.get('title'))}</b> | Ch:{_escape(ctx.get('ch_num'))}"
            await safe_render_page(query, context, img_bytes, caption, InlineKeyboardMarkup([nav, ch_nav]), True, owner_id=query.from_user.id)

    elif data.startswith("pdfmaid_"):
        await query.answer("Preparing PDF...")
        short_id = data.split("_")[1]
        path = _udata_get(context, f"maid_map_{short_id}")
        if not path:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Session expired. Please search again."
            )
        ctx = _udata_get(context, f"maid_ctx_{short_id}", {})
        manga_title = ctx.get("title", "Manga Maid")
        ch_num = ctx.get("ch_num", "?")
        urls = await _parse_maid_chapter(path, context)
        if not urls:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ No pages found for PDF."
            )
        title = f"{manga_title} - Ch {ch_num}"
        asyncio.create_task(send_pdf_task(query.message.chat_id, query.message.message_thread_id, context, title, urls, MAID_URL, "maid"))
