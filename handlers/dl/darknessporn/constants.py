"""Konstanta scraper DarknessPorn (darknessporn.com)

FLOW KONSTANTA
--------------
1. UA & timeout untuk request curl_cffi (impersonate Chrome).
2. Host DarknessPorn untuk deteksi URL dan routing download.
3. Interval progress edit pesan Telegram.
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 30

DARKNESSPORN_HOSTS = ("darknessporn.com", "www.darknessporn.com")

DARKNESSPORN_PROGRESS_INTERVAL = float(
    os.getenv("DARKNESSPORN_PROGRESS_INTERVAL", "2.5")
)
