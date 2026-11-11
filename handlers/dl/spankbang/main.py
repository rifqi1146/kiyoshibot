import os
import time
import uuid
import shutil
import logging
import asyncio
from urllib.parse import urlparse

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded
from handlers.dl.proc import spawn_process, DEVNULL
from .constants import SPANKBANG_HOSTS

log = logging.getLogger(__name__)

_VARIANT_CACHE: dict = {}
_CACHE_TTL = 300.0

_REJECT_SEGMENTS = frozenset(
    {
        "s",
        "new",
        "top",
        "trending",
        "upcoming",
        "tags",
        "pornstars",
        "channels",
        "playlists",
        "playlist",
        "categories",
        "category",
        "login",
        "signup",
        "premium",
        "search",
        "profile",
        "settings",
        "help",
        "terms",
        "privacy",
        "dmca",
        "upload",
        "random",
        "best",
        "popular",
        "latest",
        "verified",
        "hd",
        "4k",
        "vr",
    }
)


def _import_extractor():
    from . import extractor

    return extractor


def is_spankbang_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    if not any(host == h or host.endswith("." + h) for h in SPANKBANG_HOSTS):
        return False
    segments = [s for s in (parsed.path or "").split("/") if s]
    if not segments:
        return False
    head = segments[0].lower()
    if head in _REJECT_SEGMENTS:
        return False
    if len(segments) >= 2 and segments[1].lower() in ("video", "embed"):
        return True
    return len(segments) == 1 and 4 <= len(head) <= 10 and head.isalnum() and any(c.isdigit() for c in head)


def _prune_cache():
    now = time.time()
    for key in [k for k, v in list(_VARIANT_CACHE.items()) if now - v.get("ts", 0) > _CACHE_TTL]:
        _VARIANT_CACHE.pop(key, None)


def cache_probe(post_url: str, probe: dict) -> None:
    _prune_cache()
    _VARIANT_CACHE[(post_url or "").strip()] = {"ts": time.time(), "probe": probe}


def peek_probe(post_url: str) -> dict | None:
    key = (post_url or "").strip()
    item = _VARIANT_CACHE.get(key)
    if not item:
        return None
    if time.time() - item.get("ts", 0) > _CACHE_TTL:
        _VARIANT_CACHE.pop(key, None)
        return None
    return item.get("probe")


def probe_spankbang(raw_url: str) -> dict:
    ext = _import_extractor()
    key = ext.canonical_video_url(raw_url)
    cached = peek_probe(key)
    if cached:
        return cached
    data = ext.scrape_post(key)
    cache_probe(key, data)
    return data


async def spankbang_download(
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
    url = ext.canonical_video_url(raw_url)

    probe = peek_probe(url)
    from_cache = bool(probe)
    if not probe:
        probe = await asyncio.to_thread(ext.scrape_post, url)
        cache_probe(url, probe)

    title = probe.get("title") or "SpankBang Video"
    variants = probe.get("variants") or []

    chosen_fid = (format_id or "").strip().lower()
    target_var = next((v for v in variants if v["format_id"] == chosen_fid), None)
    if not target_var and variants:
        target_var = variants[-1] if fmt_key == "mp3" else variants[0]
    if not target_var:
        raise RuntimeError("Video format not found.")
    declared_total = int(target_var.get("filesize") or 0)
    if declared_total > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({declared_total / 1024 ** 3:.2f} GB). Download canceled."
        )

    work_dir = os.path.join(TMP_DIR, f"sb_{uuid.uuid4().hex[:10]}")
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
                "<b>Downloading SpankBang...</b>",
                label="SpankBang",
            )

        try:
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
        except FileSizeLimitExceeded:
            raise
        except Exception:
            if not from_cache:
                raise
            _VARIANT_CACHE.pop(url, None)
            probe = await asyncio.to_thread(ext.scrape_post, url)
            cache_probe(url, probe)
            variants = probe.get("variants") or []
            target_var = next((v for v in variants if v["format_id"] == chosen_fid), None)
            if not target_var and variants:
                target_var = variants[-1] if fmt_key == "mp3" else variants[0]
            if not target_var:
                raise RuntimeError("Video format not found.")
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
            return {
                "path": mp3_path,
                "title": title,
                "duration": probe.get("duration") or 0,
                "thumb": probe.get("thumbnail"),
                "artist": "SpankBang",
            }

        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(final_path, out_video)
        height = int(target_var.get("height") or 0)
        width = 3840 if height >= 2160 else 1920 if height >= 1080 else 1280 if height >= 720 else 854 if height >= 480 else 426
        return {
            "path": out_video,
            "title": title,
            "duration": probe.get("duration") or 0,
            "thumb": probe.get("thumbnail"),
            "width": width,
            "height": height,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
