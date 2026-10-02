"""Downloader CosXplay (cosxplay.com)

FLOW DOWNLOADER
---------------
1. `is_cosxplay_url(url)`: Cocokkan host cosxplay.com dan slug post.
2. `probe_cosxplay(url)`: Ekstrak resolusi dan simpan ke `_VARIANT_CACHE` (TTL 5 menit).
3. `cosxplay_download(...)`: Unduh resolusi pilihan (high/low) atau format terbaik.
4. Mendukung konversi mp3 (libmp3lame via ffmpeg) jika `fmt_key == 'mp3'`.
5. Silent download mode aman: picker resolusi dihapus otomatis saat dipilih, status message ditangani terpisah.
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
from .constants import COSXPLAY_HOSTS

log = logging.getLogger(__name__)

_VARIANT_CACHE: dict = {}
_CACHE_TTL = 300.0


def _import_extractor():
    from . import extractor
    return extractor


def is_cosxplay_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "cosxplay.com":
        return False
    path = (parsed.path or "").strip("/")
    # Slug format: digits-slug
    return bool(path and any(char.isdigit() for char in path.split("/")[0]))


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


def probe_cosxplay(raw_url: str) -> dict:
    url = (raw_url or "").strip()
    cached = peek_probe(url)
    if cached:
        return cached

    ext = _import_extractor()
    data = ext.scrape_post(url)
    cache_probe(url, data)
    return data


async def cosxplay_download(
    raw_url: str,
    fmt_key: str,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
):
    del has_audio, known_size
    ext = _import_extractor()
    url = (raw_url or "").strip()
    
    probe = peek_probe(url)
    if not probe:
        probe = await asyncio.to_thread(ext.scrape_post, url)
        cache_probe(url, probe)
        
    title = probe.get("title") or "CosXplay Video"
    variants = probe.get("variants") or []
    
    # Pilih varian format_id yang dipilih atau varian pertama (terbaik)
    chosen_fid = (format_id or "").strip().lower()
    target_var = next((v for v in variants if v["format_id"] == chosen_fid), None)
    if not target_var and variants:
        target_var = variants[0]
        
    if not target_var:
        raise RuntimeError("Format video tidak ditemukan.")

    work_dir = os.path.join(TMP_DIR, f"cx_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    temp_video = os.path.join(work_dir, f"{uuid.uuid4().hex[:8]}.mp4")
    
    try:
        notify = bool(status_msg_id)
        if notify and not metadata_ready:
            from handlers.dl.progress import edit_status
            await edit_status(bot, chat_id, status_msg_id, "<b>Downloading CosXplay...</b>", label="CosXplay")

        res = await ext.download_video(
            url=url,
            format_id=target_var["format_id"],
            out_path=temp_video,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            notify=notify
        )

        final_path = res["path"]
        
        # Jika mode audio / mp3
        if fmt_key == "mp3":
            mp3_path = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp3")
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", final_path,
                "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                mp3_path
            ]
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            if proc.returncode != 0 or not os.path.exists(mp3_path):
                raise RuntimeError("Gagal mengekstrak audio MP3 dari video.")
            return {
                "path": mp3_path,
                "title": title,
                "duration": probe.get("duration") or 0,
                "thumb": probe.get("thumbnail"),
                "artist": "CosXplay"
            }
            
        # Pindahkan file ke TMP_DIR agar tidak ikut terhapus di finally
        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(final_path, out_video)
        return {
            "path": out_video,
            "title": title,
            "duration": probe.get("duration") or 0,
            "thumb": probe.get("thumbnail"),
            "width": 1920 if target_var["height"] >= 1080 else (1280 if target_var["height"] >= 720 else 426),
            "height": target_var["height"]
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
