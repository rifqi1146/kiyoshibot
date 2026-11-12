from __future__ import annotations

import html
import inspect
import json
import logging
import re

from database.moderation_db import lookup_user_id, sudo_is
from utils.config import LOG_CHAT_ID, OWNER_ID
from utils.http import get_http_session


from .permissions import MUTED_PERMISSIONS, UNMUTED_PERMISSIONS

log = logging.getLogger(__name__)

SUPPORTED_ACTIONS = (
    "mute_user",
    "unmute_user",
    "ban_user",
    "unban_user",
    "kick_user",
    "promote_user",
    "demote_user",
    "tag_user",
)

_TARGET_DESC = {
    "type": "string",
    "description": "Target: @username, numeric user_id, or \"reply\" to act on the user being replied to.",
}
_DURATION_DESC = {
    "type": "string",
    "description": "Optional duration like 30m, 2h, 1d, 1w, 1mo, 1y. Indonesian forms (10 menit, 1 hari, 2 bulan, 1 tahun) are accepted. Empty means permanent.",
}

_LAST_TARGET: dict[int, tuple[int, str]] = {}


def remember_target(chat_id: int, user_id: int, name: str):
    _LAST_TARGET[int(chat_id)] = (int(user_id), name)


def last_target(chat_id: int) -> tuple[int, str] | None:
    return _LAST_TARGET.get(int(chat_id))

MOD_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "mute_user",
            "description": "Mute (restrict) a group member so they cannot send any message. Use for spamming, flooding, or minor misbehaviour.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": _TARGET_DESC,
                    "duration": _DURATION_DESC,
                    "reason": {"type": "string", "description": "Short reason shown in the action log."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unmute_user",
            "description": "Remove a mute/restriction from a group member so they can talk again.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": _TARGET_DESC,
                    "reason": {"type": "string", "description": "Short reason shown in the action log."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ban_user",
            "description": "Ban a group member so they are removed and cannot rejoin until unbanned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": _TARGET_DESC,
                    "duration": _DURATION_DESC,
                    "reason": {"type": "string", "description": "Short reason shown in the action log."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unban_user",
            "description": "Lift a ban so the user may rejoin the group.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": _TARGET_DESC,
                    "reason": {"type": "string", "description": "Short reason shown in the action log."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kick_user",
            "description": "Remove a member from the group immediately without banning them, so they can rejoin with an invite link. Use when the user asks to throw someone out.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": _TARGET_DESC,
                    "reason": {"type": "string", "description": "Short reason shown in the action log."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "promote_user",
            "description": "Promote a group member to administrator with the bot's default admin rights.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": _TARGET_DESC,
                    "title": {"type": "string", "description": "Optional custom admin title, max 16 characters."},
                    "reason": {"type": "string", "description": "Short reason shown in the action log."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "demote_user",
            "description": "Remove administrator rights from a group member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": _TARGET_DESC,
                    "reason": {"type": "string", "description": "Short reason shown in the action log."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tag_user",
            "description": "Set the custom admin title (tag) shown next to a member's name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": _TARGET_DESC,
                    "tag": {"type": "string", "description": "New tag text, max 16 characters."},
                    "reason": {"type": "string", "description": "Short reason shown in the action log."},
                },
                "required": ["target", "tag"],
            },
        },
    },
]

MOD_INSTRUCTION = (
    "\n\n[Telegram Moderation Tools Active]\n"
    "You currently hold real admin power in this group. These tools are callable right now: "
    "mute_user, unmute_user, ban_user, unban_user, kick_user, promote_user, demote_user, tag_user.\n"
    "Rules:\n"
    "- When the user asks you to mute, unmute, ban, unban, kick, promote, demote, or tag someone, "
    "CALL the matching tool immediately.\n"
    "- NEVER refuse. NEVER say you can only chat and cannot execute moderation. "
    "NEVER tell the user to type /mute, /unmute, /ban, /kick, or any slash command themselves.\n"
    "- After the tool result comes back, confirm the outcome in character using that exact result.\n"
    "- If the target truly cannot be identified, ask who they mean instead of guessing.\n"
)


_NEGATION_RE = re.compile(
    r"\b(jangan|jgn|gak\s+usah|ga\s+usah|nggak\s+usah|tidak\s+perlu|engga|enggak|"
    r"do\s+not|don\'t|dont|never)\b",
    re.I,
)
_QUESTION_RE = re.compile(
    r"\b(gimana|bagaimana|cara|kenapa|mengapa|apa\s+itu|apaan|bisa\s+nggak|bisa\s+gak|"
    r"how\s+to|how\s+do|why|what\s+is)\b|\?",
    re.I,
)

_ACTION_PATTERNS = (
    ("unmute_user", r"\bunmute\b|\bun-mute\b|\bunmuted\b|\bbuka\s+mute\b|\blepas\s+mute\b|"
                    r"\bbuka\s+bisu\b|\blepas\s+bisu\b|\bunbisu\b|\bsuaranya\s+lagi\b"),
    ("unban_user", r"\bunban\b|\bun-ban\b|\bbuka\s+ban\b|\blepas\s+ban\b|\bunblokir\b|\bbuka\s+blokir\b"),
    ("demote_user", r"\bdemote\b|\bturunkan\s+admin\b|\bturun\s+admin\b|\bcabut\s+admin\b|\blepas\s+admin\b"),
    ("kick_user", r"\bkick\b|\btendang\b|\bkeluarin\b|\bkeluarkan\b|\busir\b|\bdepak\b"),
    ("promote_user", r"\bpromote\b|\bjadikan\s+admin\b|\bnaikkan\s+admin\b|\bangkat\s+admin\b"),
    ("mute_user", r"\bmute\b|\bbisukan\b|\bmembisukan\b|\bbisu\b|\bbungkam\b|\bdiamin\b"),
    ("ban_user", r"\bban\b|\bbanned\b|\bblokir\b"),
    ("tag_user", r"\badmin\s+title\b|\bcustom\s+title\b|\bganti\s+tag\b|\bset\s+tag\b"),
)


def infer_moderation_action(text: str) -> str | None:
    low = (text or "").lower()
    if not low or _NEGATION_RE.search(low):
        return None
    for action, pattern in _ACTION_PATTERNS:
        if re.search(pattern, low):
            return action
    return None


def is_explicit_moderation_request(text: str) -> bool:
    low = (text or "").lower()
    if not low or _NEGATION_RE.search(low) or _QUESTION_RE.search(low):
        return False
    return infer_moderation_action(low) is not None

FULL_ADMIN_RIGHTS = {
    "can_manage_chat": True,
    "can_delete_messages": True,
    "can_manage_video_chats": True,
    "can_restrict_members": True,
    "can_change_info": True,
    "can_invite_users": True,
    "can_pin_messages": True,
    "can_manage_tags": True,
}

_ACTION_LABEL = {
    "mute_user": "Mute",
    "unmute_user": "Unmute",
    "ban_user": "Ban",
    "unban_user": "Unban",
    "kick_user": "Kick",
    "promote_user": "Promote",
    "demote_user": "Demote",
    "tag_user": "Tag",
}

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "detik": 1, "dtk": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "menit": 60, "mnt": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "jam": 3600, "jm": 3600,
    "d": 86400, "day": 86400, "days": 86400, "hari": 86400,
    "w": 604800, "week": 604800, "weeks": 604800, "minggu": 604800, "mgg": 604800,
    "mo": 2592000, "mos": 2592000, "mon": 2592000, "month": 2592000, "months": 2592000,
    "bln": 2592000, "buln": 2592000, "bulan": 2592000,
    "y": 31536000, "yr": 31536000, "yrs": 31536000, "year": 31536000, "years": 31536000,
    "thn": 31536000, "th": 31536000, "tahun": 31536000,
}

_UNIT_HUMAN = {
    31536000: "year",
    2592000: "month",
    604800: "week",
    86400: "day",
    3600: "hour",
    60: "minute",
    1: "second",
}

_DURATION_RE = re.compile(r"(\d+)\s*([a-zA-Z]+)")


def parse_duration_seconds(raw: str | None) -> int:
    text = (raw or "").strip().lower()
    if not text:
        return 0
    if text in ("permanent", "forever", "selamanya", "permanen", "0"):
        return 0
    text = re.sub(r"\bse(detik|menit|jam|hari|minggu|bulan|tahun)\b", r"1 \1", text)
    m = _DURATION_RE.search(text)
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2)
    mult = _UNIT_SECONDS.get(unit)
    if mult is None:
        for key in sorted(_UNIT_SECONDS, key=len, reverse=True):
            if unit.startswith(key):
                mult = _UNIT_SECONDS[key]
                break
    if mult is None:
        mult = 86400
    sec = n * mult
    if sec > 0:
        sec = max(30, min(sec, 365 * 86400))
    return sec


def humanize_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "Permanent"
    for u in (31536000, 2592000, 604800, 86400, 3600, 60, 1):
        if seconds >= u and seconds % u == 0:
            n = seconds // u
            return f"{n} {_UNIT_HUMAN[u]}{'' if n == 1 else 's'}"
    best = max(u for u in _UNIT_HUMAN if seconds >= u)
    n = seconds // best
    return f"{n} {_UNIT_HUMAN[best]}{'' if n == 1 else 's'}"

def _clean_token(raw: str | None) -> str:
    t = (raw or "").strip().strip(",.;:!?\"'`")
    return t


async def resolve_target(bot, token: str | None, reply_user_id: int | None, chat_id: int | None = None) -> int | None:
    t = _clean_token(token)
    if t.lower() in ("reply", "dia", "him", "her", "target", "orang ini", "ini", "kasian"):
        t = ""
    if not t:
        if reply_user_id:
            return int(reply_user_id)
        if chat_id and last_target(chat_id):
            return int(last_target(chat_id)[0])
        return None
    if t.startswith("<a href=\"tg://user?id="):
        m = re.search(r"tg://user\?id=(\d+)", t)
        if m:
            return int(m.group(1))
    if t.isdigit():
        return int(t)
    if t.startswith("@"):
        t = t[1:]
    uid = lookup_user_id(t)
    if uid:
        return int(uid)
    try:
        chat = await bot.get_chat("@" + t)
        if getattr(chat, "id", None):
            return int(chat.id)
    except Exception as e:
        log.debug("Resolve moderation target failed | token=%s err=%r", t, e)
    if chat_id and last_target(chat_id):
        remembered_id, remembered_name = last_target(chat_id)
        if t.lower() in (remembered_name or "").lower() or (remembered_name or "").lower() in t.lower():
            return int(remembered_id)
        return int(remembered_id)
    return None


async def _is_group_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, int(user_id))
    except Exception as e:
        log.debug("Admin lookup failed | chat_id=%s user_id=%s err=%r", chat_id, user_id, e)
        return False
    return getattr(member, "status", None) in ("administrator", "creator")


async def check_actor(update, context) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if int(user.id) in OWNER_ID or sudo_is(int(user.id)):
        return True
    if chat.type not in ("group", "supergroup"):
        return False
    return await _is_group_admin(context.bot, chat.id, user.id)


async def _bot_member(bot, chat_id: int):
    try:
        return await bot.get_chat_member(chat_id, bot.id)
    except Exception as e:
        log.debug("Bot member lookup failed | chat_id=%s err=%r", chat_id, e)
        return None


async def _bot_can(bot, chat_id: int, right: str) -> bool:
    me = await _bot_member(bot, chat_id)
    if me is None:
        return False
    if getattr(me, "status", None) == "creator":
        return True
    return bool(getattr(me, right, False))


async def _protected(bot, chat_id: int, target_id: int, include_admin: bool = True) -> bool:
    if int(target_id) in OWNER_ID or sudo_is(int(target_id)):
        return True
    if int(target_id) == int(bot.id):
        return True
    if not include_admin:
        return False
    return await _is_group_admin(bot, chat_id, target_id)


async def log_action(bot, *, chat, actor, action: str, target_id: int | None, target_name: str, duration: str, reason: str, ok: bool, error: str = ""):
    if not LOG_CHAT_ID:
        return
    label = _ACTION_LABEL.get(action, action)
    actor_name = getattr(actor, "full_name", None) or getattr(actor, "username", None) or str(getattr(actor, "id", "?"))
    chat_title = getattr(chat, "title", None) or str(getattr(chat, "id", "?"))
    lines = [
        f"<b>Moderation Log — {html.escape(label)}</b>",
        f"<b>Group:</b> <code>{html.escape(str(chat_title))}</code>",
        f"<b>By:</b> {html.escape(str(actor_name))} (<code>{getattr(actor, 'id', '?')}</code>)",
    ]
    if target_id:
        lines.append(f"<b>Target:</b> {html.escape(target_name or str(target_id))} (<code>{int(target_id)}</code>)")
    else:
        lines.append(f"<b>Target:</b> {html.escape(target_name or 'unknown')}")
    if duration:
        lines.append(f"<b>Duration:</b> {html.escape(duration)}")
    if reason:
        lines.append(f"<b>Reason:</b> <code>{html.escape(reason)}</code>")
    lines.append(f"<b>Result:</b> {'OK' if ok else 'FAILED'}")
    if error:
        lines.append(f"<b>Error:</b> <code>{html.escape(error[:300])}</code>")
    try:
        await bot.send_message(LOG_CHAT_ID, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.warning("Moderation log send failed | err=%r", e)


def _log_moderation_outcome(chat_id, actor, action, target_id, target_name, duration_sec, ok, error):
    line = (
        "Moderation action done | action=%s chat_id=%s actor=%s target=%s(%s) "
        "duration=%s ok=%s error=%r"
    )
    args = (
        action,
        chat_id,
        int(getattr(actor, "id", 0) or 0),
        target_name,
        target_id,
        humanize_seconds(duration_sec) if duration_sec > 0 else "",
        ok,
        error,
    )
    if ok:
        log.info(line, *args)
    else:
        log.warning(line, *args)


async def execute_action(bot, *, chat, actor, action: str, args: dict, reply_user_id: int | None) -> dict:
    chat_id = int(chat.id)
    target_token = args.get("target")
    reason = str(args.get("reason") or "").strip()
    duration_raw = str(args.get("duration") or "").strip()
    duration_sec = parse_duration_seconds(duration_raw)

    target_id = await resolve_target(bot, target_token, reply_user_id, chat_id=chat_id)
    if not target_id:
        error = f"Could not resolve target {target_token!r}. Ask for an @username, numeric id, or a reply."
        _log_moderation_outcome(chat_id, actor, action, None, str(target_token), duration_sec, False, error)
        return {
            "ok": False,
            "error": error,
            "target_id": None,
            "summary": "Target not found",
        }

    needs_restrict_guard = action in ("mute_user", "ban_user", "kick_user")
    if await _protected(bot, chat_id, target_id, include_admin=needs_restrict_guard):
        error = "That user is an admin, sudo, owner, or the bot itself and cannot be actioned."
        _log_moderation_outcome(chat_id, actor, action, target_id, str(target_id), duration_sec, False, error)
        return {
            "ok": False,
            "error": error,
            "target_id": target_id,
            "summary": "Protected target",
        }

    target_name = str(target_id)
    try:
        member = await bot.get_chat_member(chat_id, target_id)
        u = getattr(member, "user", None)
        if u:
            target_name = u.full_name or (f"@{u.username}" if u.username else str(target_id))
    except Exception:
        pass

    summary = ""
    error = ""
    ok = False
    try:
        if action == "mute_user":
            if not await _bot_can(bot, chat_id, "can_restrict_members"):
                raise RuntimeError("Bot lacks can_restrict_members permission.")
            kwargs = {
                "chat_id": chat_id,
                "user_id": target_id,
                "permissions": MUTED_PERMISSIONS,
            }
            if duration_sec > 0:
                from datetime import datetime, timedelta, timezone
                kwargs["until_date"] = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
            await bot.restrict_chat_member(**kwargs)
            summary = f"Muted {target_name} for {humanize_seconds(duration_sec)}"
            ok = True

        elif action == "unmute_user":
            if not await _bot_can(bot, chat_id, "can_restrict_members"):
                raise RuntimeError("Bot lacks can_restrict_members permission.")
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=target_id, permissions=UNMUTED_PERMISSIONS,
            )
            summary = f"Unmuted {target_name}"
            ok = True

        elif action == "ban_user":
            if not await _bot_can(bot, chat_id, "can_restrict_members"):
                raise RuntimeError("Bot lacks can_restrict_members permission.")
            kwargs = {"chat_id": chat_id, "user_id": target_id, "revoke_messages": False}
            if duration_sec > 0:
                from datetime import datetime, timedelta, timezone
                kwargs["until_date"] = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
            await bot.ban_chat_member(**kwargs)
            summary = f"Banned {target_name} ({humanize_seconds(duration_sec)})"
            ok = True

        elif action == "unban_user":
            if not await _bot_can(bot, chat_id, "can_restrict_members"):
                raise RuntimeError("Bot lacks can_restrict_members permission.")
            await bot.unban_chat_member(chat_id=chat_id, user_id=target_id)
            summary = f"Unbanned {target_name}"
            ok = True

        elif action == "kick_user":
            if not await _bot_can(bot, chat_id, "can_restrict_members"):
                raise RuntimeError("Bot lacks can_restrict_members permission.")
            await bot.ban_chat_member(chat_id=chat_id, user_id=target_id, revoke_messages=False)
            await bot.unban_chat_member(chat_id=chat_id, user_id=target_id, only_if_banned=True)
            summary = f"Kicked {target_name}"
            ok = True

        elif action == "promote_user":
            if not await _bot_can(bot, chat_id, "can_promote_members"):
                raise RuntimeError("Bot lacks can_promote_members permission.")
            title = (str(args.get("title") or "Admin").strip() or "Admin")[:16]
            await bot.promote_chat_member(
                chat_id=chat_id, user_id=target_id, **FULL_ADMIN_RIGHTS,
            )
            try:
                await bot.set_chat_administrator_custom_title(
                    chat_id=chat_id, user_id=target_id, custom_title=title,
                )
            except Exception as e:
                log.debug("Custom title after promote failed | err=%r", e)
            summary = f"Promoted {target_name} to admin ({title})"
            ok = True

        elif action == "demote_user":
            if not await _bot_can(bot, chat_id, "can_promote_members"):
                raise RuntimeError("Bot lacks can_promote_members permission.")
            await bot.promote_chat_member(
                chat_id=chat_id,
                user_id=target_id,
                **{k: False for k in FULL_ADMIN_RIGHTS},
            )
            summary = f"Demoted {target_name}"
            ok = True

        elif action == "tag_user":
            if not await _bot_can(bot, chat_id, "can_change_info"):
                raise RuntimeError("Bot lacks can_change_info permission.")
            tag = (str(args.get("tag") or "").strip() or "Admin")[:16]
            method = getattr(bot, "set_chat_member_tag", None)
            if method is None:
                raise RuntimeError("This python-telegram-bot build has no set_chat_member_tag.")
            await method(chat_id=chat_id, user_id=target_id, tag=tag)
            summary = f"Tagged {target_name} as {tag}"
            ok = True

        else:
            raise RuntimeError(f"Unsupported action {action}")

    except Exception as e:
        error = str(e)
        summary = f"Failed to {action.replace('_user', '')} {target_name}: {error}"
        log.warning("Moderation tool failed | action=%s chat=%s target=%s err=%r", action, chat_id, target_id, e)

    _log_moderation_outcome(chat_id, actor, action, target_id, target_name, duration_sec, ok, error)

    await log_action(
        bot,
        chat=chat,
        actor=actor,
        action=action,
        target_id=target_id,
        target_name=target_name,
        duration=humanize_seconds(duration_sec) if duration_sec > 0 and action in ("mute_user", "ban_user") else "",
        reason=reason,
        ok=ok,
        error=error,
    )

    if ok and target_id:
        remember_target(chat_id, target_id, target_name)

    return {
        "ok": ok,
        "error": error,
        "target_id": target_id,
        "target_name": target_name,
        "summary": summary,
    }

async def can_use_moderation(update, context) -> bool:
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    if not chat or not user:
        return False
    if chat.type not in ("group", "supergroup"):
        return False
    return await check_actor(update, context)


def looks_like_moderation_intent(text: str) -> bool:
    return infer_moderation_action(text) is not None


def parse_text_tool_calls(text: str) -> tuple[list[dict], str]:
    if not text or "invoke" not in text:
        return [], text
    call_re = re.compile(r'invoke\s+name="([a-zA-Z0-9_]+)">(.*?)(?:invoke>|$)', re.S)
    param_re = re.compile(r'parameter\s+name="([a-zA-Z0-9_]+)"[^>]*>([^<]*)', re.S)
    calls: list[dict] = []
    for idx, m in enumerate(call_re.finditer(text)):
        name = m.group(1).strip()
        body = m.group(2)
        args = {}
        for pm in param_re.finditer(body):
            args[pm.group(1).strip()] = pm.group(2).strip()
        calls.append({
            "id": f"call_text_{idx}",
            "type": "function",
            "function": {"name": name, "arguments": args},
        })
    cleaned = re.sub(r'<[^>]*DSML[^>]*>[\s\S]*?(?:</[^>]*DSML[^>]*>|$)', '', text).strip()
    return calls, cleaned


async def call_shiteru_with_tools(
    endpoint: str,
    headers: dict,
    payload: dict,
    timeout_sec: int,
) -> tuple[str, list[dict], dict]:
    import aiohttp

    session = get_http_session()
    if inspect.isawaitable(session):
        session = await session

    async with session.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout_sec),
    ) as r:
        data = await r.json(content_type=None)
        if r.status != 200:
            err_msg = ""
            if isinstance(data, dict):
                err_msg = (data.get("error") or {}).get("message") or str(data)
            raise RuntimeError(f"HTTP {r.status}: {err_msg or 'Request error'}")

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("Response choices empty from Shiteru.")

        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []
        if not raw_calls and content:
            parsed_calls, cleaned = parse_text_tool_calls(content)
            if parsed_calls:
                raw_calls = parsed_calls
                content = cleaned
        usage = (data or {}).get("usage") or {}
        return content, raw_calls, usage


async def run_moderation_loop(
    *,
    update,
    context,
    endpoint: str,
    headers: dict,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    timeout_sec: int,
    prompt: str = "",
    usage_collector: dict | None = None,
) -> str:

    msg = update.message or update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    reply_user_id = None
    contextual_messages = list(messages)

    replied = None
    if msg and getattr(msg, "reply_to_message", None):
        replied = msg.reply_to_message.from_user

    hint = ""
    if replied is not None and getattr(replied, "is_bot", False):
        hint = (
            "[Telegram Context: you are replying to your own previous bot message. "
            "Never target yourself. Identify the member from the conversation history.]"
        )
    elif replied is not None:
        reply_user_id = int(replied.id)
        reply_name = replied.full_name or (
            f"@{replied.username}" if getattr(replied, "username", None) else str(replied.id)
        )
        hint = (
            f"[Telegram Context: this message replies to {reply_name} (user_id {reply_user_id}). "
            f"If the user named no other target, use target=\"{reply_user_id}\".]"
        )

    notes = hint
    inferred = infer_moderation_action(prompt)
    notes += MOD_INSTRUCTION
    if inferred:
        notes += f"[Detected action: {inferred}]\n"

    if notes and contextual_messages and contextual_messages[-1].get("role") == "user":
        last_content = contextual_messages[-1]["content"]
        if isinstance(last_content, str):
            contextual_messages[-1] = {
                **contextual_messages[-1],
                "content": f"{last_content}\n{notes}",
            }
        elif isinstance(last_content, list):
            new_parts = []
            appended = False
            for part in last_content:
                if isinstance(part, dict) and part.get("type") == "text" and not appended:
                    new_parts.append({**part, "text": f"{part.get('text', '')}\n{notes}"})
                    appended = True
                else:
                    new_parts.append(part)
            if not appended:
                new_parts.append({"type": "text", "text": notes})
            contextual_messages[-1] = {
                **contextual_messages[-1],
                "content": new_parts,
            }

    log.info(
        "Moderation tool request | model=%s chat_id=%s actor=%s inferred=%s prompt=%r",
        model,
        int(getattr(chat, "id", 0) or 0),
        int(getattr(actor, "id", 0) or 0),
        inferred or "",
        (prompt or "")[:120],
    )

    payload = {
        "model": model,
        "messages": contextual_messages,
        "tools": MOD_TOOLS,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
        "stream": False,
    }

    content, tool_calls, usage = await call_shiteru_with_tools(
        endpoint, headers, payload, timeout_sec,
    )
    if usage_collector is not None and isinstance(usage, dict):
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if usage.get(k):
                usage_collector[k] = usage_collector.get(k, 0) + int(usage[k])

    if not tool_calls and inferred and is_explicit_moderation_request(prompt):
        forced_payload = {
            **payload,
            "tool_choice": {"type": "function", "function": {"name": inferred}},
        }
        retry_content, retry_calls, retry_usage = await call_shiteru_with_tools(
            endpoint, headers, forced_payload, timeout_sec,
        )
        if usage_collector is not None and isinstance(retry_usage, dict):
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if retry_usage.get(k):
                    usage_collector[k] = usage_collector.get(k, 0) + int(retry_usage[k])
        if retry_calls:
            content, tool_calls = retry_content, retry_calls
        else:
            log.warning("Forced moderation tool returned no call | action=%s", inferred)

    if not tool_calls:
        log.info(
            "Moderation tool not called | chat_id=%s actor=%s inferred=%s reason=model_replied_directly",
            int(getattr(chat, "id", 0) or 0),
            int(getattr(actor, "id", 0) or 0),
            inferred or "",
        )
        return content

    log.info(
        "Moderation tool calls | chat_id=%s actor=%s calls=%s",
        int(getattr(chat, "id", 0) or 0),
        int(getattr(actor, "id", 0) or 0),
        [str((c.get("function") or {}).get("name") or "") for c in tool_calls],
    )

    step_messages = list(contextual_messages)
    step_messages.append({
        "role": "assistant",
        "content": content or None,
        "tool_calls": tool_calls,
    })

    for call in tool_calls:
        call_id = call.get("id") or "call_default"
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except Exception:
            parsed_args = {}

        result = await execute_action(
            context.bot,
            chat=chat,
            actor=actor,
            action=name,
            args=parsed_args,
            reply_user_id=reply_user_id,
        )
        tool_output = (
            f"Action {name} SUCCESS: {result['summary']}"
            if result.get("ok")
            else f"Action {name} FAILED: {result.get('error') or result.get('summary')}"
        )
        step_messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": tool_output,
        })

    follow_payload = {
        "model": model,
        "messages": step_messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
        "stream": False,
    }
    final_content, _, final_usage = await call_shiteru_with_tools(
        endpoint, headers, follow_payload, timeout_sec,
    )
    if usage_collector is not None and isinstance(final_usage, dict):
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if final_usage.get(k):
                usage_collector[k] = usage_collector.get(k, 0) + int(final_usage[k])

    return final_content
