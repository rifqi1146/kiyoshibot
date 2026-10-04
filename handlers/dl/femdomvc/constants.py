"""Konstanta scraper FemdomVC (femdomvc.com)

FLOW KONSTANTA
--------------
1. UA & timeout untuk request curl_cffi (impersonate Chrome).
2. Host FemdomVC untuk deteksi URL dan routing download.
3. Interval progress edit pesan Telegram.
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 30

FEMDOMVC_HOSTS = ("femdomvc.com", "www.femdomvc.com")

FEMDOMVC_PROGRESS_INTERVAL = float(
    os.getenv("FEMDOMVC_PROGRESS_INTERVAL", "2.5")
)

FEMDOMVC_ARIA2_CONNS = int(os.getenv("FEMDOMVC_ARIA2_CONNS", "16"))
