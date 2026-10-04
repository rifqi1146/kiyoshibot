"""Konstanta scraper AsianGirlPorn (asiangirl.porn)

FLOW KONSTANTA
--------------
1. UA + timeout untuk request curl_cffi (impersonate Chrome).
2. Domain asiangirl.porn untuk deteksi URL dan routing download.
3. Host CDN stream (cdnlab.live, ppp.porn, dst) untuk HLS.
4. Concurrency segmen paralel HLS (12 worker) + retry.
5. Interval progress.
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("ASIANGIRL_HTTP_TIMEOUT", "30"))
SEG_TIMEOUT = int(os.getenv("ASIANGIRL_SEG_TIMEOUT", "60"))
SEG_CONCURRENCY = int(os.getenv("ASIANGIRL_SEG_CONCURRENCY", "12"))
SEG_RETRIES = int(os.getenv("ASIANGIRL_SEG_RETRIES", "3"))
FFMPEG_TIMEOUT = int(os.getenv("ASIANGIRL_FFMPEG_TIMEOUT", "300"))

ASIANGIRL_HOSTS = ("asiangirl.porn", "www.asiangirl.porn")

# TLD stream CDN yang berotasi (cdnlab.live, ppp.porn, dll)
CDN_HOST_MARKERS = ("cdnlab.live", "ppp.porn")

PROGRESS_LOG_INTERVAL = float(os.getenv("ASIANGIRL_PROGRESS_LOG_INTERVAL", "2.5"))
PROGRESS_MAX_INTERVAL = float(os.getenv("ASIANGIRL_PROGRESS_MAX_INTERVAL", "30.0"))
