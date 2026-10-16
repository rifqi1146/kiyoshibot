import asyncio
import logging
import os
import shutil
import subprocess
import time
import uuid
from urllib.parse import urlparse

from handlers.dl.constants import MAX_TG_SIZE, TMP_DIR
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename
from .extractor import (
    download_video,
    fetch_detail,
    fetch_streams,
    parse_moviebox_url,
    resolve_moviebox_streams,
)
from .constants import MOVIEBOX_HOSTS, MOVIEBOX_PROGRESS_INTERVAL

log = logging.getLogger(__name__)

_PROBE_CACHE: dict = {}
_CACHE_TTL = 300.0


def is_moviebox_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in tuple(h[4:] if h.startswith("www.") else h for h in MOVIEBOX_HOSTS):
        return False
    path = (parsed.path or "").strip("/")
    return path.startswith("movies/") or path.startswith("moviesDetail/")


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


def probe_moviebox(raw_url: str, season: int | None = None, episode: int | None = None) -> dict:
    url = (raw_url or "").strip()
    cached = peek_probe(url)
    if cached:
        if (season is None or cached.get("season") == season) and (episode is None or cached.get("episode") == episode):
            return cached

    parsed = parse_moviebox_url(url)
    se = int(season) if season is not None else parsed["season"]
    ep = int(episode) if episode is not None else parsed["episode"]

    detail = fetch_detail(parsed["subject_id"] or parsed["slug"])
    if not detail:
        raise RuntimeError("MovieBox detail not found. Check the URL and try again.")

    is_tv = detail.get("is_tv", False)
    if is_tv and se == 0 and ep == 0:
        se, ep = 1, 1

    streams = fetch_streams(
        detail["subject_id"], se=se, ep=ep, slug=detail["slug"], is_tv=is_tv
    )
    if not streams:
        label = f" S{se}E{ep}" if is_tv else ""
        raise RuntimeError(f"No download variants found for {detail.get('title')}{label}.")

    variants = []
    for s in streams:
        res = s.get("resolution") or "480"
        try:
            h = int("".join(ch for ch in str(res) if ch.isdigit()) or "480")
        except ValueError:
            h = 480
        variants.append({
            "format_id": f"{h}p",
            "label": f"{h}p",
            "height": h,
            "width": 1920 if h >= 1080 else (1280 if h >= 720 else (854 if h >= 480 else 640)),
            "filesize": s.get("size") or 0,
            "total_size": s.get("size") or 0,
            "url": s.get("url"),
            "codec": s.get("codec") or "h264",
            "format": s.get("format") or "MP4",
            "ext": "mp4",
            "has_audio": True,
            "fps": "-",
        })

    dedup = {}
    for v in variants:
        prev = dedup.get(v["height"])
        if not prev or v["total_size"] > prev["total_size"]:
            dedup[v["height"]] = v
    clean = sorted(dedup.values(), key=lambda x: x["height"], reverse=True)

    title = detail.get("title") or "MovieBox Video"
    if is_tv:
        title = f"{title} S{se}E{ep}"

    probe = {
        "title": title,
        "raw_title": detail.get("title") or title,
        "subject_id": detail.get("subject_id"),
        "slug": detail.get("slug"),
        "is_tv": is_tv,
        "season": se,
        "episode": ep,
        "seasons": detail.get("seasons") or [],
        "thumbnail": detail.get("thumbnail") or "",
        "description": detail.get("description") or "",
        "genres": detail.get("genres") or "",
        "country": detail.get("country") or "",
        "rating": detail.get("rating") or "",
        "year": detail.get("year") or "",
        "duration": 0,
        "variants": clean,
        "res_list": clean,
    }
    cache_probe(url, probe)
    return probe


async def moviebox_download(
    raw_url: str,
    fmt_key: str,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
    season: int | None = None,
    episode: int | None = None,
):
    del has_audio, known_size
    url = (raw_url or "").strip()

    probe = peek_probe(url)
    if not probe or (
        (season is not None and probe.get("season") != season)
        or (episode is not None and probe.get("episode") != episode)
    ):
        probe = await asyncio.to_thread(probe_moviebox, url, season, episode)
        cache_probe(url, probe)

    title = probe.get("title") or "MovieBox Video"
    variants = probe.get("variants") or []
    chosen_fid = (format_id or "").strip().lower()

    target = next((v for v in variants if v["format_id"].lower() == chosen_fid), None)
    if not target and variants:
        target = variants[0]
    if not target:
        raise RuntimeError("Video format not found.")

    work_dir = os.path.join(TMP_DIR, f"mb_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    temp_video = os.path.join(work_dir, f"{uuid.uuid4().hex[:8]}.mp4")

    try:
        notify = bool(status_msg_id)
        if notify and not metadata_ready:
            from handlers.dl.progress import edit_status
            await edit_status(
                bot, chat_id, status_msg_id,
                f"<b>Downloading {title}...</b>", label="MovieBox",
            )

        res = await download_video(
            url=url,
            format_id=target["format_id"],
            out_path=temp_video,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            season=probe.get("season"),
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
            proc = await asyncio.to_thread(
                subprocess.Popen,
                ffmpeg_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.to_thread(proc.wait)
            if proc.returncode != 0 or not os.path.exists(mp3_path):
                raise RuntimeError("Failed to extract MP3 audio from video.")
            return {
                "path": mp3_path,
                "title": title,
                "duration": 0,
                "thumb": probe.get("thumbnail"),
                "artist": "MovieBox",
            }

        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(final_path, out_video)
        height = target.get("height", 720)
        width = target.get("width") or (1280 if height >= 720 else 854)
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
