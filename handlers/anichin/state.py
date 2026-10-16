import time

from .constants import CACHE_TTL, INDEX_TTL, MAX_CACHE_ENTRIES

ANICHIN_CACHE: dict[str, dict] = {}

GENRES_CACHE: dict = {"ts": 0.0, "list": []}
AZ_CACHE: dict = {"ts": 0.0, "letters": []}
SCHEDULE_CACHE: dict = {"ts": 0.0, "days": []}


def purge_expired_cache() -> None:
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
