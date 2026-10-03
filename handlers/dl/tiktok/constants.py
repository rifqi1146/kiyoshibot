"""Konstanta scraper/downloader TikTok (tiktok.com / douyin.com)"""
import os
import re
import logging

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

WEB_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
}

UNIVERSAL_RE = re.compile(
    r'<script[^>]+\bid="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.S | re.I,
)
SIGI_RE = re.compile(
    r'<script[^>]+\bid="SIGI_STATE"[^>]*>(.*?)</script>',
    re.S | re.I,
)
NEXT_RE = re.compile(
    r'<script[^>]+\bid="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.S | re.I,
)
MODERN_SSR_RE = re.compile(
    r'<script[^>]+\bid="__MODERN_SSR_DATA__"[^>]*>(.*?)</script>',
    re.S | re.I,
)
SHORT_TIKTOK_RE = re.compile(r"https?://(?:vm|vt)\.tiktok\.com/", re.I)

try:
    from handlers.dl.constants import BASE_DIR
except Exception as e:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    log.warning("BASE_DIR fallback used for TikTok downloader | err=%r", e)

TIKTOK_COOKIES_PATH = os.path.abspath(
    os.path.join(BASE_DIR, "..", "..", "data", "cookies.txt")
)
TIKTOK_COOKIE_DOMAINS = (
    "tiktok.com",
    "tiktokv.com",
    "byteoversea.com",
    "ibyteimg.com",
    "muscdn.com",
    "tikwm.com",
)

# Engine & feature flags
USE_GOVD_FAST = os.getenv("TIKTOK_GOVD_FAST", "1").lower() not in ("0", "false", "off", "no")
USE_SCRAPLING = os.getenv("TIKTOK_USE_SCRAPLING", "1").lower() not in ("0", "false", "off", "no")
DEBUG_TIKTOK_LOG = os.getenv("TIKTOK_DEBUG", "0").lower() in ("1", "true", "on", "yes")
DEBUG_TIKTOK_DUMP = os.getenv("TIKTOK_DEBUG_DUMP", "0").lower() in ("1", "true", "on", "yes")
TIKTOK_DOWNLOAD_ENGINE = os.getenv("TIKTOK_DOWNLOAD_ENGINE", "aria2c").lower()
TIKTOK_PROGRESS = os.getenv("TIKTOK_PROGRESS", "1").lower() in ("1", "true", "on", "yes")
TIKTOK_PROGRESS_INTERVAL = float(os.getenv("TIKTOK_PROGRESS_INTERVAL", "1.5"))
TIKTOK_AIOHTTP_CHUNK_SIZE = int(os.getenv("TIKTOK_AIOHTTP_CHUNK_SIZE", str(256 * 1024)))
TIKTOK_ALBUM_CHUNK_SIZE = int(os.getenv("TIKTOK_ALBUM_CHUNK_SIZE", str(256 * 1024)))
ARIA2C_TIMEOUT = int(os.getenv("TIKTOK_ARIA2C_TIMEOUT", "600"))
AIOHTTP_DOWNLOAD_TIMEOUT = int(os.getenv("TIKTOK_AIOHTTP_TIMEOUT", "600"))

# Slideshow settings
TIKTOK_SLIDESHOW_IMAGE_DURATION = float(os.getenv("TIKTOK_SLIDESHOW_IMAGE_DURATION", "4.0"))
TIKTOK_SLIDESHOW_LOOP_IMAGES = os.getenv("TIKTOK_SLIDESHOW_LOOP_IMAGES", "1").lower() in ("1", "true", "on", "yes")
TIKTOK_SLIDESHOW_WIDTH = int(os.getenv("TIKTOK_SLIDESHOW_WIDTH", "720"))
TIKTOK_SLIDESHOW_HEIGHT = int(os.getenv("TIKTOK_SLIDESHOW_HEIGHT", "1280"))
TIKTOK_SLIDESHOW_FPS = int(os.getenv("TIKTOK_SLIDESHOW_FPS", "30"))
TIKTOK_SLIDESHOW_TRANSITION = os.getenv("TIKTOK_SLIDESHOW_TRANSITION", "slideleft").strip() or "slideleft"
TIKTOK_SLIDESHOW_TRANSITION_DURATION = float(os.getenv("TIKTOK_SLIDESHOW_TRANSITION_DURATION", "0.6"))
TIKTOK_SLIDESHOW_MIN_IMAGE_DURATION = float(os.getenv("TIKTOK_SLIDESHOW_MIN_IMAGE_DURATION", "0.6"))
TIKTOK_SLIDESHOW_SYNC_AUDIO = os.getenv("TIKTOK_SLIDESHOW_SYNC_AUDIO", "1").lower() in ("1", "true", "on", "yes")
