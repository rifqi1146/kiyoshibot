import os
import time
import logging
import asyncio
import sqlite3
import re
import hashlib
import urllib.parse
import html as html_lib
from io import BytesIO
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from database import premium
from database.nsfw_db import is_nsfw_allowed, nsfw_db_init
from utils.http import get_http_session

try:
    from PIL import Image
except ImportError:
    Image = None
    logging.warning("Pillow is not installed. Long images may fail to send.")

log = logging.getLogger(__name__)

MANGADEX_API = "https://api.mangadex.org"
UPLOADS_URL = "https://uploads.mangadex.org"
MAID_URL = "https://www.maid.my.id"
NH_API_URL = "https://nhentai.net/api/v2"
_MANGA_MESSAGE_LOCKS = {}
_MANGA_LOCK_TIMES = {}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"

def _is_nsfw_enabled(chat_id: int, chat_type: str) -> bool:
    return is_nsfw_allowed(chat_id, chat_type)

def _message_lock_key(chat_id: int, message_id: int):
    return (int(chat_id), int(message_id))

def _cleanup_expired_locks():
    now = time.time()
    expired = [k for k, ts in list(_MANGA_LOCK_TIMES.items()) if now - ts > 300]
    for k in expired:
        _MANGA_MESSAGE_LOCKS.pop(k, None)
        _MANGA_LOCK_TIMES.pop(k, None)

def _set_message_lock(chat_id: int, message_id: int, user_id: int):
    if chat_id is None or message_id is None or user_id is None:
        return
    _cleanup_expired_locks()
    key = _message_lock_key(chat_id, message_id)
    _MANGA_MESSAGE_LOCKS[key] = int(user_id)
    _MANGA_LOCK_TIMES[key] = time.time()

def _clear_message_lock(chat_id: int, message_id: int):
    if chat_id is None or message_id is None:
        return
    key = _message_lock_key(chat_id, message_id)
    _MANGA_MESSAGE_LOCKS.pop(key, None)
    _MANGA_LOCK_TIMES.pop(key, None)

def _move_message_lock(old_chat_id: int, old_message_id: int, new_chat_id: int, new_message_id: int, fallback_user_id: int | None = None):
    old_key = _message_lock_key(old_chat_id, old_message_id)
    owner_id = _MANGA_MESSAGE_LOCKS.pop(old_key, None)
    _MANGA_LOCK_TIMES.pop(old_key, None)
    if owner_id is None:
        owner_id = fallback_user_id
    if owner_id is not None and new_chat_id is not None and new_message_id is not None:
        new_key = _message_lock_key(new_chat_id, new_message_id)
        _MANGA_MESSAGE_LOCKS[new_key] = int(owner_id)
        _MANGA_LOCK_TIMES[new_key] = time.time()

async def _ensure_manga_allowed(chat) -> bool:
    if not chat:
        return False
    return _is_nsfw_enabled(chat.id, chat.type)

async def _ensure_callback_lock(query) -> bool:
    msg = query.message
    if not msg or not query.from_user:
        return False
    _cleanup_expired_locks()
    key = _message_lock_key(msg.chat_id, msg.message_id)
    owner_id = _MANGA_MESSAGE_LOCKS.get(key)
    if owner_id is None:
        _MANGA_MESSAGE_LOCKS[key] = int(query.from_user.id)
        _MANGA_LOCK_TIMES[key] = time.time()
        return True
    if int(owner_id) != int(query.from_user.id):
        await query.answer("Not your button.", show_alert=True)
        return False
    return True

def _escape(text) -> str:
    return html_lib.escape(str(text or ""))

async def fetch_json(url, params=None, custom_headers=None):
    headers = custom_headers if custom_headers else {"User-Agent": "MangaBot/3.0"}
    session = await get_http_session()
    try:
        async with session.get(url, params=params, headers=headers, timeout=10) as response:
            if response.status == 200:
                return await response.json()
    except Exception as e:
        log.error(f"Error fetching JSON: {e}")
    return None

async def fetch_image_bytes(url, referer="https://mangadex.org/"):
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    session = await get_http_session()
    try:
        async with session.get(url, headers=headers, timeout=15) as response:
            if response.status == 200:
                return await response.read()
    except Exception as e:
        log.error(f"Error fetching image: {e}")
    return None

async def fetch_html(url):
    headers = {"User-Agent": USER_AGENT}
    session = await get_http_session()
    try:
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 200:
                return await response.text()
    except Exception as e:
        log.error(f"Error fetching Maid HTML: {e}")
    return None

def enforce_telegram_photo_limits(img_bytes):
    if not Image:
        return img_bytes
    try:
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        new_w, new_h = w, h
        changed = False
        if (new_w + new_h) > 9500:
            scale = 9500.0 / float(new_w + new_h)
            new_w, new_h = int(new_w * scale), int(new_h * scale)
            changed = True
        if (new_h / new_w) > 19:
            new_w = int(new_h / 19)
            changed = True
        elif (new_w / new_h) > 19:
            new_h = int(new_w / 19)
            changed = True
        if changed:
            resample_method = getattr(Image, "Resampling", Image).LANCZOS
            img = img.resize((new_w, new_h), resample_method)
            out = BytesIO()
            img.save(out, format="JPEG", quality=85)
            return out.getvalue()
        return img_bytes
    except Exception as e:
        log.error(f"Failed to resize image via Pillow: {e}")
        return img_bytes

async def safe_render_page(query, context, img_bytes, caption, keyboard, is_edit=True, owner_id=None):
    img_safe = await asyncio.to_thread(enforce_telegram_photo_limits, img_bytes)
    if owner_id is None and getattr(query, "from_user", None):
        owner_id = query.from_user.id
    can_try_edit = bool(is_edit and getattr(query.message, "photo", None))
    if can_try_edit:
        for attempt in range(2):
            try:
                await query.message.edit_media(
                    media=InputMediaPhoto(media=img_safe, caption=caption, parse_mode="HTML"),
                    reply_markup=keyboard
                )
                _set_message_lock(query.message.chat_id, query.message.message_id, owner_id)
                return
            except Exception as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    _set_message_lock(query.message.chat_id, query.message.message_id, owner_id)
                    return
                retryable = "timeout" in err or "timed out" in err or "temporarily unavailable" in err or "try again" in err
                if attempt == 0 and retryable:
                    log.warning(f"Temporary media edit failure, retrying once: {e}")
                    await asyncio.sleep(0.8)
                    continue
                hard_fallback = (
                    "there is no media in the message to edit" in err
                    or "message can't be edited" in err
                    or "message to edit not found" in err
                    or "wrong file identifier" in err
                )
                if not hard_fallback:
                    log.warning(f"Edit failed, keeping current message instead of resend: {e}")
                    return
                log.warning(f"Edit failed, falling back to delete-resend: {e}")
                break
    try:
        if is_edit:
            try:
                await query.message.delete()
            except Exception as e:
                log.warning(f"Failed to delete old message while rendering page: {e}")
        sent = await context.bot.send_photo(
            chat_id=query.message.chat_id,
            message_thread_id=query.message.message_thread_id,
            photo=img_safe,
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        _move_message_lock(query.message.chat_id, query.message.message_id, sent.chat_id, sent.message_id, fallback_user_id=owner_id)
    except Exception as e:
        log.error(f"Failed to send filtered photo: {e}")
        try:
            sent = await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="<b>Telegram refuses to send this page.</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            _move_message_lock(query.message.chat_id, query.message.message_id, sent.chat_id, sent.message_id, fallback_user_id=owner_id)
        except Exception as send_err:
            log.warning(f"Failed to send manga fallback error message: {send_err}")

async def get_chapter_context(chapter_id: str, context: ContextTypes.DEFAULT_TYPE):
    cache_key = f"ctx_{chapter_id}"
    if cache_key in context.user_data:
        return context.user_data[cache_key]
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
    context.user_data[cache_key] = res
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

async def build_search_list(query: str, offset: int, context: ContextTypes.DEFAULT_TYPE):
    search_url = f"{MANGADEX_API}/manga"
    params = {"title": query, "limit": 5, "offset": offset, "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"]}
    data = await fetch_json(search_url, params)
    if not data or not data.get("data"):
        return None, None
    keyboard = []
    for manga in data["data"]:
        title = manga["attributes"]["title"].get("en", manga["attributes"]["title"].get("ja-ro", "Unknown"))
        context.user_data["last_manga_query"] = query
        keyboard.append([InlineKeyboardButton(f"📖 {title[:35]}", callback_data=f"detailmanga_{manga['id']}_0")])
    nav_buttons = []
    if offset > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"msearch_{offset - 5}"))
    if data["total"] > offset + 5:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"msearch_{offset + 5}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_manga")])
    return f"🔍 MangaDex results: <b>{_escape(query)}</b> ({(offset//5)+1})", InlineKeyboardMarkup(keyboard)

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
        [InlineKeyboardButton("❌ Close", callback_data="close_manga")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

async def build_nh_search_list(query: str, page: int, context: ContextTypes.DEFAULT_TYPE):
    data = await fetch_json(f"{NH_API_URL}/search", {"query": query, "page": page}, custom_headers=NH_HEADERS)
    if not data or not data.get("result"):
        return None, None
    context.user_data["last_nh_query"] = query
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

NH_API_KEY = os.getenv("NH_API")
NH_HEADERS = {"User-Agent": "PrivateMangaBot/3.0 (Telegram Bot)"}
if NH_API_KEY:
    NH_HEADERS["Authorization"] = f"Key {NH_API_KEY}"
    log.info("Loaded NH API Key.")
else:
    log.info("NH API Key not found, Running In Public Mode.")

async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not _is_nsfw_enabled(chat.id, chat.type):
        return await msg.reply_text("❌ NSFW is not enabled in this group.")
    if not context.args or len(context.args) < 2:
        help_txt = (
            "⚠️ <b>Invalid format!</b>\n\n"
            "Use: <code>/manga &lt;source&gt; &lt;title/code&gt;</code>\n\n"
            "📚 <b>Sources:</b> <code>dex</code> | <code>maid</code> | <code>nh</code>\n"
            "💡 <b>Examples:</b>\n"
            "<code>/manga dex Haimiya-senpai</code>\n"
            "<code>/manga maid Osananajimi wo Onnanoko</code>\n"
            "<code>/manga nh Zenles Zone Zero Or 642563</code>\n"
        )
        return await msg.reply_text(help_txt, parse_mode="HTML")
    source = context.args[0].lower()
    full_query = " ".join(context.args[1:])
    user = update.effective_user
    if source in ["nh", "nhentai"] and (not user or not premium.check(user.id)):
        return await update.message.reply_text("❌ NH manga is only available for premium users.")
    status = await msg.reply_text(
        f"🔍 Processing <code>{_escape(full_query)}</code> in <b>{_escape(source.upper())}</b>...",
        parse_mode="HTML"
    )
    _set_message_lock(status.chat_id, status.message_id, update.effective_user.id)
    if source in ["dex", "mangadex"]:
        text, markup = await build_search_list(full_query, 0, context)
        if text:
            await status.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await status.edit_text("❌ Manga not found in MangaDex.", parse_mode="HTML")
    elif source in ["maid", "maidmanga"]:
        url = f"{MAID_URL}/?s={urllib.parse.quote(full_query)}"
        html_doc = await fetch_html(url)
        if not html_doc:
            return await status.edit_text("❌ Failed to contact the Maid-Manga server.", parse_mode="HTML")
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
            context.user_data[f"maid_map_{short_id}"] = path
            keyboard.append([InlineKeyboardButton(f"📖 {title[:35]}", callback_data=f"maiddet_{short_id}")])
            if len(keyboard) >= 5:
                break
        if not keyboard:
            return await status.edit_text("❌ Manga not found in Maid-Manga.", parse_mode="HTML")
        keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_manga")])
        await status.edit_text(
            f"🔍 <b>Maid-Manga:</b> <code>{_escape(full_query)}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif source in ["nh", "nhentai"]:
        if full_query.isdigit():
            data = await fetch_json(f"{NH_API_URL}/galleries/{full_query}", custom_headers=NH_HEADERS)
            if not data or "error" in data or "title" not in data:
                return await status.edit_text("❌ Doujin not found (invalid code).", parse_mode="HTML")
            text, markup = build_nh_detail_ui(data)
            cover_url = get_nh_cover_url(data)
            cover_bytes = await fetch_image_bytes(cover_url, referer="https://nhentai.net/")
            if cover_bytes:
                await status.delete()
                img_safe = await asyncio.to_thread(enforce_telegram_photo_limits, cover_bytes)
                sent = await context.bot.send_photo(
                    status.chat_id,
                    photo=img_safe,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
                _move_message_lock(status.chat_id, status.message_id, sent.chat_id, sent.message_id, fallback_user_id=update.effective_user.id)
            else:
                await status.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            text, markup = await build_nh_search_list(full_query, 1, context)
            if text:
                await status.edit_text(text, parse_mode="HTML", reply_markup=markup)
            else:
                await status.edit_text("❌ Doujin not found.", parse_mode="HTML")
    else:
        await status.edit_text(
            "❌ <b>Unknown source.</b>\nChoose <code>dex</code>, <code>maid</code>, or <code>nh</code>.",
            parse_mode="HTML"
        )

async def manga_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    query = update.callback_query
    data = query.data
    if not _is_nsfw_enabled(query.message.chat_id, query.message.chat.type):
        return await query.answer("❌ NSFW is not enabled in this group.", show_alert=True)
    if not await _ensure_callback_lock(query):
        return
    if data.startswith(("nhsearch_", "nhdetail_", "nhread_", "nhnav_")):
        user = update.effective_user
        if not user or not premium.check(user.id):
            return await query.answer("❌ NH manga is only available for premium users.", show_alert=True)
    if data == "ignore":
        return await query.answer()
    elif data == "close_manga":
        await query.answer("Reader closed.")
        _clear_message_lock(query.message.chat_id, query.message.message_id)
        return await query.message.delete()
    elif data.startswith("msearch_"):
        offset = int(data.split("_")[1])
        q = context.user_data.get("last_manga_query", "")
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
                keyboard.append([InlineKeyboardButton(f"📖 Ch.{ch_num} [{lang}]", callback_data=f"readmanga_{ch['id']}")])
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
        context.user_data[f"manga_{chapter_id}"] = urls
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
        urls = context.user_data.get(f"manga_{chapter_id}")
        if not urls:
            return await query.answer("❌ Session expired.", show_alert=True)
        prev_ch, next_ch, m_title, m_ch, m_lang = context.user_data.get(f"ctx_{chapter_id}", (None, None, "Unknown", "?", "?"))
        await query.answer()
        img_bytes = await fetch_image_bytes(urls[page_idx])
        if img_bytes:
            keyboard = get_nav_keyboard(chapter_id, page_idx, len(urls), prev_ch, next_ch)
            caption_text = f"📖 <b>{_escape(m_title)}</b> | Ch:{_escape(m_ch)} | 🌐 {_escape(m_lang)}"
            await safe_render_page(query, context, img_bytes, caption_text, keyboard, True, owner_id=query.from_user.id)
    elif data.startswith("maiddet_"):
        parts = data.split("_")
        short_id = parts[1]
        offset = int(parts[2]) if len(parts) > 2 else 0
        path = context.user_data.get(f"maid_map_{short_id}")
        if not path:
            return await query.answer("❌ Session expired.", show_alert=True)
        await query.answer(f"Loading chapters {offset+1}-{offset+5}...")
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
            context.user_data[f"maid_map_{ch_sid}"] = ch_path
            n_sid = hashlib.md5(all_chapters[i - 1].get("href").replace(MAID_URL, "").encode()).hexdigest()[:8] if i > 0 else None
            p_sid = hashlib.md5(all_chapters[i + 1].get("href").replace(MAID_URL, "").encode()).hexdigest()[:8] if i < total_ch - 1 else None
            context.user_data[f"maid_ctx_{ch_sid}"] = {"next_ch": n_sid, "prev_ch": p_sid, "title": title, "ch_num": ch_num}
            if offset <= i < offset + 5:
                keyboard.append([InlineKeyboardButton(f"📖 Ch. {ch_num}", callback_data=f"maidread_{ch_sid}")])
        list_nav = []
        if offset > 0:
            list_nav.append(InlineKeyboardButton("⬅️ Newer", callback_data=f"maiddet_{short_id}_{offset - 5}"))
        if offset + 5 < total_ch:
            list_nav.append(InlineKeyboardButton("Older ➡️", callback_data=f"maiddet_{short_id}_{offset + 5}"))
        if list_nav:
            keyboard.append(list_nav)
        keyboard.append([InlineKeyboardButton("❌ Tutup", callback_data="close_manga")])
        caption = (
            f"📚 <b>{_escape(title)}</b>\n\n"
            f"📝 {_escape(desc[:300])}...\n\n"
            f"🗂 <i>Chapters {offset+1}-{min(offset+5, total_ch)} of {total_ch}</i>"
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
        path = context.user_data.get(f"maid_map_{short_id}")
        if not path:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Session expired."
            )
        ctx = context.user_data.get(f"maid_ctx_{short_id}", {})
        manga_title = ctx.get("title", "Manga Maid")
        ch_num = ctx.get("ch_num", "?")
        target_url = f"{MAID_URL}{path}" if path.startswith("/") else f"{MAID_URL}/{path}"
        html_doc = await fetch_html(target_url)
        if not html_doc:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ Failed to load chapter."
            )
        soup = BeautifulSoup(html_doc, "html.parser")
        img_tags = soup.select("#readerarea img, .reader-area img, .chapter-image img, .mangareader img")
        urls = []
        for img in img_tags:
            src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
            if src and src.startswith("http"):
                urls.append(src)
        if not urls:
            return await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=query.message.message_thread_id,
                text="❌ No pages found."
            )
        context.user_data[f"maid_imgs_{short_id}"] = urls
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
        ch_nav.append(InlineKeyboardButton("❌ Tutup", callback_data="close_manga"))
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
        urls = context.user_data.get(f"maid_imgs_{short_id}")
        if not urls:
            return await query.answer("❌ Reader session expired.", show_alert=True)
        ctx = context.user_data.get(f"maid_ctx_{short_id}", {})
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
            ch_nav.append(InlineKeyboardButton("❌ Tutup", callback_data="close_manga"))
            if ctx.get("next_ch"):
                ch_nav.append(InlineKeyboardButton("Ch Next ⏩", callback_data=f"maidread_{ctx['next_ch']}"))
            caption = f"📖 <b>{_escape(ctx.get('title'))}</b> | Ch:{_escape(ctx.get('ch_num'))}"
            await safe_render_page(query, context, img_bytes, caption, InlineKeyboardMarkup([nav, ch_nav]), True, owner_id=query.from_user.id)
    elif data.startswith("nhsearch_"):
        page = int(data.split("_")[1])
        q = context.user_data.get("last_nh_query", "")
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
        if cache_key not in context.user_data:
            g_data = await fetch_json(f"{NH_API_URL}/galleries/{gallery_id}", custom_headers=NH_HEADERS)
            if not g_data:
                return await query.answer("❌ Server error.")
            context.user_data[cache_key] = g_data
        else:
            g_data = context.user_data[cache_key]
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
                InlineKeyboardButton("🔙 Detail", callback_data=f"nhdetail_{gallery_id}"),
                InlineKeyboardButton("❌ Tutup", callback_data="close_manga")
            ]
        ])
        title = g_data["title"]["pretty"] or g_data["title"]["english"]
        caption_text = f"🔞 <b>{_escape(title[:100])}</b>\n📄 Page: {page_idx + 1}/{len(pages)}"
        is_edit = bool(getattr(query.message, "photo", None))
        await safe_render_page(query, context, img_bytes, caption_text, keyboard, is_edit, owner_id=query.from_user.id)

try:
    nsfw_db_init()
except Exception as e:
    log.warning(f"Failed to initialize manga NSFW database: {e}")
