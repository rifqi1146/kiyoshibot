import asyncio
import time
from urllib.parse import urlsplit

import aiohttp

from .system_info import psutil

try:
    from handlers.quotly import QUOTE_API_URI
except Exception:
    QUOTE_API_URI = None


def gather_process_stats():
    if not psutil:
        return {}
    try:
        proc = psutil.Process()
        proc.cpu_percent()
        time.sleep(1.0)
        return {
            "uptime": max(0, int(time.time() - proc.create_time())),
            "rss": proc.memory_info().rss,
            "cpu": proc.cpu_percent(),
            "threads": proc.num_threads(),
            "fds": proc.num_fds(),
        }
    except (psutil.Error, OSError):
        return {}


async def sample_loop_delay():
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.sleep(0.2)
    return max(0.0, loop.time() - start - 0.2) * 1000


async def _probe_quote():
    if not QUOTE_API_URI:
        return "Not configured"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as session:
            async with session.get(QUOTE_API_URI.rstrip('/') + "/health", allow_redirects=False) as response:
                return "OK" if response.status == 200 else "Unavailable"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "Unavailable"


async def gather_diagnostics(bot):
    process, delay, quote = await asyncio.gather(
        asyncio.to_thread(gather_process_stats), sample_loop_delay(), _probe_quote()
    )
    hostname = urlsplit(str(bot.base_url)).hostname
    mode = "Local" if hostname in ("127.0.0.1", "localhost", "::1") else "Cloud / custom"
    return {"process": process, "loop_delay_ms": delay, "api_mode": mode, "quote": quote}
