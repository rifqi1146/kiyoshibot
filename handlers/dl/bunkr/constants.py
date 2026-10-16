import os
import re

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 45

BUNKR_KNOWN_HOSTS = (
    "bunkr.cr", "bunkr.ph", "bunkr.si", "bunkr.sk", "bunkr.black",
    "bunkr.media", "bunkr.ws", "bunkr.ac", "bunkr.fi", "bunkr.is",
    "bunkr.ru", "bunkrr.su", "bunkr.site", "bunkr.pk",
)

SIGN_URL = "https://glb-apisign.cdn.cr/sign"

BUNKR_PROGRESS_INTERVAL = float(os.getenv("BUNKR_PROGRESS_INTERVAL", "3"))

BUNKR_ALBUM_CONCURRENCY = int(os.getenv("BUNKR_ALBUM_CONCURRENCY", "6"))

_VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ts")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
