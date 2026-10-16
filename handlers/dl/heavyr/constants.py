import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("HEAVYR_HTTP_TIMEOUT", "30"))
HTTP_ATTEMPTS = int(os.getenv("HEAVYR_HTTP_ATTEMPTS", "3"))

HEAVYR_HOSTS = ("heavy-r.com", "www.heavy-r.com", "embed.heavy-r.com")

HEAVYR_EMBED_ORIGIN = "https://embed.heavy-r.com"

HEAVYR_REFERER = "https://www.heavy-r.com/"

HEAVYR_ARIA2_CONNS = int(os.getenv("HEAVYR_ARIA2_CONNS", "16"))

HEAVYR_PROGRESS_INTERVAL = float(os.getenv("HEAVYR_PROGRESS_INTERVAL", "2.5"))
