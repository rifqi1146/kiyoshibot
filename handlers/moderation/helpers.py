import html
import logging
import re
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from database.moderation_db import lookup_user_id

log = logging.getLogger(__name__)

DURATION_RE = re.compile(r"^(\d+)([a-zA-Z]+)$", re.I)
_DURATION_UNITS = {
    "s": (1, "sec"), "sec": (1, "sec"), "secs": (1, "sec"), "second": (1, "sec"), "seconds": (1, "sec"), "detik": (1, "sec"),
    "m": (60, "min"), "min": (60, "min"), "mins": (60, "min"), "minute": (60, "min"), "minutes": (60, "min"), "menit": (60, "min"),
    "h": (3600, "hour"), "hr": (3600, "hour"), "hrs": (3600, "hour"), "hour": (3600, "hour"), "hours": (3600, "hour"), "jam": (3600, "hour"),
    "d": (86400, "day"), "day": (86400, "day"), "days": (86400, "day"), "hari": (86400, "day"),
    "w": (604800, "week"), "week": (604800, "week"), "weeks": (604800, "week"), "minggu": (604800, "week"),
    "mo": (2592000, "month"), "mos": (2592000, "month"), "mon": (2592000, "month"), "month": (2592000, "month"), "months": (2592000, "month"),
    "bln": (2592000, "month"), "buln": (2592000, "month"), "bulan": (2592000, "month"),
    "y": (31536000, "year"), "yr": (31536000, "year"), "yrs": (31536000, "year"), "year": (31536000, "year"), "years": (31536000, "year"),
    "thn": (31536000, "year"), "tahun": (31536000, "year"),
}


def get_message_thread_id(msg) -> int | None:
    if not msg:
        return None

    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return None

    try:
        return int(thread_id)
    except Exception:
        return None


def get_topic_reply_kwargs(msg) -> dict:
    thread_id = get_message_thread_id(msg)
    if thread_id is None:
        return {}
    return {"message_thread_id": thread_id}


async def reply_in_topic(msg, text: str, **kwargs):
    if not msg:
        return None
    kwargs = {**get_topic_reply_kwargs(msg), **kwargs}
    return await msg.reply_text(text, **kwargs)

def _looks_like_explicit_target(token: str | None) -> bool:
    t = (token or "").strip()
    if not t:
        return False
    if t.startswith("@"):
        return True
    if t.isdigit():
        return True
    return False
    
def parse_duration(raw: str) -> tuple[datetime | None, str | None]:
    text = (raw or "").strip().lower()
    text = re.sub(r"^se(detik|menit|jam|hari|minggu|bulan|tahun)$", r"1\1", text)
    m = DURATION_RE.match(text)
    if not m:
        return None, None

    n = int(m.group(1))
    unit = m.group(2).lower()

    if n <= 0:
        return None, None

    entry = _DURATION_UNITS.get(unit)
    if not entry:
        return None, None

    mult, unit_name = entry
    seconds = max(30, min(n * mult, 365 * 86400))
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    if seconds == 365 * 86400 and n * mult > 365 * 86400:
        human = "365 days"
    elif seconds == 30 and n * mult < 30:
        human = "30 secs"
    else:
        human = f"{n} {unit_name}{'' if n == 1 else 's'}"

    return until, human


def mention_html(user_id: int, name: str) -> str:
    safe = html.escape(name or "User")
    return f'<a href="tg://user?id={int(user_id)}">{safe}</a>'


def display_name(obj) -> str:
    if not obj:
        return ""
    first = getattr(obj, "first_name", "") or ""
    last = getattr(obj, "last_name", "") or ""
    username = getattr(obj, "username", "") or ""
    name = (first + (" " + last if last else "")).strip()
    if name:
        return name
    if username:
        return f"@{username}"
    return ""


def text_mention_user_from_message(msg, token: str | None):
    if not msg or not getattr(msg, "entities", None):
        return None

    if not token:
        for ent in msg.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user
        return None

    t = (token or "").strip()
    if not t:
        return None

    text = msg.text or ""
    for ent in msg.entities:
        if ent.type == "text_mention" and ent.user:
            try:
                part = text[ent.offset : ent.offset + ent.length]
            except Exception:
                continue
            if part == t:
                return ent.user

    return None


def _merge_duration_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        cur = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if cur.isdigit() and len(cur) <= 5 and nxt and re.fullmatch(r"[a-zA-Z]+", nxt):
            if parse_duration(cur + nxt)[0] is not None:
                out.append(cur + nxt)
                i += 2
                continue

        out.append(cur)
        i += 1
    return out


def extract_duration_target_reason(args: list[str], has_reply_target: bool) -> tuple[datetime | None, str | None, str | None, str]:
    a = _merge_duration_tokens([x for x in (args or []) if (x or "").strip()])
    if not a:
        return None, None, None, "-"

    until, dur_human = parse_duration(a[0])

    if until is not None:
        if len(a) >= 2 and _looks_like_explicit_target(a[1]):
            target = a[1]
            reason = " ".join(a[2:]).strip() or "-"
            return until, dur_human, target, reason

        if has_reply_target:
            reason = " ".join(a[1:]).strip() or "-"
            return until, dur_human, None, reason

        target = a[1] if len(a) >= 2 else None
        reason = " ".join(a[2:]).strip() or "-"
        return until, dur_human, target, reason

    if _looks_like_explicit_target(a[0]):
        target = a[0]
        if len(a) >= 2:
            u2, d2 = parse_duration(a[1])
            if u2 is not None:
                reason = " ".join(a[2:]).strip() or "-"
                return u2, d2, target, reason
        reason = " ".join(a[1:]).strip() or "-"
        return None, None, target, reason
    if has_reply_target:
        reason = " ".join(a).strip() or "-"
        return None, None, None, reason

    target = a[0]
    reason = " ".join(a[1:]).strip() or "-"
    return None, None, target, reason


def extract_target_reason(args: list[str], has_reply_target: bool) -> tuple[str | None, str]:
    a = [x for x in (args or []) if (x or "").strip()]

    if not a:
        return None, "-"

    if _looks_like_explicit_target(a[0]):
        return a[0], (" ".join(a[1:]).strip() or "-")

    if has_reply_target:
        return None, (" ".join(a).strip() or "-")

    return a[0], (" ".join(a[1:]).strip() or "-")


async def resolve_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str | None) -> int | None:
    msg = update.message

    raw = (token or "").strip()
    if raw:
        ent_user = text_mention_user_from_message(msg, token)
        if ent_user and getattr(ent_user, "id", None):
            return int(ent_user.id)

        if raw.isdigit():
            return int(raw)

        if raw.startswith("@"):
            raw = raw[1:].strip()

        return lookup_user_id(raw)

    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return int(msg.reply_to_message.from_user.id)

    ent_user = text_mention_user_from_message(msg, token)
    if ent_user and getattr(ent_user, "id", None):
        return int(ent_user.id)

    return None


async def resolve_target_user_obj_for_display(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str | None):
    msg = update.message
    raw = (token or "").strip()

    if raw:
        ent_user = text_mention_user_from_message(msg, token)
        if ent_user:
            return ent_user
        return None

    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user

    ent_user = text_mention_user_from_message(msg, token)
    if ent_user:
        return ent_user

    return None


async def resolve_user_obj_for_display_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    chat = update.effective_chat

    if chat and chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(chat.id, int(user_id))
            user = getattr(member, "user", None)
            if user:
                return user
        except Exception as e:
            log.warning(
                "Failed to get chat member for moderation display | chat_id=%s user_id=%s err=%s",
                getattr(chat, "id", None),
                user_id,
                e,
            )

    try:
        return await context.bot.get_chat(int(user_id))
    except Exception as e:
        log.warning("Failed to get user chat for moderation display | user_id=%s err=%s", user_id, e)
        return None


def display_name_from_token(token: str | None) -> str:
    t = (token or "").strip()
    if not t:
        return "User"
    if t.startswith("@"):
        return t
    return "User"