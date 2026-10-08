"""Downloader BDSMLust (bdsmlust.com)

FLOW DOWNLOADER
---------------
1. `is_bdsmlust_url(url)`: Cocokkan host bdsmlust.com + path non-kosong yang
   bukan halaman statis (search, category, page, dst).
2. `probe_bdsmlust(url)`: Ekstrak metadata (title, duration, thumbnail,
   download_url, filesize) dan simpan ke cache singkat (TTL 300s).
3. `bdsmlust_download(...)`: Unduh direct MP4 (via embed bdsmstreak atau
   KVS bdsmx.tube). Mendukung konversi mp3 (libmp3lame via ffmpeg) jika
   `fmt_key == 'mp3'`. Hasil membawa metadata untuk upload Telegram.
"""

import asyncio
import logging
import os
import shutil
import time
import uuid
from urllib.parse import urlparse

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded

from .constants import BDSMLUST_PROGRESS_INTERVAL
from . import extractor

log = logging.getLogger(__name__)

_PROBE_CACHE: dict = {}
_CACHE_TTL = 300.0

_STATIC_PREFIXES = (
    "search", "categories", "bdsm-pornstars", "bdsm-studios",
    "kink-channels", "bdsm-tags", "bdsm-guides", "bdsm-glossary",
    "bdsm-test", "what-is-bdsm", "category", "tag", "page",
    "partners", "contact", "2257-2",
)


def is_bdsmlust_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "bdsmlust.com":
        return False
    path = (parsed.path or "").strip("/")
    if not path:
        return False
    parts = path.split("/")
    if parts[0].lower() in _STATIC_PREFIXES:
        return False
    return True


def _prune_cache():
    now = time.time()
    for key in [k for k, v in list(_PROBE_CACHE.items())
                if now - v.get("ts", 0) > _CACHE_TTL]:
        _PROBE_CACHE.pop(key, None)


def cache_probe(post_url: str, probe: dict) -> None:
    _prune_cache()
    _PROBE_CACHE[(post_url or "").strip()] = {"ts": time.time(), "probe": probe}


def peek_probe(post_url: str) -> dict | None:
    key = (post_url or "").strip()
    item = _PROBE_CACHE.get(key)
    if not item:
        return None
    if time.time() - item.get("ts", 0) > _CACHE_TTL:
        _PROBE_CACHE.pop(key, None)
        return None
    return item.get("probe")


def probe_bdsmlust(raw_url: str) -> dict:
    url = (raw_url or "").strip()
    cached = peek_probe(url)
    if cached:
        return cached
    data = extractor.scrape_post(url)
    cache_probe(url, data)
    return data


async def bdsmlust_download(
    raw_url: str,
    fmt_key: str,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
    engine: str | None = None,
):
    del format_id, has_audio, known_size, engine
    url = (raw_url or "").strip()

    probe = peek_probe(url)
    if not probe:
        probe = await asyncio.to_thread(extractor.scrape_post, url)
        cache_probe(url, probe)

    title = probe.get("title") or "BDSMLust Video"

    work_dir = os.path.join(TMP_DIR, f"bl_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    temp_video = os.path.join(work_dir, f"{uuid.uuid4().hex[:8]}.mp4")

    try:
        notify = bool(status_msg_id)
        if notify and not metadata_ready:
            from handlers.dl.progress import edit_status
            await edit_status(
                bot, chat_id, status_msg_id,
                "<b>Downloading BDSMLust...</b>", label="BDSMLust",
            )

        res = await extractor.download_video(
            url=url,
            out_path=temp_video,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            notify=notify,
        )

        final_path = res["path"]

        # Mode audio / mp3
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
                raise RuntimeError("Failed to extract MP3 audio from BDSMLust video.")
            return {
                "path": mp3_path,
                "title": title,
                "duration": probe.get("duration") or 0,
                "thumb": probe.get("thumbnail"),
                "artist": "BDSMLust",
            }

        # Pindahkan file ke TMP_DIR
        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(final_path, out_video)
        return {
            "path": out_video,
            "title": title,
            "duration": probe.get("duration") or 0,
            "thumb": probe.get("thumbnail"),
            "width": probe.get("width") or 1280,
            "height": probe.get("height") or 720,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
