import time

from .constants import CACHE_TTL, INDEX_TTL, MAX_CACHE_ENTRIES

SESSION_CACHE: dict[str, dict] = {}

CATEGORIES_CACHE: dict = {}
TAGS_CACHE: dict = {}


def _cache_slot(cache: dict, site_key: str) -> dict:
    slot = cache.get(site_key)
    if not slot:
        slot = {"ts": 0.0, "list": []}
        cache[site_key] = slot
    return slot


def get_categories_cache(site_key: str) -> dict:
    return _cache_slot(CATEGORIES_CACHE, site_key)


def get_tags_cache(site_key: str) -> dict:
    return _cache_slot(TAGS_CACHE, site_key)


def purge_expired_cache() -> None:
    now = time.time()
    for key in [k for k, v in list(SESSION_CACHE.items()) if now - v.get("ts", 0) > CACHE_TTL]:
        SESSION_CACHE.pop(key, None)
    if len(SESSION_CACHE) > MAX_CACHE_ENTRIES:
        for key in list(SESSION_CACHE)[: len(SESSION_CACHE) - MAX_CACHE_ENTRIES]:
            SESSION_CACHE.pop(key, None)


def purge_index_cache() -> None:
    now = time.time()
    for cache in (CATEGORIES_CACHE, TAGS_CACHE):
        for site_key, slot in list(cache.items()):
            if slot.get("list") and now - slot.get("ts", 0) > INDEX_TTL:
                slot["list"] = []
                slot["ts"] = 0.0
