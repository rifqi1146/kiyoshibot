"""State in-memory untuk manga reader: cache data reader + lock kepemilikan tombol.

- `_MANGA_CACHE` : cache per-user ber-TTL + ber-cap (pengganti `context.user_data`).
  Key = `(user_id, suffix)`.
- `_MANGA_MESSAGE_LOCKS` / `_MANGA_LOCK_TIMES` : pemetaan pesan -> user yang
  memiliki tombolnya (fail-closed: tombol milik orang lain ditolak).
"""
import time

from .constants import _MANGA_CACHE_TTL, _MANGA_CACHE_MAX

# (user_id, suffix) -> (timestamp, value)
_MANGA_CACHE: dict[tuple[int, str], tuple[float, object]] = {}

_MANGA_MESSAGE_LOCKS = {}
_MANGA_LOCK_TIMES = {}


# ---------------------------------------------------------------------------
# Cache per-user
# ---------------------------------------------------------------------------
def _udata_key(context, suffix: str):
    uid = getattr(getattr(context, "effective_user", None), "id", None)
    if uid is None:
        uid = getattr(context, "_user_id", None)
    if uid is None:
        uid = 0
    return (int(uid), str(suffix))


def _udata_prune(now: float | None = None) -> None:
    if len(_MANGA_CACHE) <= _MANGA_CACHE_MAX:
        return
    now = time.time() if now is None else now
    for k in [k for k, v in _MANGA_CACHE.items() if now - v[0] > _MANGA_CACHE_TTL]:
        _MANGA_CACHE.pop(k, None)
    if len(_MANGA_CACHE) > _MANGA_CACHE_MAX:
        for k in list(_MANGA_CACHE)[: len(_MANGA_CACHE) - _MANGA_CACHE_MAX]:
            _MANGA_CACHE.pop(k, None)


def _udata_get(context, suffix: str, default=None):
    item = _MANGA_CACHE.get(_udata_key(context, suffix))
    if not item:
        return default
    if time.time() - item[0] > _MANGA_CACHE_TTL:
        _MANGA_CACHE.pop(_udata_key(context, suffix), None)
        return default
    return item[1]


def _udata_set(context, suffix: str, value) -> None:
    _MANGA_CACHE[_udata_key(context, suffix)] = (time.time(), value)
    _udata_prune()


def _udata_has(context, suffix: str) -> bool:
    return _udata_get(context, suffix) is not None


# ---------------------------------------------------------------------------
# Lock kepemilikan tombol per pesan
# ---------------------------------------------------------------------------
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
