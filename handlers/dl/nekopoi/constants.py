import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 45

FORCE_IPV4 = os.getenv("NEKOPOI_FORCE_IPV4", "1").strip().lower() in ("1", "true", "on", "yes")

DEBUG_NEKOPOI = os.getenv("NEKOPOI_DEBUG", "0").strip().lower() in ("1", "true", "on", "yes")

NEKOPOI_HOSTS = ("nekopoi.care", "nekopoi.best", "nekopoi.care")

EMBED_HOST_STREAMPOI = ("streampoi.com", "streamruby.com", "streamruby.net")
EMBED_HOST_DOOD = ("playmogo.com", "doodstream.com", "dood.", "ds2play.com")

SEG_CONCURRENCY = int(os.getenv("NEKOPOI_SEG_CONCURRENCY", "12"))
SEG_RETRIES = int(os.getenv("NEKOPOI_SEG_RETRIES", "3"))
SEG_TIMEOUT = float(os.getenv("NEKOPOI_SEG_TIMEOUT", "60"))
PROGRESS_POLL = float(os.getenv("NEKOPOI_PROGRESS_POLL", "0.7"))
PROGRESS_MIN_INTERVAL = float(os.getenv("NEKOPOI_PROGRESS_MIN_INTERVAL", "5"))
PROGRESS_MAX_INTERVAL = float(os.getenv("NEKOPOI_PROGRESS_MAX_INTERVAL", "30"))
PROGRESS_TARGET_EDITS = float(os.getenv("NEKOPOI_PROGRESS_TARGET_EDITS", "15"))
NEKOPOI_PROGRESS_INTERVAL = PROGRESS_MIN_INTERVAL
FFMPEG_TIMEOUT = float(os.getenv("NEKOPOI_FFMPEG_TIMEOUT", "1800"))

VARIANT_LABEL = {
    "l": 360,
    "n": 480,
    "h": 720,
    "o": 720,
    "v": 1080,
}
