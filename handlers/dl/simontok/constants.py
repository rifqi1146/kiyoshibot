import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("SIMONTOK_HTTP_TIMEOUT", "30"))
SEG_TIMEOUT = int(os.getenv("SIMONTOK_SEG_TIMEOUT", "60"))
FFMPEG_TIMEOUT = int(os.getenv("SIMONTOK_FFMPEG_TIMEOUT", "600"))

SIMONTOK_HOSTS = ("simontok.study", "www.simontok.study")

EMBED_HOST_MARKERS = ("putarin.", "puterin.")

SEG_CONCURRENCY = int(os.getenv("SIMONTOK_SEG_CONCURRENCY", "12"))
SEG_RETRIES = int(os.getenv("SIMONTOK_SEG_RETRIES", "3"))

PROGRESS_INTERVAL = float(os.getenv("SIMONTOK_PROGRESS_INTERVAL", "2.5"))
