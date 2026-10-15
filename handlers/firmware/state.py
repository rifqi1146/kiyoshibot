"""In-memory cache for Xiaomi Firmware Explorer sessions and metadata."""
import time
from .constants import CACHE_TTL, DETAIL_TTL

# session_id -> { title, codename, device_name, results, filtered_results, region_filter, type_filter, page, user_id, chat_id, ts }
FIRMWARE_CACHE: dict[str, dict] = {}

# detail_path -> { "ts": float, "blocks": { "recovery": {...}, "fastboot": {...} } }
MIRRORS_CACHE: dict[str, dict] = {}

# Cache daftar 300+ device Xiaomi: { "ts": float, "map": {codename: full_name} }
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
