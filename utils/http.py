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
        _HTTP_SESSION = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=60),
        )
    return _HTTP_SESSION


async def close_http_session():
    global _HTTP_SESSION
    if _HTTP_SESSION and not _HTTP_SESSION.closed:
        await _HTTP_SESSION.close()
        _HTTP_SESSION = None
        logger.info("Kyahhh Modar 🥲")
