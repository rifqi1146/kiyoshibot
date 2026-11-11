import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 30

SPANKBANG_HOSTS = ("spankbang.com", "spankbang.party")

SPANKBANG_REFERER = "https://spankbang.com/"

SPANKBANG_QUALITIES = (
    ("4k", 2160),
    ("1080p", 1080),
    ("720p", 720),
    ("480p", 480),
    ("320p", 320),
    ("240p", 240),
)

SPANKBANG_PROGRESS_INTERVAL = float(
    os.getenv("SPANKBANG_PROGRESS_INTERVAL", "2.5")
)

SPANKBANG_BROWSER_TIMEOUT_MS = int(
    os.getenv("SPANKBANG_BROWSER_TIMEOUT_MS", "60000")
)

SPANKBANG_CF_TTL = float(os.getenv("SPANKBANG_CF_TTL", "1800"))
