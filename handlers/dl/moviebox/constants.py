import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("MOVIEBOX_HTTP_TIMEOUT", "30"))

MOVIEBOX_HOSTS = (
    "officialmoviebox.com",
    "www.officialmoviebox.com",
    "moviebox.ph",
    "www.moviebox.ph",
    "moviebox.co",
    "www.moviebox.co",
)

BASE_URL = "https://officialmoviebox.com"
API_BASE = "https://h5-api.aoneroom.com/wefeed-h5api-bff"

MOVIEBOX_ARIA2_CONNS = int(os.getenv("MOVIEBOX_ARIA2_CONNS", "16"))
MOVIEBOX_PROGRESS_INTERVAL = float(os.getenv("MOVIEBOX_PROGRESS_INTERVAL", "5.0"))
