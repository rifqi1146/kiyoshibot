"""Centralized message ownership guard for inline keyboards.

Problem: Telegram inline buttons are not bound to a user. Any member of a group
can press a button that belongs to someone else's menu / search result / picker.
Session-based caches (`MOVIEBOX_CACHE`, `ANICHIN_CACHE`, ...) only protect
actions that carry a `session_id`; static screens (menu, latest, categories,
A-Z, schedule, popular, ...) have no session and were therefore clickable by
anyone.

Solution: when a command sends its explorer message, record
`(chat_id, message_id) -> owner_user_id` in this module. Every callback handler
calls `guard_callback_owner()` before doing any work. If the clicker is not the
recorded owner the click is rejected with an alert popup.

This is a hard lock: in a group chat an unprovable owner is denied (fail-closed),
so a stale/lost registry can never be exploited to press someone else's button.
"""
import logging
import time

log = logging.getLogger(__name__)

# (chat_id, message_id) -> (owner_user_id, registered_at)
_MESSAGE_OWNERS: dict[tuple[int, int], tuple[int, float]] = {}
_LOCK_TTL = 6 * 3600.0  # 6 hours
_MAX_LOCKS = 4000

DEFAULT_ALERT = "This menu is not yours."


def _prune(now: float) -> None:
    if len(_MESSAGE_OWNERS) <= _MAX_LOCKS:
        return
    for key in [k for k, v in list(_MESSAGE_OWNERS.items()) if now - v[1] > _LOCK_TTL]:
        _MESSAGE_OWNERS.pop(key, None)
    # Still too big -> drop the oldest entries.
    if len(_MESSAGE_OWNERS) > _MAX_LOCKS:
        ordered = sorted(_MESSAGE_OWNERS.items(), key=lambda kv: kv[1][1])
        for key, _ in ordered[: len(_MESSAGE_OWNERS) - _MAX_LOCKS]:
            _MESSAGE_OWNERS.pop(key, None)


def register_message_owner(chat_id, message_id, user_id) -> None:
    """Bind an inline-keyboard message to the user who created it."""
    try:
        chat_id = int(chat_id)
        message_id = int(message_id)
        user_id = int(user_id)
    except (TypeError, ValueError):
        return
    if not chat_id or not message_id or not user_id:
        return
    now = time.time()
    _prune(now)
    _MESSAGE_OWNERS[(chat_id, message_id)] = (user_id, now)


def get_message_owner(chat_id, message_id) -> int | None:
    """Return the recorded owner of a message, or None if unknown/expired."""
    try:
        key = (int(chat_id), int(message_id))
    except (TypeError, ValueError):
        return None
    entry = _MESSAGE_OWNERS.get(key)
    if not entry:
        return None
    user_id, ts = entry
    if time.time() - ts > _LOCK_TTL:
        _MESSAGE_OWNERS.pop(key, None)
        return None
    return user_id


def message_id_from_send(result) -> int | None:
    """Extract `message_id` from a `sendRichMessage` dict or a `Message` object."""
    if result is None:
        return None
    if isinstance(result, dict):
        mid = result.get("message_id")
        try:
            return int(mid) if mid is not None else None
        except (TypeError, ValueError):
            return None
    mid = getattr(result, "message_id", None)
    try:
        return int(mid) if mid is not None else None
    except (TypeError, ValueError):
        return None


def is_owner(q, expected_user_id=None, session_data=None) -> bool:
    """True if the callback clicker is the rightful owner of this message."""
    if not q or not getattr(q, "from_user", None):
        return False

    clicker = int(q.from_user.id)

    if expected_user_id is not None:
        try:
            return clicker == int(expected_user_id)
        except (TypeError, ValueError):
            pass

    if session_data:
        uid = session_data.get("user_id") or session_data.get("user")
        if uid is not None:
            try:
                return clicker == int(uid)
            except (TypeError, ValueError):
                pass

    msg = getattr(q, "message", None)
    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(msg, "message_id", None)

    if chat_id and message_id:
        recorded = get_message_owner(chat_id, message_id)
        if recorded is not None:
            return clicker == recorded

    # Fallback: the message replies to the original command -> its sender owns it.
    reply = getattr(msg, "reply_to_message", None)
    reply_from = getattr(reply, "from_user", None)
    if reply_from is not None:
        try:
            return clicker == int(reply_from.id)
        except (TypeError, ValueError):
            pass

    # Private chat with the bot: only one human can press the buttons.
    if getattr(chat, "type", None) == "private":
        return True

    # Group / supergroup without a provable owner -> fail closed.
    return False


async def guard_callback_owner(
    q,
    expected_user_id=None,
    session_data=None,
    alert_text: str = DEFAULT_ALERT,
) -> bool:
    """Return True when the clicker owns the message; otherwise alert and deny."""
    if is_owner(q, expected_user_id=expected_user_id, session_data=session_data):
        return True
    try:
        await q.answer(alert_text, show_alert=True)
    except Exception as e:
        log.debug("owner guard alert failed | err=%r", e)
    return False
