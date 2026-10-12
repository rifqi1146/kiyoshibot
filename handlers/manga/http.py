"""HTTP fetcher, helper gambar, dan render page untuk manga reader.

- `fetch_json` / `fetch_image_bytes` / `fetch_html` : HTTP client (aiohttp session bersama).
- `enforce_telegram_photo_limits` : resize Pillow agar lolos batas Telegram.
- `safe_render_page` : edit/send foto halaman reader dengan lock kepemilikan.
- `_escape` : HTML escape helper.
"""
import asyncio
import html as html_lib
import logging
from io import BytesIO

from telegram import InputMediaPhoto

from utils.http import get_http_session

from .constants import USER_AGENT
from .state import _move_message_lock, _set_message_lock

try:
    from PIL import Image
except ImportError:
    Image = None
    logging.warning("Pillow is not installed. Long images may fail to send.")

log = logging.getLogger(__name__)


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
                raw = await response.read()
                # Sebagian halaman Maid-Manga bukan UTF-8 valid; decode toleran
                # supaya tidak menggagalkan seluruh request.
                charset = response.charset or "utf-8"
                try:
                    return raw.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    return raw.decode("utf-8", errors="replace")
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


async def send_detail_view(context, chat_id, message_thread_id, rich_html, plain_html, keyboard, owner_id=None, reply_to_message_id=None):
    """Kirim detail manga sebagai Rich Message (Bot API 10.3).

    Cover sudah dibawa sebagai `<img src="..."/>` di dalam `rich_html` sehingga
    tidak perlu upload foto terpisah. Fallback otomatis ke `sendMessage` plain HTML
    bila server Bot API tidak mendukung `sendRichMessage`.
    """
    from .rich import send_manga_rich

    try:
        sent = await send_manga_rich(
            context.bot,
            chat_id,
            rich_html,
            plain_html,
            keyboard,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )
    except Exception as e:
        log.error(f"Failed to send manga detail rich message: {e}")
        return None

    msg_id = None
    if isinstance(sent, dict):
        msg_id = sent.get("message_id")
    else:
        msg_id = getattr(sent, "message_id", None)
    if msg_id is not None and owner_id is not None:
        _set_message_lock(chat_id, msg_id, owner_id)
    return sent
