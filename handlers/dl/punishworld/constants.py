"""Konstanta untuk Downloader PunishWorld (punishworld.com).

FLOW KONSTANTA:
---------------
1. PUNISHWORLD_DOMAINS: Domain yang didukung.
2. UA: User agent Chrome yang valid untuk bypass filter Cloudflare/anti-bot.
3. TIMEOUT: Batas waktu koneksi/download.
"""

PUNISHWORLD_DOMAINS = (
    "punishworld.com",
    "www.punishworld.com",
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 30
PROGRESS_MIN_INTERVAL = 3.0
