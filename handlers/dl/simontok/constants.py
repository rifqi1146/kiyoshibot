"""Konstanta scraper Simontok (simontok.study)

FLOW KONSTANTA
--------------
1. UA + timeout untuk request curl_cffi (impersonate Chrome).
2. Domain simontok.study untuk deteksi URL dan routing download.
3. Host embed putarin.*/puterin.* (ejaan + mirror TLD berrotasi) untuk resolusi HLS.
4. Concurrency + retry untuk unduhan segmen HLS.
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("SIMONTOK_HTTP_TIMEOUT", "30"))
SEG_TIMEOUT = int(os.getenv("SIMONTOK_SEG_TIMEOUT", "60"))
FFMPEG_TIMEOUT = int(os.getenv("SIMONTOK_FFMPEG_TIMEOUT", "600"))

SIMONTOK_HOSTS = ("simontok.study", "www.simontok.study")

# Host embed player. TLD putarin berotasi (biz/xyz/...) DAN ejaannya bervariasi
# (`putarin.` / `puterin.`) tergantung post, jadi request selalu memakai origin
# yang benar-benar ada di iframe post, bukan host yang di-pin. Kedua varian pakai
# alur identik (window.__PX + /api/hls).
EMBED_HOST_MARKERS = ("putarin.", "puterin.")

SEG_CONCURRENCY = int(os.getenv("SIMONTOK_SEG_CONCURRENCY", "12"))
SEG_RETRIES = int(os.getenv("SIMONTOK_SEG_RETRIES", "3"))

# Interval progress adaptif (detik) — samakan dengan downloader lain.
PROGRESS_INTERVAL = float(os.getenv("SIMONTOK_PROGRESS_INTERVAL", "2.5"))
