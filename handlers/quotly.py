import io
import os
import re
import time
import asyncio
import aiohttp
import logging
from telegram import Update
from telegram.ext import ContextTypes

from utils import recent_messages

log = logging.getLogger(__name__)
QUOTE_API_URI = os.getenv("QUOTE_API_URI")
_MAX_QUOTE_COUNT = 10


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

    # Support semi-transparent hex: "//#292232"
    if arg.startswith("//#") and len(arg) in (6, 9):
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

    uid = int(getattr(sender, "id", 0) or 0)
    username = getattr(sender, "username", None)

    payload = {
        "id": uid,
        "username": username,
    }

    if hasattr(sender, "title"):
        payload["first_name"] = getattr(sender, "title", None) or "Channel"
        payload["last_name"] = ""
    else:
        payload["first_name"] = getattr(sender, "first_name", None) or "User"
        payload["last_name"] = getattr(sender, "last_name", None) or ""

    photo = getattr(sender, "photo", None)
    if photo:
        big_file_id = getattr(photo, "big_file_id", None)
        if big_file_id:
            payload["photo"] = {"big_file_id": big_file_id}

    emoji_status = getattr(sender, "emoji_status", None)
    if emoji_status:
        custom_emoji_id = getattr(emoji_status, "custom_emoji_id", None)
        if custom_emoji_id:
            payload["emoji_status"] = custom_emoji_id

    return payload


def _get_message_text_and_entities(message):
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    entities = message.entities if getattr(message, "text", None) else getattr(message, "caption_entities", None)
    return text, entities


def _extract_media_list(message) -> tuple[list[dict] | None, str | None, int | None]:
    """Extract media list and mediaType for quote-api.
    
    quote-api expects media to be an ARRAY of file objects.
    Returns (media_array, media_type, duration)
    """
    # Photo: send all photo sizes as an array (quote-api picks the best size)
    photos = getattr(message, "photo", None)
    if photos:
        items = []
        for p in photos:
            items.append({
                "file_id": p.file_id,
                "width": getattr(p, "width", 512),
                "height": getattr(p, "height", 512),
            })
        if items:
            return items, "photo", None

    # Sticker
    sticker = getattr(message, "sticker", None)
    if sticker:
        thumb = getattr(sticker, "thumbnail", None) or getattr(sticker, "thumb", None)
        item = {
            "file_id": sticker.file_id,
            "width": getattr(sticker, "width", 512),
            "height": getattr(sticker, "height", 512),
            "is_animated": getattr(sticker, "is_animated", False),
            "is_video": getattr(sticker, "is_video", False),
        }
        if thumb:
            item["thumb"] = {
                "file_id": thumb.file_id,
                "width": getattr(thumb, "width", 100),
                "height": getattr(thumb, "height", 100),
            }
        return [item], "sticker", None

    # Animation / GIF
    animation = getattr(message, "animation", None)
    if animation:
        thumb = getattr(animation, "thumbnail", None) or getattr(animation, "thumb", None)
        file_id = thumb.file_id if thumb else animation.file_id
        item = {
            "file_id": file_id,
            "width": getattr(animation, "width", 512),
            "height": getattr(animation, "height", 512),
        }
        return [item], "animation", getattr(animation, "duration", None)

    # Video
    video = getattr(message, "video", None)
    if video:
        thumb = getattr(video, "thumbnail", None) or getattr(video, "thumb", None)
        file_id = thumb.file_id if thumb else video.file_id
        item = {
            "file_id": file_id,
            "width": getattr(video, "width", 512),
            "height": getattr(video, "height", 512),
        }
        return [item], "video", getattr(video, "duration", None)

    # Video note
    video_note = getattr(message, "video_note", None)
    if video_note:
        thumb = getattr(video_note, "thumbnail", None) or getattr(video_note, "thumb", None)
        file_id = thumb.file_id if thumb else video_note.file_id
        return [{"file_id": file_id}], "video_note", getattr(video_note, "duration", None)

    # Document as image
    document = getattr(message, "document", None)
    if document:
        mime = getattr(document, "mime_type", "") or ""
        if mime.startswith("image/"):
            thumb = getattr(document, "thumbnail", None) or getattr(document, "thumb", None)
            file_id = thumb.file_id if thumb else document.file_id
            return [{"file_id": file_id}], "photo", None

    return None, None, None


def _extract_voice(message):
    """Extract voice waveform and duration if available."""
    voice = getattr(message, "voice", None)
    if not voice:
        return None

    waveform_bytes = getattr(voice, "waveform", None)
    waveform = []
    if waveform_bytes:
        try:
            waveform = list(waveform_bytes)
        except Exception:
            pass

    return {
        "waveform": waveform,
        "duration": getattr(voice, "duration", 0) or 0,
    }


def _build_reply_payload(message):
    """Build replyMessage context. If replied message has no text, provide
    a descriptive placeholder so quote-api will always render the reply block.
    """
    reply = getattr(message, "reply_to_message", None)
    if not reply:
        return {}

    reply_sender = _get_sender_obj(reply)
    reply_text, reply_entities = _get_message_text_and_entities(reply)

    # If the replied message has no text (e.g. photo or sticker), create a label
    # so that index.js (which requires both name AND text) will render the reply block.
    reply_thumb_file_id = None

    if not reply_text:
        if getattr(reply, "photo", None):
            reply_text = "📷 Photo"
            reply_thumb_file_id = reply.photo[-1].file_id
        elif getattr(reply, "sticker", None):
            stk = reply.sticker
            emoji = getattr(stk, "emoji", "")
            reply_text = f"{emoji} Sticker" if emoji else "🎭 Sticker"
            thumb = getattr(stk, "thumbnail", None) or getattr(stk, "thumb", None)
            reply_thumb_file_id = thumb.file_id if thumb else stk.file_id
        elif getattr(reply, "animation", None):
            reply_text = "GIF"
            thumb = getattr(reply.animation, "thumbnail", None) or getattr(reply.animation, "thumb", None)
            if thumb:
                reply_thumb_file_id = thumb.file_id
        elif getattr(reply, "video", None):
            reply_text = "📹 Video"
            thumb = getattr(reply.video, "thumbnail", None) or getattr(reply.video, "thumb", None)
            if thumb:
                reply_thumb_file_id = thumb.file_id
        elif getattr(reply, "voice", None):
            reply_text = "🎤 Voice message"
        elif getattr(reply, "audio", None):
            reply_text = "🎵 Audio"
        elif getattr(reply, "document", None):
            doc_name = getattr(reply.document, "file_name", "Document")
            reply_text = f"📁 {doc_name}"
        else:
            reply_text = "Pesan"

    if len(reply_text) > 200:
        reply_text = reply_text[:200].rstrip() + "..."

    if reply_sender and hasattr(reply_sender, "title"):
        reply_name = getattr(reply_sender, "title", None) or "Channel"
        reply_chat_id = int(getattr(reply_sender, "id", 0) or 0)
    elif reply_sender:
        first = (getattr(reply_sender, "first_name", "") or "").strip()
        last = (getattr(reply_sender, "last_name", "") or "").strip()
        username = (getattr(reply_sender, "username", "") or "").strip()
        reply_name = first or f"{first} {last}".strip() or (f"@{username}" if username else "User")
        reply_chat_id = int(getattr(reply_sender, "id", 0) or 0)
    else:
        reply_name = "User"
        reply_chat_id = 0

    payload = {
        "name": reply_name,
        "chatId": reply_chat_id,
        "text": reply_text,
        "entities": _entities_to_quote(reply_entities),
    }

    if reply_thumb_file_id:
        payload["media"] = {"fileId": reply_thumb_file_id}

    return payload


# --- MTProto fallback for stripped nested replies ----------------------------
# The Bot API deliberately drops `reply_to_message` from the message inside
# another `reply_to_message`, so for grand-replies the bot never sees the
# context. When the in-memory buffer has no richer copy either, we ask the
# Telethon (MTProto) bot session — already used by the uploader — to re-fetch
# the message and read its reply link directly.

_TELETHON_ENTITY_TYPES = {
    "bold": "bold",
    "italic": "italic",
    "underline": "underline",
    "strike": "strikethrough",
    "spoiler": "spoiler",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
    "customemoji": "custom_emoji",
    "mention": "mention",
    "mentionname": "text_mention",
    "hashtag": "hashtag",
    "cashtag": "cashtag",
    "botcommand": "bot_command",
    "url": "url",
    "email": "email",
    "phone": "phone_number",
    "texturl": "text_link",
}


def _telethon_entity_type(entity) -> str:
    name = type(entity).__name__
    if name.startswith("MessageEntity"):
        name = name[len("MessageEntity"):]
    key = name.lower()
    if key == "blockquote" and getattr(entity, "collapsed", False):
        return "expandable_blockquote"
    return _TELETHON_ENTITY_TYPES.get(key, key)


def _telethon_entities_to_quote(entities) -> list:
    out = []
    for ent in entities or []:
        item = {
            "offset": int(getattr(ent, "offset", 0) or 0),
            "length": int(getattr(ent, "length", 0) or 0),
            "type": _telethon_entity_type(ent),
        }
        url = getattr(ent, "url", None)
        if url:
            item["url"] = url
        language = getattr(ent, "language", None)
        if language:
            item["language"] = language
        document_id = getattr(ent, "document_id", None)
        if document_id is not None:
            item["custom_emoji_id"] = str(document_id)
        out.append(item)
    return out


def _telethon_sender_name(sender, sender_id):
    if sender is not None:
        sender_id = int(getattr(sender, "id", 0) or sender_id or 0)
        title = getattr(sender, "title", None)
        if title:
            return title, sender_id
        first = (getattr(sender, "first_name", "") or "").strip()
        last = (getattr(sender, "last_name", "") or "").strip()
        username = (getattr(sender, "username", "") or "").strip()
        name = f"{first} {last}".strip() or (f"@{username}" if username else "User")
        return name, sender_id
    try:
        fallback = int(sender_id or 0)
    except (TypeError, ValueError):
        fallback = 0
    return (f"User {fallback}" if fallback else "User"), fallback


async def _build_reply_payload_from_telethon(message) -> dict:
    """Build a `replyMessage` payload from a Telethon Message object."""
    text = (getattr(message, "message", None) or "").strip()

    if not text:
        if getattr(message, "photo", None):
            text = "📷 Photo"
        elif getattr(message, "sticker", None):
            emoji = getattr(message.sticker, "emoji", "")
            text = f"{emoji} Sticker" if emoji else "🎭 Sticker"
        elif getattr(message, "gif", None):
            text = "GIF"
        elif getattr(message, "video", None):
            text = "📹 Video"
        elif getattr(message, "voice", None):
            text = "🎤 Voice message"
        elif getattr(message, "audio", None):
            text = "🎵 Audio"
        elif getattr(message, "document", None):
            file_name = getattr(message.document, "file_name", None) or "Document"
            text = f"📁 {file_name}"
        else:
            text = "Pesan"

    if len(text) > 200:
        text = text[:200].rstrip() + "..."

    sender = getattr(message, "sender", None)
    if sender is None:
        try:
            sender = await message.get_sender()
        except Exception:
            sender = None
    name, chat_id = _telethon_sender_name(sender, getattr(message, "sender_id", None))

    return {
        "name": name,
        "chatId": chat_id,
        "text": text,
        "entities": _telethon_entities_to_quote(getattr(message, "entities", None)),
    }


async def _reply_payload_with_fallback(message, chat_id: int) -> dict:
    """Reply payload for `message`, falling back to an MTProto re-fetch when
    the Bot API stripped the reply chain (nested replies)."""
    payload = _build_reply_payload(message)
    if payload:
        return payload

    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return {}

    try:
        from handlers.dl.mtproto_uploader import fetch_messages_raw
    except Exception as exc:  # pragma: no cover - import guard
        log.debug("MTProto reply fallback unavailable: %s", exc)
        return {}

    try:
        fetched = await fetch_messages_raw(chat_id, message_id)
        reply_id = getattr(fetched, "reply_to_msg_id", None) if fetched else None
        if not reply_id:
            return {}
        reply = await fetch_messages_raw(chat_id, reply_id)
        if not reply:
            return {}
        return await _build_reply_payload_from_telethon(reply)
    except Exception as exc:
        log.warning("MTProto reply fallback failed | chat_id=%s msg_id=%s err=%r",
                    chat_id, message_id, exc)
        return {}


def _collect_reply_chain(start_message, count: int) -> list:
    """Walk backward along explicit reply-to links."""
    items = []
    current = start_message
    seen_ids = set()

    while current and len(items) < count:
        mid = getattr(current, "message_id", None)
        if mid in seen_ids:
            break
        if mid is not None:
            seen_ids.add(mid)
        items.append(current)
        current = getattr(current, "reply_to_message", None)

    items.reverse()
    return items


def _resolve_source_messages(chat_id: int, target, count: int,
                              backwards: bool = False,
                              cmd_msg_id: int | None = None) -> list:
    """Resolve up to `count` messages starting (default) or ending (backwards)
    at `target`, following QuotLy semantics:

    - default (forward): target is the FIRST of the range — e.g. reply to
      message 100 with `/q 3` gives [100, 101, 102].
    - backwards (`/q -3`): target is the LAST of the range —
      e.g. reply to message 100 with `/q -3` gives [98, 99, 100].

    Prefers the recent_messages buffer (natural conversation), then falls
    back to the explicit reply_to_message chain. Guarantees target is
    included in the result.
    """
    if count <= 1:
        return [target]

    target_mid = getattr(target, "message_id", None)
    if not target_mid:
        return [target]

    def _without_cmd(items: list) -> list:
        if not cmd_msg_id:
            return items
        return [m for m in items
                if getattr(m, "message_id", None) != cmd_msg_id]

    if backwards:
        chosen = _without_cmd(recent_messages.get_window(chat_id, target_mid, count))
    else:
        chosen = _without_cmd(recent_messages.get_slice_after(chat_id, target_mid, count))

    # Fall back to reply chain if the buffer did not yield enough
    if len(chosen) < count:
        chain = _collect_reply_chain(target, count)
        if backwards:
            if len(chain) > len(chosen):
                chosen = chain
        elif not chosen:
            # Forward chain from a single target has nothing to walk to,
            # but a 1-chain is still better than nothing.
            chosen = chain

    # Guarantee target is present
    if not chosen or all(getattr(m, "message_id", None) != target_mid for m in chosen):
        chosen = [target]

    # Dedupe while preserving order
    seen = set()
    deduped = []
    for m in chosen:
        mid = getattr(m, "message_id", None)
        if mid is None or mid not in seen:
            if mid is not None:
                seen.add(mid)
            deduped.append(m)

    if backwards:
        return deduped[-count:]
    return deduped[:count]


def parse_quote_args(cmd_name: str, args: list[str]) -> tuple[int, bool, bool, str | None]:
    """Parse count, direction, reply flag, and color/gradient.

    Supports (QuotLy-style):
      /q 3       → 3 messages, target is the FIRST (forward range)
      /q -3      → 3 messages, target is the LAST (backward range)
      /q3, /q-3  → same, glued form
      /q r, /qr  → include reply-context block
      /q3r       → count 3 + reply block
      /qi2, /qs5 → same options for image / stories
      colors: red, #ff0000, #111/#222, transparent, random, //#292232

    Returns (count, backwards, include_reply, color_arg).
    """
    count = 1
    backwards = False
    include_reply = False
    color_arg = None

    # 1. Glued suffix from the command name: "q3", "qr", "q2r", "q-3", "qi2", "qs5"
    base_prefix = "q"
    for p in ("qi", "qs", "q"):
        if cmd_name.startswith(p):
            base_prefix = p
            break
    suffix = cmd_name[len(base_prefix):].lower()

    m_num = re.search(r"[-+]?\d+", suffix)
    if m_num:
        val = int(m_num.group())
        if val < 0:
            backwards = True
            val = abs(val)
        count = max(1, min(val, _MAX_QUOTE_COUNT))
    if "r" in suffix:
        include_reply = True

    # 2. Arguments
    for raw in args or []:
        arg = (raw or "").strip().lower()
        if not arg:
            continue

        # "3r", "r3", "-3r", "r-3"
        if re.fullmatch(r"[-+]?\d+r|r[-+]?\d+", arg):
            m = re.search(r"[-+]?\d+", arg)
            if m:
                val = int(m.group())
                if val < 0:
                    backwards = True
                    val = abs(val)
                count = max(1, min(val, _MAX_QUOTE_COUNT))
            include_reply = True
            continue

        # Bare signed number
        if re.fullmatch(r"[-+]?\d+", arg):
            val = int(arg)
            if val < 0:
                backwards = True
                val = abs(val)
            count = max(1, min(val, _MAX_QUOTE_COUNT))
            continue

        if arg in ("r", "reply"):
            include_reply = True
            continue

        # Color, gradient, or hex
        color_arg = arg

    return count, backwards, include_reply, color_arg


async def _generate_quote(session, bot_token: str, payload: dict, output_format: str):
    """Call quote-api POST /generate.<format>?botToken=<token>"""
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
    """Base handler for quote generation.
    
    Immediately generates and sends sticker/photo without noisy progress text.
    """
    msg = update.effective_message
    if not msg:
        return

    if not QUOTE_API_URI:
        return await msg.reply_text("⚠️ Quote API belum dikonfigurasi.")

    target = msg.reply_to_message
    if target:
        # Telegram Bot API strips the `reply_to_message` field from nested messages.
        # So we try to find a richer version in our buffer.
        buf = recent_messages.get_window(msg.chat_id, target.message_id, count=1)
        if buf and buf[0].message_id == target.message_id and buf[0].reply_to_message:
            target = buf[0]
    else:
        # Fall back to last message in buffer before the command
        buf = recent_messages.get_window(msg.chat_id, msg.message_id, count=10)
        candidates = [m for m in buf if getattr(m, "message_id", None) != msg.message_id]
        if candidates:
            target = candidates[-1]
        else:
            return await msg.reply_text("❌ Reply ke pesan yang ingin dijadikan quote.")

    # Determine command name
    raw_cmd = ""
    text = (msg.text or "").strip()
    if text.startswith("/"):
        first_word = text.split()[0][1:]
        raw_cmd = first_word.split("@")[0].lower()

    count, backwards, include_reply, color_arg = parse_quote_args(raw_cmd, context.args or [])
    background_color = _pick_color(color_arg)

    # Also remember the target and command messages in rolling buffer
    recent_messages.remember(msg.chat_id, target)
    recent_messages.remember(msg.chat_id, msg)

    messages = _resolve_source_messages(
        msg.chat_id, target, count,
        backwards=backwards,
        cmd_msg_id=msg.message_id,
    )
    if not messages:
        return await msg.reply_text("❌ Pesan tidak ditemukan.")

    # Build payload messages
    api_messages = []
    for item in messages:
        text, entities = _get_message_text_and_entities(item)
        sender = _get_sender_obj(item)
        from_payload = _build_from_payload(sender)

        # Root chatId controls same-sender grouping in quote-api
        sender_id = int(from_payload.get("id") or 0)

        msg_payload = {
            "chatId": sender_id,
            "avatar": True,
            "from": from_payload,
            "text": text or "",
            "entities": _entities_to_quote(entities),
        }

        # Include reply context when requested
        if include_reply:
            reply_data = await _reply_payload_with_fallback(item, msg.chat_id)
            if reply_data:
                msg_payload["replyMessage"] = reply_data

        # Add media (photo, sticker, animation, video) as array
        media_list, media_type, duration = _extract_media_list(item)
        if media_list:
            msg_payload["media"] = media_list
            if media_type:
                msg_payload["mediaType"] = media_type
            if duration is not None:
                msg_payload["mediaDuration"] = duration

        # Add voice waveform
        voice = _extract_voice(item)
        if voice:
            msg_payload["voice"] = voice

        # Add document row
        doc = getattr(item, "document", None)
        if doc and not media_list:
            msg_payload["document"] = {
                "file_name": getattr(doc, "file_name", "File"),
                "file_size": getattr(doc, "file_size", 0),
            }

        # Add audio row
        audio = getattr(item, "audio", None)
        if audio:
            audio_obj = {
                "title": getattr(audio, "title", "Audio"),
                "performer": getattr(audio, "performer", None),
                "duration": getattr(audio, "duration", 0),
            }
            thumb = getattr(audio, "thumbnail", None) or getattr(audio, "thumb", None)
            if thumb:
                audio_obj["thumb"] = thumb.file_id
            msg_payload["audio"] = audio_obj

        api_messages.append(msg_payload)

    # Filter messages that have no renderable content
    valid_messages = [
        m for m in api_messages
        if m.get("text") or m.get("media") or m.get("voice") or m.get("document") or m.get("audio")
    ]
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
                # Send directly as sticker
                sticker = io.BytesIO(image_bytes)
                sticker.name = f"quote.{output_format}"
                await context.bot.send_sticker(
                    chat_id=msg.chat_id,
                    sticker=sticker,
                    reply_to_message_id=target.message_id,
                    **kwargs,
                )
            else:
                # Send as photo (wallpaper / stories)
                photo = io.BytesIO(image_bytes)
                photo.name = f"quote.{output_format}"
                await context.bot.send_photo(
                    chat_id=msg.chat_id,
                    photo=photo,
                    reply_to_message_id=target.message_id,
                    **kwargs,
                )

    except Exception as e:
        log.error("Quote generation failed: %s", e, exc_info=True)
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
