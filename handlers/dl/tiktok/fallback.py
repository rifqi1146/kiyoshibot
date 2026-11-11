import uuid
import logging
import asyncio
import aiohttp
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR
from handlers.dl.utils import sanitize_filename

log = logging.getLogger(__name__)

async def _tikwm_result(url, bot, chat_id, status_msg_id, fmt_key="mp4"):
    from .main import _download_with_best_engine, _download_album_images, USER_AGENT

    session = await get_http_session()
    last_data = None
    for attempt in range(3):
        try:
            async with session.post("https://www.tikwm.com/api/", data={"url": url}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                last_data = await r.json()
        except Exception as e:
            log.warning("Tikwm request failed | attempt=%s url=%s err=%r", attempt + 1, url, e)
            last_data = None
            await asyncio.sleep(0.6 * (attempt + 1))
            continue
        break

    data = last_data or {}
    info = data.get("data") or {}

    if fmt_key == "mp3":
        music_url = info.get("music") or (info.get("music_info") or {}).get("play")
        if not music_url:
            raise RuntimeError("Audio not found")
        tmp_audio = f"{TMP_DIR}/{uuid.uuid4().hex}.mp3"
        await _download_with_best_engine(session, music_url, tmp_audio, bot, chat_id, status_msg_id, "Downloading TikTok audio (tikwm)...")
        return {
            "path": tmp_audio,
            "title": (info.get("title") or info.get("desc") or "TikTok Audio").strip() or "TikTok Audio",
            "source": "tikwm",
            "kind": "audio",
        }

    images = info.get("images") or []
    if images:
        title = (info.get("title") or info.get("desc") or "TikTok Slideshow").strip() or "TikTok Slideshow"
        urls = [str(x).strip() for x in images if str(x).strip()]
        items = await _download_album_images(
            session,
            urls,
            title,
            bot,
            chat_id,
            status_msg_id,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.tiktok.com/"},
        )
        return {"items": items, "title": title, "source": "tikwm", "kind": "album"}

    video_url = info.get("play") or info.get("wmplay") or info.get("hdplay") or info.get("play_url")
    if not video_url:
        raise RuntimeError("TikTok download failed (no video/images from tikwm)")

    title = (info.get("title") or info.get("desc") or "TikTok Video").strip() or "TikTok Video"
    desc = info.get("desc") or info.get("title") or ""
    out_path = f"{TMP_DIR}/{uuid.uuid4().hex}_{sanitize_filename(title)}.mp4"
    await _download_with_best_engine(session, video_url, out_path, bot, chat_id, status_msg_id, "Downloading TikTok video (tikwm)...")
    return {"path": out_path, "title": title, "desc": desc, "source": "tikwm", "kind": "video"}
    