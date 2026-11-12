import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 30

THISVID_HOSTS = ("thisvid.com", "www.thisvid.com")

THISVID_REFERER = "https://thisvid.com/"

THISVID_ARIA2_CONNS = int(os.getenv("THISVID_ARIA2_CONNS", "16"))

THISVID_PROGRESS_INTERVAL = float(os.getenv("THISVID_PROGRESS_INTERVAL", "2.5"))
