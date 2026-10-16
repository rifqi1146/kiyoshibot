import time

from .constants import CACHE_TTL, INDEX_TTL, MAX_CACHE_ENTRIES

SESSION_CACHE: dict[str, dict] = {}

CATEGORIES_CACHE: dict = {"ts": 0.0, "list": []}


def get_categories_cache() -> dict:
    return CATEGORIES_CACHE


def purge_expired_cache() -> None:
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
