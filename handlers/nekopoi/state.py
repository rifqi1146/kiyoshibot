import time

from .constants import AZ_TTL, CACHE_TTL, MAX_CACHE_ENTRIES

NEKOPOI_CACHE: dict[str, dict] = {}

GENRES_CACHE: dict = {"ts": 0.0, "list": []}
AZ_CACHE: dict = {}
SCHEDULE_CACHE: dict = {"ts": 0.0, "months": []}


def purge_expired_cache() -> None:
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
