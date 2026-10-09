import aiohttp
import logging

logger = logging.getLogger(__name__)

_HTTP_SESSION: aiohttp.ClientSession | None = None


async def get_http_session():
    global _HTTP_SESSION
    if _HTTP_SESSION is None or _HTTP_SESSION.closed:
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=20,
            enable_cleanup_closed=True,
            force_close=False,
        )
        # total=None penting: timeout total 60s memotong transfer body besar
        # yang legit (download media). Batas per-socket tetap ada lewat
        # sock_connect/sock_read, jadi koneksi mati tetap terdeteksi.
        _HTTP_SESSION = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120),
        )
    return _HTTP_SESSION


async def close_http_session():
    global _HTTP_SESSION
    if _HTTP_SESSION and not _HTTP_SESSION.closed:
        await _HTTP_SESSION.close()
        _HTTP_SESSION = None
        logger.info("Kyahhh Modar 🥲")
