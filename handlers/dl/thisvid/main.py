import os
import re
import time
import uuid
import shutil
import logging
import asyncio
from urllib.parse import urlparse

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded
from handlers.dl.proc import spawn_process, DEVNULL
from .constants import THISVID_HOSTS, UA, THISVID_REFERER

log = logging.getLogger(__name__)

_VARIANT_CACHE: dict = {}
_CACHE_TTL = 300.0

_VIDEO_PATH_RE = re.compile(r"^/(?:videos|embed)/[A-Za-z0-9_-]+/?$")
_PLAYLIST_VIDEO_PATH_RE = re.compile(r"^/playlist/\d+/video/[A-Za-z0-9_-]+/?$")

def _import_extractor():
    from . import extractor

    return extractor

def is_thisvid_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or not any(host == h or host.endswith("." + h) for h in ("thisvid.com",)):
        return False
    path = parsed.path or ""
    if _VIDEO_PATH_RE.match(path):
        return True
    if _PLAYLIST_VIDEO_PATH_RE.match(path):
        return True
    return False

def _prune_cache():
    now = time.time()
    for key in [k for k, v in list(_VARIANT_CACHE.items()) if now - v.get("ts", 0) > _CACHE_TTL]:
        _VARIANT_CACHE.pop(key, None)

def cache_probe(post_url: str, probe: dict) -> None:
    _prune_cache()
    _VARIANT_CACHE[(post_url or "").strip()] = {"ts": time.time(), "probe": probe}

def peek_probe(post_url: str) -> dict | None:
    item = _VARIANT_CACHE.get((post_url or "").strip())
    if not item:
        return None
    if time.time() - item.get("ts", 0) > _CACHE_TTL:
        _VARIANT_CACHE.pop((post_url or "").strip(), None)
        return None
    return item.get("probe")

def probe_thisvid(raw_url: str) -> dict:
    url = (raw_url or "").strip()
    cached = peek_probe(url)
    if cached:
        return cached
    ext = _import_extractor()
    data = ext.scrape_post(url)
    cache_probe(url, data)
    return data

def _download_thumb(thumb_url: str, out_path: str) -> str | None:
    if not thumb_url:
        return None
    try:
        from curl_cffi import requests as curl_requests

        r = curl_requests.get(
            thumb_url,
            headers={"User-Agent": UA, "Referer": THISVID_REFERER},
            impersonate="chrome",
            timeout=10,
        )
        if r.status_code == 200 and r.content and len(r.content) > 100:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception as e:
        log.debug("ThisVid thumb fetch failed | err=%r", e)
    return None

async def thisvid_download(
    raw_url: str,
    fmt_key: str,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
    user_id: int | None = None,
    chat_type: str = "",
    strict_adult: bool = False,
    engine: str | None = None,
):
    del has_audio, known_size, user_id, chat_type, strict_adult, engine
    ext = _import_extractor()
    url = (raw_url or "").strip()

    probe = peek_probe(url)
    if not probe:
        probe = await asyncio.to_thread(ext.scrape_post, url)
        cache_probe(url, probe)

    title = probe.get("title") or "ThisVid Video"
    variants = probe.get("variants") or []

    chosen_fid = (format_id or "").strip().lower()
    target_var = next(
        (v for v in variants if v["format_id"].lower() == chosen_fid or str(v["height"]) == chosen_fid),
        None,
    )
    if not target_var and variants:
        target_var = variants[-1] if fmt_key == "mp3" else variants[0]
    if not target_var:
        raise RuntimeError("Video format not found.")

    declared_total = int(target_var.get("filesize") or 0)
    if declared_total > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({declared_total / 1024 ** 3:.2f} GB). Download canceled."
        )

    work_dir = os.path.join(TMP_DIR, f"tv_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    temp_video = os.path.join(work_dir, f"{uuid.uuid4().hex[:8]}.mp4")

    try:
        notify = bool(status_msg_id)
        if notify and not metadata_ready:
            from handlers.dl.progress import edit_status

            await edit_status(
                bot,
                chat_id,
                status_msg_id,
                "<b>Downloading ThisVid...</b>",
                label="ThisVid",
            )

        res = await ext.download_video(
            url=url,
            format_id=target_var["format_id"],
            out_path=temp_video,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            notify=notify,
            variants=variants,
        )

        final_path = res["path"]

        if fmt_key == "mp3":
            mp3_path = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp3")
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                final_path,
                "-vn",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                mp3_path,
            ]
            proc = await spawn_process(*ffmpeg_cmd, stdout=DEVNULL, stderr=DEVNULL)
            await proc.wait()
            if proc.returncode != 0 or not os.path.exists(mp3_path):
                raise RuntimeError("Failed to extract MP3 audio from video.")
            thumb_path = None
            raw_thumb = probe.get("thumbnail")
            if raw_thumb:
                thumb_dest = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}_thumb.jpg")
                thumb_path = await asyncio.to_thread(_download_thumb, raw_thumb, thumb_dest)
            return {
                "path": mp3_path,
                "title": title,
                "duration": probe.get("duration") or 0,
                "thumb": thumb_path,
                "artist": "ThisVid",
            }

        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(final_path, out_video)
        height = int(target_var.get("height") or 0)
        width = 1920 if height >= 1080 else 1280 if height >= 720 else 854 if height >= 480 else 426
        thumb_path = None
        raw_thumb = probe.get("thumbnail")
        if raw_thumb:
            thumb_dest = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}_thumb.jpg")
            thumb_path = await asyncio.to_thread(_download_thumb, raw_thumb, thumb_dest)
        return {
            "path": out_video,
            "title": title,
            "duration": probe.get("duration") or 0,
            "thumb": thumb_path,
            "width": width,
            "height": height,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
