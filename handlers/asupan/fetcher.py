import random
import asyncio
import logging
import json
import time
import uuid
import os
from pathlib import Path
import aiohttp
import aiofiles
from scrapling.fetchers import StealthyFetcher
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR
from .constants import DEFAULT_ASUPAN_KEYWORDS

log = logging.getLogger(__name__)
logging.getLogger("scrapling").setLevel(logging.ERROR)

_VIDEO_POOL: list[dict] = []
_FETCH_LOCK = asyncio.Lock()

def _fetch_api_in_browser(query: str) -> list[dict]:
    """Mengeksekusi request di browser dan menyimpan 20 video sekaligus ke memori."""
    result_container = []

    def page_action(page):
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)

        js_script = f"""
        (async () => {{
            try {{
                const resp = await fetch("/api/feed/search", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest"
                    }},
                    body: new URLSearchParams({{
                        keywords: {json.dumps(query)},
                        count: "20",
                        cursor: "0",
                        web: "1",
                        hd: "1"
                    }})
                }});
                const data = await resp.json();
                
                if (!data || data.code !== 0 || !data.data || !data.data.videos) {{
                    return [];
                }}

                // Saring foto/slideshow dan kembalikan array video MP4 murni
                return data.data.videos.filter(v => !v.images || v.images.length === 0).map(v => ({{
                    id: v.video_id || v.id,
                    unique_id: (v.author && v.author.unique_id) ? v.author.unique_id : "_",
                    play: v.play || v.wmplay || ""
                }}));
            }} catch (err) {{
                return [];
            }}
        }})()
        """
        
        try:
            data = page.evaluate(js_script)
            if isinstance(data, list):
                result_container.extend(data)
        except Exception as e:
            log.warning("Gagal mengevaluasi JS priming: %s", e)

    fetcher = StealthyFetcher()
    fetcher.fetch(
        "https://www.tikwm.com/",
        headless=True,
        solve_cloudflare=True,
        timeout=40000,
        page_action=page_action
    )

    return result_container

async def _prime_and_get_url(video_item: dict) -> str:
    """Memanggil endpoint downloader biasa (/api/) tanpa browser untuk mendapatkan URL stream MP4 asli."""
    target_url = f"https://www.tiktok.com/@{video_item['unique_id']}/video/{video_item['id']}"
    api_url = "https://www.tikwm.com/api/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }

    session = await get_http_session()
    try:
        async with session.post(api_url, headers=headers, data={"url": target_url, "hd": "1"}, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0 and data.get("data"):
                    vid_data = data.get("data", {})
                    return vid_data.get("play") or vid_data.get("hdplay") or vid_data.get("wmplay")
    except Exception as e:
        log.debug("Priming via API gagal, fallback ke URL dummy: %s", e)
    
    fallback = video_item.get("play")
    if fallback and fallback.startswith("/"):
        return f"https://www.tikwm.com{fallback}"
    return fallback

async def _download_video_to_tmp(media_url: str) -> Path:
    os.makedirs(TMP_DIR, exist_ok=True)
    out_path = Path(TMP_DIR) / f"asupan_{uuid.uuid4().hex[:10]}.mp4"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.tikwm.com/",
        "Accept": "*/*"
    }

    session = await get_http_session()
    async with session.get(media_url, headers=headers, timeout=aiohttp.ClientTimeout(total=45), allow_redirects=True) as resp:
        if resp.status not in (200, 206):
            raise RuntimeError(f"HTTP {resp.status}")
        
        async with aiofiles.open(out_path, "wb") as f:
            async for chunk in resp.content.iter_chunked(64 * 1024):
                if chunk:
                    await f.write(chunk)

    if not out_path.exists() or out_path.stat().st_size <= 0:
        raise RuntimeError("File hasil unduhan CDN kosong")

    return out_path

async def fetch_asupan_tikwm(keyword: str | None = None) -> Path:
    global _VIDEO_POOL

    video_item = None
    if _VIDEO_POOL:
        video_item = _VIDEO_POOL.pop(random.randrange(len(_VIDEO_POOL)))

    if not video_item:
        async with _FETCH_LOCK:
            if _VIDEO_POOL:
                video_item = _VIDEO_POOL.pop(random.randrange(len(_VIDEO_POOL)))
            else:
                query = keyword.strip() if keyword else random.choice(DEFAULT_ASUPAN_KEYWORDS)
                
                log.info("Memulai browser untuk mengisi pool asupan | query=%s", query)
                new_videos = await asyncio.to_thread(_fetch_api_in_browser, query)
                
                if not new_videos:
                    raise RuntimeError("Pencarian asupan kosong (Cloudflare timeout atau filter aktif)")
                    
                _VIDEO_POOL.extend(new_videos)
                log.info("Pool asupan terisi: %s video", len(_VIDEO_POOL))
                
                video_item = _VIDEO_POOL.pop(random.randrange(len(_VIDEO_POOL)))

    final_url = await _prime_and_get_url(video_item)
    if not final_url:
        raise RuntimeError("Gagal mendapatkan stream URL dari video asupan")

    return await _download_video_to_tmp(final_url)
