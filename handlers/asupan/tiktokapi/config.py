import os

PORT = int(os.environ.get("PORT", "8000"))

MAX_RETRIES = 3
RETRY_BACKOFF = 2

TIKTOK_PROXY = os.environ.get("TIKTOK_PROXY") or None
TIKTOK_USE_PROXY = TIKTOK_PROXY is not None

TIKTOK_DEFAULT_COUNTRY = "us"
TIKTOK_EU_COUNTRY = "de"

TIKTOK_POSTS_CALLS_PER_EXIT = 12
TIKTOK_SESSION_MAX_AGE = 1800
TIKTOK_IDLE_SESSIONS_PER_COUNTRY = 6


def tiktok_proxy(country):
    if not TIKTOK_PROXY:
        return None
    return TIKTOK_PROXY.replace("{country}", (country or TIKTOK_DEFAULT_COUNTRY).lower())
