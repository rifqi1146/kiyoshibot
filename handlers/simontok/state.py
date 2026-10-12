"""Cache in-memory untuk sesi browse explorer Simontok (`/simontok`).

- `SESSION_CACHE`    : sesi hasil listing/detail (key: session_id 8 hex).
- `CATEGORIES_CACHE` : daftar kategori (TTL 1 jam).

Semua cache dibatasi TTL + jumlah entri supaya RAM tidak membengkak.
"""
import time

from .constants import CACHE_TTL, INDEX_TTL, MAX_CACHE_ENTRIES

# session_id -> { title, query, results, page, user_id, chat_id, ts, details, origin }
SESSION_CACHE: dict[str, dict] = {}

# {"ts": float, "list": [...]}
CATEGORIES_CACHE: dict = {"ts": 0.0, "list": []}


def get_categories_cache() -> dict:
    return CATEGORIES_CACHE


def purge_expired_cache() -> None:
    """Buang sesi basi + paksa batas jumlah entri."""
    now = time.time()
    for key in [k for k, v in list(SESSION_CACHE.items()) if now - v.get("ts", 0) > CACHE_TTL]:
        SESSION_CACHE.pop(key, None)
    if len(SESSION_CACHE) > MAX_CACHE_ENTRIES:
        for key in list(SESSION_CACHE)[: len(SESSION_CACHE) - MAX_CACHE_ENTRIES]:
            SESSION_CACHE.pop(key, None)


def purge_index_cache() -> None:
    slot = CATEGORIES_CACHE
    if slot.get("list") and time.time() - slot.get("ts", 0) > INDEX_TTL:
        slot["list"] = []
        slot["ts"] = 0.0
