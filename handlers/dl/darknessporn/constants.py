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
