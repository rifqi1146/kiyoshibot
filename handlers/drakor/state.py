"""Cache in-memory untuk sesi pencarian, browsing, dan daftar kategori."""
import time
from .constants import CACHE_TTL, MAX_CACHE_ENTRIES

# session_id -> { title, query, results, page, user_id, chat_id, ts, type, slug, details }
DRAKOR_CACHE: dict[str, dict] = {}
CATEGORIES_CACHE: dict = {"ts": 0, "list": []}


def purge_expired_cache() -> None:
    now = time.time()
    for key in [k for k, v in list(DRAKOR_CACHE.items()) if now - v.get("ts", 0) > CACHE_TTL]:
        DRAKOR_CACHE.pop(key, None)
    if len(DRAKOR_CACHE) > MAX_CACHE_ENTRIES:
        for key in list(DRAKOR_CACHE)[: len(DRAKOR_CACHE) - MAX_CACHE_ENTRIES]:
            DRAKOR_CACHE.pop(key, None)
