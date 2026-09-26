import io
import os
import aiohttp
import logging
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)
QUOTE_API_URI = os.getenv("QUOTE_API_URI")


def _entity_type_value(entity_type):
    if hasattr(entity_type, "value"):
        return entity_type.value
    return str(entity_type).replace("MessageEntityType.", "").lower()


def _entities_to_quote(entities):
    out = []
    for ent in entities or []:
        item = {
            "offset": int(ent.offset),
            "length": int(ent.length),
            "type": _entity_type_value(ent.type),
        }
        if getattr(ent, "url", None):
            item["url"] = ent.url
        if getattr(ent, "language", None):
            item["language"] = ent.language
        if getattr(ent, "custom_emoji_id", None):
            item["custom_emoji_id"] = ent.custom_emoji_id
        out.append(item)
    return out


def _pick_color(arg: str | None) -> str:
    color_map = {
        "black": "//#292232",
        "dark": "//#292232",
        "purple": "//#292232",
        "white": "#ffffff",
        "gray": "#3a3f44",
        "grey": "#3a3f44",
        "lightgray": "#d3d3d3",
        "lightgrey": "#d3d3d3",
        "silver": "#c0c0c0",    
        "red": "#ff0000",
        "darkred": "#8b0000",
        "maroon": "#800000",
        "crimson": "#dc143c",
        "tomato": "#ff6347",
        "coral": "#ff7f50",
        "salmon": "#fa8072",
        "orangered": "#ff4500",   
        "pink": "#ea80ff",
        "hotpink": "#ff69b4",
        "deeppink": "#ff1493",
        "lightpink": "#ffb6c1",
        "rose": "#ff007f",
        "fuchsia": "#ff00ff",
        "magenta": "#ff00ff",    
        "blue": "#0000ff",
        "darkblue": "#00008b",
        "navy": "#000080",
        "royalblue": "#4169e1",
        "dodgerblue": "#1e90ff",
        "deepskyblue": "#00bfff",
        "skyblue": "#87ceeb",
        "lightskyblue": "#87cefa",
        "steelblue": "#4682b4",
        "cyan": "#00ffff",
        "aqua": "#00ffff",
        "teal": "#008080",
        "turquoise": "#40e0d0",    
        "green": "#2fbf71",
        "darkgreen": "#006400",
        "lime": "#00ff00",
        "limegreen": "#32cd32",
        "lightgreen": "#90ee90",
        "forestgreen": "#228b22",
        "seagreen": "#2e8b57",
        "springgreen": "#00ff7f",
        "olive": "#808000",    
        "yellow": "#ffff00",
        "gold": "#ffd700",
        "goldenrod": "#daa520",
        "orange": "#ffa500",
        "darkorange": "#ff8c00",
        "amber": "#ffbf00",
        "khaki": "#f0e68c",    
        "brown": "#8b4513",
        "saddlebrown": "#8b4513",
        "chocolate": "#d2691e",
        "peru": "#cd853f",
        "tan": "#d2b48c",
        "beige": "#f5f5dc",    
        "indigo": "#4b0082",
        "violet": "#8a2be2",
        "plum": "#dda0dd",
        "lavender": "#e6e6fa",
        "transparent": "rgba(0,0,0,0)",
        "random": "random",
    }

    if not arg:
        return "//#292232"

    arg = arg.strip().lower()

    if arg in color_map:
        return color_map[arg]

    # Support gradient: "#111/#222"
    if "/" in arg and not arg.startswith("//"):
        parts = arg.split("/")
        if len(parts) == 2 and all(p.startswith("#") for p in parts):
            return arg

    # Support hex color
    if arg.startswith("#") and len(arg) in (4, 7):
        return arg

    return "//#292232"


def _get_sender_obj(message):
    if getattr(message, "from_user", None):
        return message.from_user
    if getattr(message, "sender_chat", None):
        return message.sender_chat
    return None


def _build_from_payload(sender):
    if not sender:
        return {
            "id": 0,
            "first_name": "User",
            "last_name": "",
            "username": None,
        }

    payload = {
        "id": int(getattr(sender, "id", 0) or 0),
        "username": getattr(sender, "username", None),
    }

    # Handle channel/chat vs user
    if hasattr(sender, "title"):
        payload["first_name"] = getattr(sender, "title", None)
        payload["last_name"] = ""
    else:
        payload["first_name"] = getattr(sender, "first_name", None)
        payload["last_name"] = getattr(sender, "last_name", None)

    # Avatar from profile photos
    photo = getattr(sender, "photo", None)
    if photo:
        big_file_id = getattr(photo, "big_file_id", None)
        if big_file_id:
            payload["photo"] = {"big_file_id": big_file_id}

    # Emoji status (custom emoji)
    emoji_status = getattr(sender, "emoji_status", None)
    if emoji_status:
        custom_emoji_id = getattr(emoji_status, "custom_emoji_id", None)
        if custom_emoji_id:
            payload["emoji_status"] = custom_emoji_id

    return payload


def _get_message_text_and_entities(message):
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    entities = message.entities if getattr(message, "text", None) else message.caption_entities
    return text, entities


def _extract_media(message):
    """Extract media (photo, sticker, video) from message."""
    # Photo
    if getattr(message, "photo", None):
        photos = message.photo
        if photos:
            largest = max(photos, key=lambda p: p.width * p.height)
            return {
                "file_id": largest.file_id,
                "width": largest.width,
                "height": largest.height,
            }

    # Sticker
    if getattr(message, "sticker", None):
        sticker = message.sticker
        return {
            "file_id": sticker.file_id,
            "width": getattr(sticker, "width", 512),
            "height": getattr(sticker, "height", 512),
            "is_animated": getattr(sticker, "is_animated", False),
            "is_video": getattr(sticker, "is_video", False),
        }

    # Document as image
    if getattr(message, "document", None):
        doc = message.document
        mime = getattr(doc, "mime_type", "")
        if mime.startswith("image/"):
            return {
                "file_id": doc.file_id,
            }

    return None


def _extract_voice(message):
    """Extract voice waveform if available."""
    voice = getattr(message, "voice", None)
    if not voice:
        return None

    waveform_bytes = getattr(voice, "waveform", None)
    if not waveform_bytes:
        return None

    # Telegram voice waveform is 5-bit encoded, 100 samples
    # Convert bytes to list of integers
    try:
        waveform = list(waveform_bytes)
        if waveform:
            return {"waveform": waveform}
    except Exception as e:
        log.warning(f"Failed to extract waveform: {e}")

    return None


def _build_reply_payload(message):
    reply = getattr(message, "reply_to_message", None)
    if not reply:
        return {}

    reply_sender = _get_sender_obj(reply)
    reply_text, reply_entities = _get_message_text_and_entities(reply)

    if not reply_text:
        return {}

    if len(reply_text) > 200:
        reply_text = reply_text[:200].rstrip() + "..."

    if reply_sender and hasattr(reply_sender, "title"):
        reply_name = getattr(reply_sender, "title", None) or "User"
        reply_chat_id = int(getattr(reply_sender, "id", 0) or 0)
    else:
        first = (getattr(reply_sender, "first_name", "") or "").strip()
        last = (getattr(reply_sender, "last_name", "") or "").strip()
        username = (getattr(reply_sender, "username", "") or "").strip()
        reply_name = first or f"{first} {last}".strip() or (f"@{username}" if username else "User")
        reply_chat_id = int(getattr(reply_sender, "id", 0) or 0)

    return {
        "name": reply_name,
        "chatId": reply_chat_id,
        "text": reply_text,
        "entities": _entities_to_quote(reply_entities),
    }


def _collect_reply_chain(start_message, count: int):
    items = []
    current = start_message

    while current and len(items) < count:
        items.append(current)
        current = getattr(current, "reply_to_message", None)

    items.reverse()
    return items


def _parse_args(args: list[str]):
    count = 1
    include_reply = False
    color_arg = None

    for raw in args or []:
        arg = (raw or "").strip().lower()
        if not arg:
            continue

        if arg.isdigit():
            count = max(1, min(int(arg), 10))
            continue

        if arg in ("r", "reply"):
            include_reply = True
            continue

        # Color or gradient
        color_arg = arg

    return count, include_reply, color_arg


async def _generate_quote(session, bot_token: str, payload: dict, output_format: str):
    """Generate quote via quote-api.
    
    Args:
        output_format: 'webp' for sticker, 'png' for image
    """
    endpoint = f"{QUOTE_API_URI.rstrip('/')}/generate.{output_format}?botToken={bot_token}"
    
    async with session.post(
        endpoint,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=45),
    ) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise RuntimeError(f"Quote API {resp.status}: {err[:300]}")
        return await resp.read()


async def _quote_base(update: Update, context: ContextTypes.DEFAULT_TYPE, quote_type: str, output_format: str):
    """Base handler for all quote commands.
    
    Args:
        quote_type: 'quote' (sticker), 'image' (wallpaper), 'stories' (9:16)
        output_format: 'webp' or 'png'
    """
    msg = update.effective_message
    if not msg:
        return

    if not QUOTE_API_URI:
        return await msg.reply_text("⚠️ Quote API belum dikonfigurasi.")

    target = msg.reply_to_message
    if not target:
        return await msg.reply_text("❌ Reply ke pesan yang ingin dijadikan quote.")

    count, include_reply, color_arg = _parse_args(context.args or [])
    background_color = _pick_color(color_arg)

    messages = _collect_reply_chain(target, count)
    if not messages:
        return await msg.reply_text("❌ Pesan tidak ditemukan.")

    # Build payload messages
    api_messages = []
    for item in messages:
        text, entities = _get_message_text_and_entities(item)
        sender = _get_sender_obj(item)
        from_payload = _build_from_payload(sender)

        msg_payload = {
            "chatId": int(from_payload["id"] or 0),
            "avatar": True,
            "from": from_payload,
            "text": text or "",
            "entities": _entities_to_quote(entities),
        }

        # Add reply context if requested
        if include_reply:
            reply_data = _build_reply_payload(item)
            if reply_data:
                msg_payload["replyMessage"] = reply_data

        # Add media (photo/sticker)
        media = _extract_media(item)
        if media:
            msg_payload["media"] = media
            # Sticker type
            if "is_animated" in media or "is_video" in media:
                msg_payload["mediaType"] = "sticker"

        # Add voice waveform
        voice = _extract_voice(item)
        if voice:
            msg_payload["voice"] = voice

        api_messages.append(msg_payload)

    # Filter empty messages
    valid_messages = [m for m in api_messages if m.get("text") or m.get("media") or m.get("voice")]
    if not valid_messages:
        return await msg.reply_text("❌ Tidak ada konten yang bisa dijadikan quote.")

    kwargs = {}
    if getattr(msg, "message_thread_id", None):
        kwargs["message_thread_id"] = msg.message_thread_id

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "type": quote_type,
                "format": output_format,
                "backgroundColor": background_color,
                "width": 512,
                "height": 768,
                "scale": 2,
                "emojiBrand": "apple",
                "messages": valid_messages,
            }

            image_bytes = await _generate_quote(session, context.bot.token, payload, output_format)

            if quote_type == "quote":
                # Send as sticker
                sticker = io.BytesIO(image_bytes)
                sticker.name = f"quote.{output_format}"
                await context.bot.send_sticker(
                    chat_id=msg.chat_id,
                    sticker=sticker,
                    reply_to_message_id=target.message_id,
                    **kwargs,
                )
            else:
                # Send as photo (image/stories)
                photo = io.BytesIO(image_bytes)
                photo.name = f"quote.{output_format}"
                await context.bot.send_photo(
                    chat_id=msg.chat_id,
                    photo=photo,
                    reply_to_message_id=target.message_id,
                    **kwargs,
                )

    except Exception as e:
        log.error(f"Quote generation failed: {e}", exc_info=True)
        await msg.reply_text(f"❌ Gagal membuat quote: {e}")


async def q_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate quote sticker (webp)."""
    await _quote_base(update, context, quote_type="quote", output_format="webp")


async def qi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate quote image with wallpaper (png)."""
    await _quote_base(update, context, quote_type="image", output_format="png")


async def qs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate quote for stories (720x1280 png)."""
    await _quote_base(update, context, quote_type="stories", output_format="png")
