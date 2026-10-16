import time
from .constants import CACHE_TTL, DETAIL_TTL

FIRMWARE_CACHE: dict[str, dict] = {}

MIRRORS_CACHE: dict[str, dict] = {}

DEVICES_CACHE: dict = {"ts": 0.0, "map": {}, "lower_map": {}}

MAX_CACHE_ENTRIES = 120
MAX_MIRRORS_ENTRIES = 300


def purge_expired_cache() -> None:
    now = time.time()
    for key in [k for k, v in list(FIRMWARE_CACHE.items()) if now - v.get("ts", 0) > CACHE_TTL]:
        FIRMWARE_CACHE.pop(key, None)
    if len(FIRMWARE_CACHE) > MAX_CACHE_ENTRIES:
        for key in list(FIRMWARE_CACHE)[: len(FIRMWARE_CACHE) - MAX_CACHE_ENTRIES]:
            FIRMWARE_CACHE.pop(key, None)

    for key in [k for k, v in list(MIRRORS_CACHE.items()) if now - v.get("ts", 0) > DETAIL_TTL]:
        MIRRORS_CACHE.pop(key, None)
    if len(MIRRORS_CACHE) > MAX_MIRRORS_ENTRIES:
        for key in list(MIRRORS_CACHE)[: len(MIRRORS_CACHE) - MAX_MIRRORS_ENTRIES]:
            MIRRORS_CACHE.pop(key, None)
