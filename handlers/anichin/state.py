"""Cache in-memory untuk sesi browse `/anichin`.

- `ANICHIN_CACHE` : sesi hasil listing/detail (key: session_id 8 hex).
- `GENRES_CACHE`  : daftar genre dari `/genres/` (TTL 1 jam).
- `AZ_CACHE`      : daftar huruf A-Z dari `/az-lists/` (TTL 1 jam).
- `SCHEDULE_CACHE`: jadwal rilis dari `/schedule/` (TTL 1 jam).

Semua cache dibatasi TTL + jumlah entri supaya RAM tidak membengkak.
"""
import time

from .constants import CACHE_TTL, INDEX_TTL, MAX_CACHE_ENTRIES

# session_id -> { title, query, results, page, user_id, chat_id, ts, details, origin }
ANICHIN_CACHE: dict[str, dict] = {}

GENRES_CACHE: dict = {"ts": 0.0, "list": []}
AZ_CACHE: dict = {"ts": 0.0, "letters": []}
SCHEDULE_CACHE: dict = {"ts": 0.0, "days": []}


def purge_expired_cache() -> None:
    """Buang sesi basi + paksa batas jumlah entri."""
    now = time.time()
    for key in [k for k, v in list(ANICHIN_CACHE.items()) if now - v.get("ts", 0) > CACHE_TTL]:
        ANICHIN_CACHE.pop(key, None)
    if len(ANICHIN_CACHE) > MAX_CACHE_ENTRIES:
        for key in list(ANICHIN_CACHE)[: len(ANICHIN_CACHE) - MAX_CACHE_ENTRIES]:
            ANICHIN_CACHE.pop(key, None)


def purge_index_cache() -> None:
    now = time.time()
    for cache in (GENRES_CACHE, AZ_CACHE, SCHEDULE_CACHE):
        if now - cache.get("ts", 0) > INDEX_TTL:
            cache["ts"] = 0.0
            for k in ("list", "letters", "days"):
                if k in cache:
                    cache[k] = []
