"""Konstanta scraper CosXplay (cosxplay.com) — full mandiri, TANPA yt-dlp.

FLOW KONSTANTA
--------------
1. UA dan timeout untuk request curl_cffi (impersonate Chrome).
2. Domain cosxplay.com untuk deteksi URL dan routing download.
3. Interval progress adaptif untuk edit pesan Telegram (menghindari RetryAfter).
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 30
COSXPLAY_HOSTS = ("cosxplay.com", "www.cosxplay.com")
COSXPLAY_PROGRESS_INTERVAL = float(os.getenv("COSXPLAY_PROGRESS_INTERVAL", "2.5"))
