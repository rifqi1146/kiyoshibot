"""Downloader Drakor.id (drakorid.co)

FLOW DOWNLOADER
---------------
1. `is_drakorid_url(url)`: Cocokkan host drakorid.co dan path video.
2. `probe_drakorid(url)`: Ekstrak resolusi untuk episode yang diminta dan
   simpan ke `_PROBE_CACHE` (TTL 5 menit).
3. `drakorid_download(...)`: Unduh resolusi pilihan (720p/480p/360p) via aria2c/curl_cffi.
4. Mendukung konversi mp3 (`libmp3lame` via ffmpeg) jika `fmt_key == 'mp3'`.
"""
import os
import time
import uuid
import shutil
import logging
import asyncio
from urllib.parse import urlparse

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded
from .constants import DRAKORID_HOSTS
from . import extractor

log = logging.getLogger(__name__)

_PROBE_CACHE: dict = {}
_CACHE_TTL = 300.0


def is_drakorid_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in DRAKORID_HOSTS:
        return False
    path = (parsed.path or "").strip("/")
    return any(path.startswith(prefix) for prefix in ("nonton/", "download-streaming/", "watch-streaming/"))


def _prune_cache():
    now = time.time()
    for key in [k for k, v in list(_PROBE_CACHE.items()) if now - v.get("ts", 0) > _CACHE_TTL]:
        _PROBE_CACHE.pop(key, None)


def cache_probe(post_url: str, probe: dict) -> None:
    _prune_cache()
    _PROBE_CACHE[(post_url or "").strip()] = {"ts": time.time(), "probe": probe}


def peek_probe(post_url: str) -> dict | None:
    item = _PROBE_CACHE.get((post_url or "").strip())
    if not item:
        return None
    if time.time() - item.get("ts", 0) > _CACHE_TTL:
        _PROBE_CACHE.pop((post_url or "").strip(), None)
        return None
    return item.get("probe")


def probe_drakorid(raw_url: str, episode: int | None = None) -> dict:
    url = (raw_url or "").strip()
    cached = peek_probe(url)
    if cached and (episode is None or cached.get("episode") == episode):
        return cached

    post = extractor.scrape_post(url)
    ep = int(episode) if episode else post.get("episode", 1)
    variants = extractor.resolve_episode(post["slug"], ep)
    if not variants:
        raise RuntimeError(
            f"No video variants found for {post.get('title')} Episode {ep}."
        )

    title = f"{post.get('title')} Episode {ep}"
    res = {
        "title": title,
        "drama_title": post.get("title"),
        "slug": post.get("slug"),
        "episode": ep,
        "episodes": post.get("episodes") or [1],
        "total_episodes": post.get("episode_count") or 1,
        "thumbnail": post.get("poster") or "",
        "duration": 0,
        "variants": variants,
        "res_list": sorted(variants, key=lambda x: x["height"], reverse=True),
    }
    cache_probe(url, res)
    return res


async def drakorid_download(
    raw_url: str,
    fmt_key: str,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
    episode: int | None = None,
):
    del has_audio, known_size
    url = (raw_url or "").strip()

    probe = peek_probe(url)
    if not probe or (episode is not None and probe.get("episode") != episode):
        probe = await asyncio.to_thread(probe_drakorid, url, episode=episode)
        cache_probe(url, probe)

    title = probe.get("title") or "Drakor.id Video"
    variants = probe.get("variants") or []
    chosen_fid = (format_id or "").strip().lower()

    target_var = next((v for v in variants if v["format_id"].lower() == chosen_fid), None)
    if not target_var and variants:
        target_var = variants[0]
    if not target_var:
        raise RuntimeError("Video format not found.")

    work_dir = os.path.join(TMP_DIR, f"dk_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    temp_video = os.path.join(work_dir, f"{uuid.uuid4().hex[:8]}.mp4")

    try:
        notify = bool(status_msg_id)
        if notify and not metadata_ready:
            from handlers.dl.progress import edit_status
            await edit_status(
                bot, chat_id, status_msg_id,
                f"<b>Downloading {title}...</b>", label="Drakor.id",
            )

        res = await extractor.download_video(
            url=url,
            format_id=target_var["format_id"],
            out_path=temp_video,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            episode=probe.get("episode"),
            notify=notify,
        )

        final_path = res["path"]

        if fmt_key == "mp3":
            mp3_path = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp3")
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", final_path,
                "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                mp3_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0 or not os.path.exists(mp3_path):
                raise RuntimeError("Failed to extract MP3 audio from video.")
            return {
                "path": mp3_path,
                "title": title,
                "duration": 0,
                "thumb": probe.get("thumbnail"),
                "artist": "Drakor.id",
            }

        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(final_path, out_video)
        height = target_var.get("height", 720)
        width = 1280 if height >= 720 else (854 if height >= 480 else 640)
        return {
            "path": out_video,
            "title": title,
            "duration": 0,
            "thumb": probe.get("thumbnail"),
            "width": width,
            "height": height,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
