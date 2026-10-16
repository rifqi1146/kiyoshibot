import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 30
COSXPLAY_HOSTS = ("cosxplay.com", "www.cosxplay.com")
COSXPLAY_PROGRESS_INTERVAL = float(os.getenv("COSXPLAY_PROGRESS_INTERVAL", "2.5"))
