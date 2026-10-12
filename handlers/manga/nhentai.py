"""nHentai API client, UI builder, dan callback handler.

Menangani fitur nHentai (premium-only):
- Search & paginasi (`nhsearch_`)
- Detail gallery (`nhdetail_`)
- Reader & navigasi halaman (`nhread_`, `nhnav_`)
- Unduh PDF (`pdfnh_`)

`NH_HEADERS` dibangun di `constants.py` dari env `NH_API_KEY`.
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from .constants import NH_API_URL, NH_HEADERS
from .http import _escape, enforce_telegram_photo_limits, fetch_image_bytes, fetch_json, safe_render_page
from .pdf import send_pdf_task
from .state import _move_message_lock, _set_message_lock, _udata_get, _udata_has, _udata_set

log = logging.getLogger(__name__)


def get_nh_cover_url(g_data):
    pages = g_data.get("pages", [])
    media_id = g_data.get("media_id")
    if not pages:
        return None
    path = pages[0].get("path", "")
    if path.startswith("http"):
        return path
    ext = path.split(".")[-1] if "." in path else "jpg"
    return f"https://i.nhentai.net/galleries/{media_id}/1.{ext}"


def build_nh_detail_ui(data):
    title = data["title"]["pretty"] or data["title"]["english"]
    tags = ", ".join(t["name"] for t in data["tags"] if t["type"] == "tag")
    if len(tags) > 500:
        tags = tags[:500] + "..."
    text = (
        f"📚 <b>{_escape(title)}</b>\n\n"
        f"🆔 <b>Code:</b> <code>{_escape(data['id'])}</code>\n"
        f"📄 <b>Pages:</b> {_escape(data['num_pages'])}\n"
        f"❤️ <b>Favorites:</b> {_escape(data['num_favorites'])}\n"
        f"🏷 <b>Tags:</b> {_escape(tags)}"
    )
    keyboard = [
        [InlineKeyboardButton("📖 Read Now", callback_data=f"nhread_read_{data['id']}_0")],
        [InlineKeyboardButton("⬇️ Download PDF", callback_data=f"pdfnh_{data['id']}")],
        [InlineKeyboardButton("❌ Close", callback_data="close_manga")]
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def build_nh_search_list(query: str, page: int, context):
    data = await fetch_json(f"{NH_API_URL}/search", {"query": query, "page": page}, custom_headers=NH_HEADERS)
    if not data or not data.get("result"):
        return None, None
    _udata_set(context, "last_nh_query", query)
    keyboard = []
    for item in data["result"]:
        raw_title = item["english_title"] or "Unknown"
        title = raw_title[:40] + "..." if len(raw_title) > 40 else raw_title
        gallery_id = item["id"]
        keyboard.append([InlineKeyboardButton(f"📖 {title}", callback_data=f"nhdetail_{gallery_id}")])
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nhsearch_{page - 1}"))
    if data["num_pages"] > page:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"nhsearch_{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_manga")])
    return f"🔍 nHentai results: <b>{_escape(query)}</b> (Page {page}/{data['num_pages']})", InlineKeyboardMarkup(keyboard)


async def handle_nhentai_callback(query, context, data: str):
    if data.startswith("nhsearch_"):
        page = int(data.split("_")[1])
        q = _udata_get(context, "last_nh_query", "")
        if not q:
            return await query.answer("❌ Search session expired.", show_alert=True)
        text, markup = await build_nh_search_list(q, page, context)
        if text:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

    elif data.startswith("nhdetail_"):
        gallery_id = data.split("_")[1]
        await query.answer("Loading details... ⏳")
        g_data = await fetch_json(f"{NH_API_URL}/galleries/{gallery_id}", custom_headers=NH_HEADERS)
        if not g_data:
            return await query.answer("❌ Server error.")
        text, markup = build_nh_detail_ui(g_data)
        cover_url = get_nh_cover_url(g_data)
        cover_bytes = await fetch_image_bytes(cover_url, referer="https://nhentai.net/")
        if query.message.photo:
            if cover_bytes:
                img_safe = await asyncio.to_thread(enforce_telegram_photo_limits, cover_bytes)
                await query.edit_message_media(
                    media=InputMediaPhoto(media=img_safe, caption=text, parse_mode="HTML"),
                    reply_markup=markup
                )
                _set_message_lock(query.message.chat_id, query.message.message_id, query.from_user.id)
            else:
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
            if cover_bytes:
                await query.message.delete()
                img_safe = await asyncio.to_thread(enforce_telegram_photo_limits, cover_bytes)
                sent = await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    message_thread_id=query.message.message_thread_id,
                    photo=img_safe,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
                _move_message_lock(query.message.chat_id, query.message.message_id, sent.chat_id, sent.message_id, fallback_user_id=query.from_user.id)
            else:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                _set_message_lock(query.message.chat_id, query.message.message_id, query.from_user.id)

    elif data.startswith("nhread_") or data.startswith("nhnav_"):
        await query.answer("Downloading page... ⏳")
        parts = data.split("_")
        gallery_id = parts[-2]
        page_idx = int(parts[-1])
        cache_key = f"nh_{gallery_id}"
        if not _udata_has(context, cache_key):
            g_data = await fetch_json(f"{NH_API_URL}/galleries/{gallery_id}", custom_headers=NH_HEADERS)
            if not g_data:
                return await query.answer("❌ Server error.")
            _udata_set(context, cache_key, g_data)
        else:
            g_data = _udata_get(context, cache_key)
        pages = g_data["pages"]
        config = await fetch_json(f"{NH_API_URL}/config", custom_headers=NH_HEADERS)
        img_server = config["image_servers"][0] if config and "image_servers" in config else "https://i.nhentai.net"
        path = pages[page_idx]["path"]
        if path.startswith("http"):
            img_url = path
        else:
            ext = path.split(".")[-1] if "." in path else "jpg"
            img_url = f"{img_server}/galleries/{g_data['media_id']}/{page_idx + 1}.{ext}"
        img_bytes = await fetch_image_bytes(img_url, referer="https://nhentai.net/")
        if not img_bytes:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Failed to download page."
            )
        nav = []
        if page_idx > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nhnav_{gallery_id}_{page_idx - 1}"))
        nav.append(InlineKeyboardButton(f"📄 {page_idx + 1}/{len(pages)}", callback_data="ignore"))
        if page_idx < len(pages) - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"nhnav_{gallery_id}_{page_idx + 1}"))
        keyboard = InlineKeyboardMarkup([
            nav,
            [
                InlineKeyboardButton("🔙 Details", callback_data=f"nhdetail_{gallery_id}"),
                InlineKeyboardButton("❌ Close", callback_data="close_manga")
            ]
        ])
        title = g_data["title"]["pretty"] or g_data["title"]["english"]
        caption_text = f"🔞 <b>{_escape(title[:100])}</b>\n📄 Page: {page_idx + 1}/{len(pages)}"
        is_edit = bool(getattr(query.message, "photo", None))
        await safe_render_page(query, context, img_bytes, caption_text, keyboard, is_edit, owner_id=query.from_user.id)

    elif data.startswith("pdfnh_"):
        await query.answer("Preparing PDF...")
        gallery_id = data.split("_")[1]
        g_data = await fetch_json(f"{NH_API_URL}/galleries/{gallery_id}", custom_headers=NH_HEADERS)
        if not g_data:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Server error while fetching Doujin data."
            )
        pages = g_data.get("pages", [])
        if not pages:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ No pages found in this Doujin."
            )
        config = await fetch_json(f"{NH_API_URL}/config", custom_headers=NH_HEADERS)
        img_server = config["image_servers"][0] if config and "image_servers" in config else "https://i.nhentai.net"
        urls = []
        for i, page in enumerate(pages):
            path = page["path"]
            if path.startswith("http"):
                urls.append(path)
            else:
                ext = path.split(".")[-1] if "." in path else "jpg"
                urls.append(f"{img_server}/galleries/{g_data['media_id']}/{i + 1}.{ext}")
        title = g_data["title"]["pretty"] or g_data["title"]["english"]
        asyncio.create_task(send_pdf_task(query.message.chat_id, query.message.message_thread_id, context, title, urls, "https://nhentai.net/", "nh"))
