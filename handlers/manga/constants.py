import os
import asyncio
import logging

log = logging.getLogger(__name__)

MANGADEX_API = "https://api.mangadex.org"
UPLOADS_URL = "https://uploads.mangadex.org"
MAID_URL = "https://www.maid.my.id"
NH_API_URL = "https://nhentai.net/api/v2"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"

_PDF_SEM = asyncio.Semaphore(int(os.getenv("MANGA_PDF_CONCURRENCY", "2")))

_MANGA_CACHE_TTL = float(os.getenv("MANGA_CACHE_TTL", "7200"))
_MANGA_CACHE_MAX = int(os.getenv("MANGA_CACHE_MAX", "400"))

NH_API_KEY = os.getenv("NH_API_KEY")
NH_HEADERS = {"User-Agent": "PrivateMangaBot/3.0 (Telegram Bot)"}
if NH_API_KEY:
    NH_HEADERS["Authorization"] = f"Key {NH_API_KEY}"
    log.info("Loaded NH API Key.")
else:
    log.info("NH API Key not found, Running In Public Mode.")
