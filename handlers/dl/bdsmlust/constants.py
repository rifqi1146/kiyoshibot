import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("BDSMLUST_HTTP_TIMEOUT", "30"))
HTTP_ATTEMPTS = int(os.getenv("BDSMLUST_HTTP_ATTEMPTS", "3"))

BDSMLUST_HOSTS = ("bdsmlust.com", "www.bdsmlust.com")

STATIC_PATH_PREFIXES = (
    "search", "categories", "bdsm-pornstars", "bdsm-studios",
    "kink-channels", "bdsm-tags", "bdsm-guides", "bdsm-glossary",
    "bdsm-test", "what-is-bdsm", "category", "tag", "page",
    "partners", "contact", "2257-2",
)

EMBED_HOST_BDSMSTREAK = "bdsmstreak.com"
EMBED_HOST_VIDBX = "vid-bx.com"
EMBED_HOST_BDSMX = "bdsmx.tube"

BDSMX_ORIGIN = "https://bdsmx.tube"

BDSMLUST_ARIA2_CONNS = int(os.getenv("BDSMLUST_ARIA2_CONNS", "16"))

BDSMLUST_PROGRESS_INTERVAL = float(os.getenv("BDSMLUST_PROGRESS_INTERVAL", "2.5"))
