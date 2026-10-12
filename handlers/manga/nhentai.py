"""nHentai API client, detail view, dan callback handler.

Menangani fitur nHentai (premium-only):
- Detail gallery -> dibangun `fetch_detail_view`, dikirim sebagai foto cover
  oleh layer search (`search.py`).
- Reader & navigasi halaman (`nhread_`, `nhnav_`)
- Unduh PDF (`pdfnh_`)

`NH_HEADERS` dibangun di `constants.py` dari env `NH_API_KEY`.
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .constants import NH_API_URL, NH_HEADERS
from .http import _escape, fetch_image_bytes, fetch_json, safe_render_page
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


async def fetch_detail_view(gallery_id: str):
    """-> (caption_html, keyboard, cover_url) untuk pesan foto detail nHentai."""
    g_data = await fetch_json(f"{NH_API_URL}/galleries/{gallery_id}", custom_headers=NH_HEADERS)
    if not g_data or "title" not in g_data:
        return None, None, None
    text, markup = build_nh_detail_ui(g_data)
    cover_url = get_nh_cover_url(g_data)
    return text, markup, cover_url


async def handle_nhentai_callback(query, context, data: str):
    if data.startswith("nhdetail_"):
        gallery_id = data.split("_")[1]
        await query.answer("Loading details... ⏳")
        caption, markup, cover_url = await fetch_detail_view(gallery_id)
        if not caption:
            return await query.answer("❌ Server error.")
        if query.message.photo:
            await query.edit_message_caption(caption=caption, parse_mode="HTML", reply_markup=markup)
            _set_message_lock(query.message.chat_id, query.message.message_id, query.from_user.id)
        else:
            await query.edit_message_text(caption, parse_mode="HTML", reply_markup=markup)
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
