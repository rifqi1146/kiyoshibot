import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("DRAKORID_HTTP_TIMEOUT", "30"))

DRAKORID_HOSTS = ("drakorid.co", "www.drakorid.co")

BASE_URL = "https://drakorid.co"
CDN_RESOLVER = "http://admin.drakor.la/go/files"

DRAKORID_ARIA2_CONNS = int(os.getenv("DRAKORID_ARIA2_CONNS", "16"))

DRAKORID_PROGRESS_INTERVAL = float(os.getenv("DRAKORID_PROGRESS_INTERVAL", "2.5"))

CDN_QUALITY_DIRS = ("files", "720p", "480p", "360p")
