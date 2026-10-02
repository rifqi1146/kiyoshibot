"""Konstanta scraper Bunkr (bunkr.cr dll.)

FLOW KONSTANTA
--------------
1. UA & timeout untuk request HTTP (curl_cffi impersonate Chrome).
2. Host Bunkr yang dikenal (rotasi TLD).
3. Endpoint signing CDN.
4. Interval progress.
"""
import os
import re

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 45

# Domain Bunkr sering rotasi TLD. Daftar ini mencakup host yang umum
# dipakai; URL matching di `main.py` juga mendukung wildcard `bunkr.*`.
BUNKR_KNOWN_HOSTS = (
    "bunkr.cr", "bunkr.ph", "bunkr.si", "bunkr.sk", "bunkr.black",
    "bunkr.media", "bunkr.ws", "bunkr.ac", "bunkr.fi", "bunkr.is",
    "bunkr.ru", "bunkrr.su", "bunkr.site", "bunkr.pk",
)

# Endpoint tanda tangan token CDN untuk akses stream langsung
SIGN_URL = "https://glb-apisign.cdn.cr/sign"

# Interval edit status progress (detik)
BUNKR_PROGRESS_INTERVAL = float(os.getenv("BUNKR_PROGRESS_INTERVAL", "3"))

_VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ts")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
