import os
import logging
from collections import OrderedDict, deque

log = logging.getLogger(__name__)

_MAX_CHATS = int(os.getenv("RECENT_MSG_MAX_CHATS", "500"))
_MAX_PER_CHAT = int(os.getenv("RECENT_MSG_PER_CHAT", "50"))

_store: "OrderedDict[int, deque]" = OrderedDict()


def remember(chat_id, message) -> None:
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
