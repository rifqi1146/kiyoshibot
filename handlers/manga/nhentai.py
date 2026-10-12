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
from .rich import build_nh_detail_rich, edit_manga_rich
from .state import _move_message_lock, _set_message_lock, _udata_get, _udata_has, _udata_set, get_reply_target, set_reply_target

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


def build_nh_keyboard(data) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Read Now", callback_data=f"nhread_read_{data['id']}_0")],
        [InlineKeyboardButton("⬇️ Download PDF", callback_data=f"pdfnh_{data['id']}")],
        [InlineKeyboardButton("❌ Close", callback_data="close_manga")],
    ])


async def fetch_detail_view(gallery_id: str):
    """-> (rich_html, plain_html, keyboard, cover_url) untuk detail nHentai."""
    g_data = await fetch_json(f"{NH_API_URL}/galleries/{gallery_id}", custom_headers=NH_HEADERS)
    if not g_data or "title" not in g_data:
        return None, None, None, None
    cover_url = get_nh_cover_url(g_data)
    rich_html, plain_html = build_nh_detail_rich(g_data, cover_url or "")
    keyboard = build_nh_keyboard(g_data)
    return rich_html, plain_html, keyboard, cover_url


async def handle_nhentai_callback(query, context, data: str):
    if data.startswith("nhdetail_"):
        gallery_id = data.split("_")[1]
        await query.answer("Loading details... ⏳")
        rich_html, plain_html, markup, cover_url = await fetch_detail_view(gallery_id)
        if not rich_html:
            return await query.answer("❌ Server error.")
        if getattr(query.message, "photo", None):
            # Dari reader (pesan foto): hapus lalu kirim rich detail baru sebagai reply.
            from .http import send_detail_view
            chat_id = query.message.chat_id
            thread_id = getattr(query.message, "message_thread_id", None)
            reply_to = get_reply_target(chat_id, query.message.message_id)
            try:
                await query.message.delete()
            except Exception:
                pass
            sent = await send_detail_view(
                context, chat_id, thread_id, rich_html, plain_html, markup,
                owner_id=query.from_user.id, reply_to_message_id=reply_to,
            )
            if sent:
                msg_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
                if msg_id and reply_to:
                    set_reply_target(chat_id, msg_id, reply_to)
            return
        await edit_manga_rich(context.bot, query.message.chat_id, query.message.message_id, rich_html, plain_html, markup)
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
        reply_to = get_reply_target(query.message.chat_id, query.message.message_id)
        is_edit = bool(getattr(query.message, "photo", None)) and data.startswith("nhnav_")
        if not is_edit:
            # Entry dari detail (nhread_): pesan detail hilang, reader dikirim sebagai reply.
            try:
                await query.message.delete()
            except Exception:
                pass
        await safe_render_page(
            query, context, img_bytes, caption_text, keyboard, is_edit,
            owner_id=query.from_user.id, reply_to_message_id=reply_to,
        )

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
        reply_to = get_reply_target(query.message.chat_id, query.message.message_id)
        # Pesan detail hilang, PDF dikirim sebagai reply ke pesan command user.
        try:
            await query.message.delete()
        except Exception:
            pass
        asyncio.create_task(send_pdf_task(
            query.message.chat_id, query.message.message_thread_id, context,
            title, urls, "https://nhentai.net/", "nh", reply_to_message_id=reply_to,
        ))
