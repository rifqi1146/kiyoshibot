"""In-memory cache for MovieBox Explorer sessions and metadata."""
import time
from .constants import CACHE_TTL

# session_id -> { title, query, results, page, user_id, chat_id, ts, details }
MOVIEBOX_CACHE: dict[str, dict] = {}
TRENDING_CACHE: dict = {"ts": 0, "list": []}
HOME_CACHE: dict = {"ts": 0, "by_title": {}}
MAX_CACHE_ENTRIES = 120


def purge_expired_cache() -> None:
    now = time.time()
    for key in [k for k, v in list(MOVIEBOX_CACHE.items()) if now - v.get("ts", 0) > CACHE_TTL]:
        MOVIEBOX_CACHE.pop(key, None)
    if len(MOVIEBOX_CACHE) > MAX_CACHE_ENTRIES:
        for key in list(MOVIEBOX_CACHE)[: len(MOVIEBOX_CACHE) - MAX_CACHE_ENTRIES]:
            MOVIEBOX_CACHE.pop(key, None)
