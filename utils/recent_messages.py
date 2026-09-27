"""In-memory rolling window of recently seen Telegram messages per chat.

The Telegram Bot API has no "get chat history" endpoint, so to build
multi-message quotes (/q 2, /q 3, ...) we keep a small per-chat buffer of
the messages the bot has already received and slice the ones right before
(or after) the message the user replied to.
"""
import os
import logging
from collections import OrderedDict, deque

log = logging.getLogger(__name__)

_MAX_CHATS = int(os.getenv("RECENT_MSG_MAX_CHATS", "500"))
_MAX_PER_CHAT = int(os.getenv("RECENT_MSG_PER_CHAT", "50"))

_store: "OrderedDict[int, deque]" = OrderedDict()


def remember(chat_id, message) -> None:
    """Store a message in the rolling window of its chat.

    Message IDs are monotonically increasing within a chat, so we insert in
    ID order instead of append order. That keeps the window correct even when
    a message is first seen late (e.g. pulled in as a `reply_to_message` of a
    newer command) instead of when it was originally sent.
    """
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return

    mid = getattr(message, "message_id", None)
    if mid is None:
        return

    dq = _store.get(key)
    if dq is None:
        dq = deque(maxlen=_MAX_PER_CHAT)
        _store[key] = dq
    else:
        _store.move_to_end(key)

    # Replace in place when already stored (edited messages arrive again).
    # Keep the variant that carries a reply chain — late arrivals from
    # `msg.reply_to_message` lack nested `reply_to_message` (Bot API strips
    # it), so never let them overwrite a buffered copy that has it.
    for i in range(len(dq)):
        existing = getattr(dq[i], "message_id", None)
        if existing == mid:
            old_chain = getattr(dq[i], "reply_to_message", None)
            new_chain = getattr(message, "reply_to_message", None)
            if old_chain is not None and new_chain is None:
                return
            dq[i] = message
            return
        if existing is not None and existing > mid:
            try:
                dq.insert(i, message)
            except IndexError:
                # Full deque: evict the oldest entry first, then insert.
                if i == 0:
                    dq.appendleft(message)
                else:
                    dq.popleft()
                    dq.insert(i - 1, message)
            return
    dq.append(message)

    while len(_store) > _MAX_CHATS:
        _store.popitem(last=False)


def get_window(chat_id, message_id, count: int) -> list:
    """Return up to `count` messages ending at `message_id` (oldest first)."""
    if count < 1:
        return []
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return []

    dq = _store.get(key)
    if not dq:
        return []

    items = list(dq)
    idx = -1
    for i in range(len(items) - 1, -1, -1):
        if getattr(items[i], "message_id", None) == message_id:
            idx = i
            break
    if idx < 0:
        return []

    start = max(0, idx - count + 1)
    return items[start:idx + 1]


def get_slice_after(chat_id, message_id, count: int) -> list:
    """Return up to `count` messages starting at `message_id` (oldest first)."""
    if count < 1:
        return []
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return []

    dq = _store.get(key)
    if not dq:
        return []

    items = list(dq)
    idx = -1
    for i in range(len(items)):
        if getattr(items[i], "message_id", None) == message_id:
            idx = i
            break
    if idx < 0:
        return []

    return items[idx:idx + count]


def forget(chat_id) -> None:
    try:
        _store.pop(int(chat_id), None)
    except (TypeError, ValueError):
        pass
