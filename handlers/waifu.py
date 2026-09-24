from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes
import aiohttp
import time
from handlers.join import require_join_or_block
from utils.http import get_http_session
from database.nsfw_db import is_nsfw_allowed as _nsfw_allowed, nsfw_db_init as _nsfw_db_init

_nsfw_db_init()

_WAIFU_LAST_TAG = {}
_WAIFU_HISTORY = {}
_WAIFU_TS = {}

EXPIRE_SEC = 30 * 60

def _is_nsfw_enabled(chat_id: int, chat_type: str) -> bool:
    if chat_type == "private":
        return True
    return _nsfw_allowed(chat_id, chat_type)

def _state_key(chat_id: int, user_id: int):
    return f"{int(chat_id)}:{int(user_id)}"

def _cleanup(key):
    now = time.time()
    ts = _WAIFU_TS.get(key)
    if not ts or now - ts > EXPIRE_SEC:
        _WAIFU_HISTORY.pop(key, None)
        _WAIFU_LAST_TAG.pop(key, None)
        _WAIFU_TS.pop(key, None)

def _push(key, img):
    _cleanup(key)
    _WAIFU_HISTORY.setdefault(key, []).append(img)
    _WAIFU_TS[key] = time.time()

def _pop(key):
    _cleanup(key)
    hist = _WAIFU_HISTORY.get(key, [])
    if len(hist) < 2:
        return None
    hist.pop()
    return hist[-1]

def _build_caption(img, tag):
    cap = "💖 <b>Waifu</b>\n"
    if tag:
        cap += f"🏷 Tag: <code>{tag}</code>\n"
    artist = img.get("artist") or {}
    if artist.get("name"):
        cap += f"🎨 Artist: <b>{artist['name']}</b>"
    return cap

def _build_kb(chat_id: int, user_id: int, img):
    prefix = f"waifu:{int(chat_id)}:{int(user_id)}"
    rows = [[
        InlineKeyboardButton("⏪ Pref", callback_data=f"{prefix}:pref"),
        InlineKeyboardButton("▶️ Next", callback_data=f"{prefix}:next")
    ]]
    if img.get("source"):
        rows.append([InlineKeyboardButton("🔗 Source", url=img["source"])])
    return InlineKeyboardMarkup(rows)

def _parse_cb(data: str):
    parts = (data or "").split(":")
    if len(parts) != 4:
        return None
    if parts[0] != "waifu":
        return None
    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except Exception:
        return None
    action = parts[3]
    if action not in ("next", "pref"):
        return None
    return chat_id, user_id, action

async def _fetch_waifu(tag: str | None):
    params = {"IsNsfw": "All", "Gif": "False"}
    if tag:
        params["IncludedTags"] = tag
    session = await get_http_session()
    async with session.get(
        "https://api.waifu.im/images",
        params=params,
        timeout=aiohttp.ClientTimeout(total=15)
    ) as resp:
        if resp.status != 200:
            return None, resp.status
        data = await resp.json()
    images = data.get("items")
    if not images:
        return None, 200
    return images[0], 200

async def waifu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if not _is_nsfw_enabled(chat.id, chat.type):
        return await msg.reply_text("❌ NSFW tidak diaktifkan di grup ini.")
    if not context.args:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏷️ Daftar Tag Waifu", url="https://www.waifu.im/tags")]
        ])
        return await msg.reply_text(
            "💖 <b>Waifu Command</b>\n\n"
            "• <code>/waifu random</code>\n"
            "• <code>/waifu maid</code>\n"
            "• <code>/waifu raiden-shogun</code>\n\n"
            "Klik tombol di bawah untuk lihat tag 👇",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard
        )
    keyword = context.args[0].lower()
    tag = None if keyword == "random" else keyword
    key = _state_key(chat.id, user.id)
    _WAIFU_LAST_TAG[key] = tag
    _cleanup(key)
    img, status = await _fetch_waifu(tag)
    if status != 200:
        return await msg.reply_text(f"❌ API Error ({status})")
    if not img:
        return await msg.reply_text("❌ Waifu tidak ditemukan 😭")
    _push(key, img)
    await msg.reply_photo(
        photo=img["url"],
        caption=_build_caption(img, tag),
        parse_mode="HTML",
        reply_markup=_build_kb(chat.id, user.id, img)
    )

async def waifu_next_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    q = update.callback_query
    if not q or not q.message:
        return
    parsed = _parse_cb(q.data)
    if not parsed:
        return await q.answer()
    chat_id, owner_id, action = parsed
    user = update.effective_user
    if not user:
        return await q.answer()
    if user.id != owner_id:
        return await q.answer("Bukan punya lu goblok.", show_alert=True)
    key = _state_key(chat_id, owner_id)
    _cleanup(key)
    tag = _WAIFU_LAST_TAG.get(key)
    img, status = await _fetch_waifu(tag)
    if status != 200:
        return await q.answer("API error", show_alert=True)
    if not img:
        return await q.answer("Waifu kosong.", show_alert=True)
    _push(key, img)
    await q.message.edit_media(
        media=InputMediaPhoto(media=img["url"], caption=_build_caption(img, tag), parse_mode="HTML"),
        reply_markup=_build_kb(chat_id, owner_id, img)
    )
    await q.answer()

async def waifu_pref_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    q = update.callback_query
    if not q or not q.message:
        return
    parsed = _parse_cb(q.data)
    if not parsed:
        return await q.answer()
    chat_id, owner_id, action = parsed
    user = update.effective_user
    if not user:
        return await q.answer()
    if user.id != owner_id:
        return await q.answer("Bukan punya lu goblok.", show_alert=True)
    key = _state_key(chat_id, owner_id)
    img = _pop(key)
    if not img:
        return await q.answer("Ga ada waifu sebelumnya.", show_alert=True)
    tag = _WAIFU_LAST_TAG.get(key)
    await q.message.edit_media(
        media=InputMediaPhoto(media=img["url"], caption=_build_caption(img, tag), parse_mode="HTML"),
        reply_markup=_build_kb(chat_id, owner_id, img)
    )
    await q.answer()

try:
    _nsfw_db_init()
except Exception:
    pass