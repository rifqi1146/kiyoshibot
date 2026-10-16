import logging
import time

log = logging.getLogger(__name__)

_MESSAGE_OWNERS: dict[tuple[int, int], tuple[int, float]] = {}
_LOCK_TTL = 6 * 3600.0
_MAX_LOCKS = 4000

DEFAULT_ALERT = "This menu is not yours."


def _prune(now: float) -> None:
    if len(_MESSAGE_OWNERS) <= _MAX_LOCKS:
        return
    for key in [k for k, v in list(_MESSAGE_OWNERS.items()) if now - v[1] > _LOCK_TTL]:
        _MESSAGE_OWNERS.pop(key, None)
    if len(_MESSAGE_OWNERS) > _MAX_LOCKS:
        ordered = sorted(_MESSAGE_OWNERS.items(), key=lambda kv: kv[1][1])
        for key, _ in ordered[: len(_MESSAGE_OWNERS) - _MAX_LOCKS]:
            _MESSAGE_OWNERS.pop(key, None)


def register_message_owner(chat_id, message_id, user_id) -> None:
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

    reply = getattr(msg, "reply_to_message", None)
    reply_from = getattr(reply, "from_user", None)
    if reply_from is not None:
        try:
            return clicker == int(reply_from.id)
        except (TypeError, ValueError):
            pass

    if getattr(chat, "type", None) == "private":
        return True

    return False


async def guard_callback_owner(
    q,
    expected_user_id=None,
    session_data=None,
    alert_text: str = DEFAULT_ALERT,
) -> bool:
    if is_owner(q, expected_user_id=expected_user_id, session_data=session_data):
        return True
    try:
        await q.answer(alert_text, show_alert=True)
    except Exception as e:
        log.debug("owner guard alert failed | err=%r", e)
    return False
