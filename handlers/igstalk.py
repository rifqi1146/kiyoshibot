import os
import html
import inspect
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from utils.http import get_http_session

NEOXR_IGSTALK_API = os.getenv("NEOXR_IGSTALK_API", "https://api.neoxr.eu/api/igstalk").strip()

def _clean_text(value: str, default: str = "-") -> str:
    text = str(value or "").strip()
    return text if text else default

async def _shared_http_session():
    session = get_http_session()
    if inspect.isawaitable(session):
        session = await session
    return session

async def _fetch_ig_profile(username: str) -> dict:
    api_key = os.getenv("NEOXR_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("NEOXR_API_KEY is not set in the environment.")
    
    session = await _shared_http_session()
    params = {"username": username, "apikey": api_key}
    
    async with session.get(NEOXR_IGSTALK_API, params=params) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"API error {resp.status}: {text[:500]}")
        try:
            data = await resp.json(content_type=None)
        except Exception:
            raise RuntimeError(f"Invalid API JSON: {text[:500]}")
            
    if not data.get("status"):
        raise RuntimeError(data.get("message") or "Instagram user not found.")
    
    result = data.get("data")
    if not isinstance(result, dict):
        raise RuntimeError("Invalid profile data received.")
    return result

async def _download_photo(url: str) -> BytesIO:
    session = await _shared_http_session()
    async with session.get(url) as resp:
        if resp.status != 200:
            raise RuntimeError("Failed to download profile picture.")
        photo_bytes = await resp.read()
        buffer = BytesIO(photo_bytes)
        buffer.name = "profile.jpg"
        return buffer

def _build_ig_caption(data: dict) -> str:
    username = _clean_text(data.get("username"), "unknown")
    user_id = _clean_text(data.get("id"), "-")
    followers = f"{data.get('follower', 0):,}"
    following = f"{data.get('following', 0):,}"
    posts = f"{data.get('post', 0):,}"
    is_private = "Yes 🔒" if data.get("private") else "No 🔓"
    about = _clean_text(data.get("about"), "-")

    return (
        f"📸 <b>Instagram Profile: @{html.escape(username)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID:</b> <code>{html.escape(str(user_id))}</code>\n"
        f"👤 <b>Username:</b> @{html.escape(username)}\n"
        f"👥 <b>Followers:</b> {followers}\n"
        f"👣 <b>Following:</b> {following}\n"
        f"📦 <b>Posts:</b> {posts}\n"
        f"🔐 <b>Private:</b> {is_private}\n"
        f"📝 <b>Bio:</b>\n<i>{html.escape(about)}</i>"
    )

def _build_ig_keyboard(username: str) -> InlineKeyboardMarkup:
    url = f"https://instagram.com/{username}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open Instagram Profile", url=url)]
    ])

async def igstalk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
        
    msg = update.effective_message
    raw_query = " ".join(context.args).strip()
    
    if not raw_query:
        return await msg.reply_text(
            "🔍 <b>Instagram Stalker</b>\n\n"
            "Usage format:\n"
            "<code>/igstalk &lt;username&gt;</code>\n\n"
            "<i>Example: <code>/igstalk hosico_cat</code></i>",
            parse_mode="HTML",
        )
        
    username = raw_query.lstrip("@").split("/")[-1].split("?")[0].strip()
    status = await msg.reply_text(
        "<b>Fetching Instagram profile...</b>",
        reply_to_message_id=msg.message_id,
        parse_mode="HTML",
    )
    
    try:
        data = await _fetch_ig_profile(username)
        caption = _build_ig_caption(data)
        reply_markup = _build_ig_keyboard(data.get("username", username))
        photo_url = data.get("photo")

        if photo_url and photo_url.startswith(("http://", "https://")):
            photo_file = await _download_photo(photo_url)
            try:
                await msg.reply_photo(
                    photo=photo_file,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            finally:
                photo_file.close()
        else:
            await msg.reply_text(
                caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            
        await status.delete()

    except Exception as e:
        await status.edit_text(
            f"❌ <b>Failed to fetch profile</b>\n\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
