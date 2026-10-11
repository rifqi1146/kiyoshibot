"""Cache in-memory untuk sesi browse `/nekopoi`.

- `NEKOPOI_CACHE`     : sesi hasil listing/detail (key: session_id 8 hex).
- `GENRES_CACHE`      : daftar 78 genre dari `/genre-list/` (TTL 1 jam).
- `AZ_CACHE`          : daftar A-Z (`hentai-list`/`jav-list`, TTL 1 jam).
- `SCHEDULE_CACHE`    : jadwal rilis dari `/jadwal-new-hentai/` (TTL 1 jam).

Semua cache dibatasi TTL + jumlah entri supaya RAM tidak membengkak.
"""
import time

from .constants import AZ_TTL, CACHE_TTL, MAX_CACHE_ENTRIES

# session_id -> { title, query, results, page, user_id, chat_id, ts, type, details }
NEKOPOI_CACHE: dict[str, dict] = {}

GENRES_CACHE: dict = {"ts": 0.0, "list": []}
# { "hentai": {"ts": float, "letters": {letter: [items]}, "flat": [items]}, "jav": {...} }
AZ_CACHE: dict = {}
SCHEDULE_CACHE: dict = {"ts": 0.0, "months": []}


def purge_expired_cache() -> None:
    """Buang sesi basi + paksa batas jumlah entri."""
    now = time.time()
    for key in [k for k, v in list(NEKOPOI_CACHE.items()) if now - v.get("ts", 0) > CACHE_TTL]:
        NEKOPOI_CACHE.pop(key, None)
    if len(NEKOPOI_CACHE) > MAX_CACHE_ENTRIES:
        for key in list(NEKOPOI_CACHE)[: len(NEKOPOI_CACHE) - MAX_CACHE_ENTRIES]:
            NEKOPOI_CACHE.pop(key, None)


def purge_az_cache() -> None:
    now = time.time()
    for key in [k for k, v in list(AZ_CACHE.items()) if now - v.get("ts", 0) > AZ_TTL]:
        AZ_CACHE.pop(key, None)
