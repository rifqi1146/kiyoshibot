import uuid
import logging
import asyncio
import aiohttp
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR
from handlers.dl.utils import sanitize_filename

log = logging.getLogger(__name__)


def _tikwm_abs(value: str) -> str:
    if not value:
        return ""
    return value if value.startswith("http") else "https://www.tikwm.com" + value


def _tikwm_pick_video(info: dict) -> tuple[str, int]:
    hd = _tikwm_abs(str(info.get("hdplay") or ""))
    if hd:
        return hd, int(info.get("hd_size") or 0)
    sd = _tikwm_abs(str(info.get("play") or info.get("play_url") or ""))
    if sd:
        return sd, int(info.get("size") or 0)
    watermark = _tikwm_abs(str(info.get("wmplay") or ""))
    if watermark:
        return watermark, int(info.get("wm_size") or 0)
    return "", 0

async def _tikwm_result(url, bot, chat_id, status_msg_id, fmt_key="mp4"):
    from .main import _download_with_best_engine, _download_album_images, USER_AGENT

    session = await get_http_session()
    last_data = None
    for attempt in range(3):
        try:
            async with session.post("https://www.tikwm.com/api/", data={"url": url, "hd": "1"}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                last_data = await r.json()
        except Exception as e:
            log.warning("Tikwm request failed | attempt=%s url=%s err=%r", attempt + 1, url, e)
            last_data = None
            await asyncio.sleep(0.6 * (attempt + 1))
            continue
        if (last_data or {}).get("data"):
            break
        await asyncio.sleep(0.6 * (attempt + 1))

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

    video_url, known_size = _tikwm_pick_video(info)
    if not video_url:
        raise RuntimeError("TikTok download failed (no video/images from tikwm)")

    title = (info.get("title") or info.get("desc") or "TikTok Video").strip() or "TikTok Video"
    desc = info.get("desc") or info.get("title") or ""
    out_path = f"{TMP_DIR}/{uuid.uuid4().hex}_{sanitize_filename(title)}.mp4"
    await _download_with_best_engine(session, video_url, out_path, bot, chat_id, status_msg_id, "Downloading TikTok video (tikwm)...", known_size=known_size)
    return {
        "path": out_path,
        "title": title,
        "desc": desc,
        "source": "tikwm",
        "kind": "video",
        "duration": int(info.get("duration") or 0),
    }
    