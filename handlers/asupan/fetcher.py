import random
import asyncio
import aiohttp
from utils.http import get_http_session
from .constants import DEFAULT_ASUPAN_KEYWORDS


async def fetch_asupan_tikwm(keyword: str | None = None):
    query = keyword.strip() if keyword else random.choice(DEFAULT_ASUPAN_KEYWORDS)

    api_url = "https://www.tikwm.com/api/feed/search"
    payload = {
        "keywords": query,
        "count": 20,
        "cursor": 0,
        "region": "ID",
    }

    session = await get_http_session()
    
    for attempt in range(3):
        try:
            async with session.post(
                api_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < 2:
                await asyncio.sleep(1.5)
                continue
            raise RuntimeError(f"TikWM API network error: {e}")

        if data.get("code") != 0:
            err_msg = data.get('msg', '')
            if "limit" in err_msg.lower():
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
            raise RuntimeError(f"TikWM API error: {err_msg}")

        videos = data.get("data", {}).get("videos") or []
        if not videos:
            raise RuntimeError("Asupan kosong")

        return random.choice(videos)["play"]
