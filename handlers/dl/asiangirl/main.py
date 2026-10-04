"""Downloader AsianGirlPorn (asiangirl.porn)

FLOW DOWNLOADER
---------------
1. `is_asiangirl_url(url)`: cocokkan host asiangirl.porn + path `/v/<slug>`.
2. `probe_asiangirl(url)`: ambil metadata (`title`, `stream_url`, `poster`) untuk
   picker/status.
3. `asiangirl_download(...)`:
   - scrape ULANG halaman tiap unduh (token stream `/<epoch>/` bertanda tangan).
   - `parse_hls(stream_url)` -> kunci AES + IV + daftar segmen + durasi.
   - `download_segments_aes(...)` -> unduh paralel `SEG_CONCURRENCY=12` +
     dekripsi AES-128-CBC lokal (bukan ffmpeg serial).
   - `concat_segments(...)` -> MP4 faststart. fmt `mp3` -> `extract_audio`.
   - hasil membawa `"remux_done": True` (jangan remux dua kali).
"""

import asyncio
import logging
import os
import shutil
import time
import uuid
from urllib.parse import urlparse

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename

from .constants import UA
from . import extractor

log = logging.getLogger(__name__)

_PROBE_CACHE: dict = {}
_CACHE_TTL = 300.0


def is_asiangirl_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "asiangirl.porn":
        return False
    path = (parsed.path or "").strip("/")
    parts = path.split("/")
    return len(parts) >= 2 and parts[0].lower() == "v" and bool(parts[1])


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


def probe_asiangirl(raw_url: str) -> dict:
    url = (raw_url or "").strip()
    cached = peek_probe(url)
    if cached:
        return cached
    data = extractor.scrape_post(url)
    cache_probe(url, data)
    return data


async def asiangirl_download(
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

    # Token stream bertanda tangan (`/<epoch>/`) — selalu scrape ulang.
    probe = await asyncio.to_thread(extractor.scrape_post, url)
    cache_probe(url, probe)

    title = probe.get("title") or "AsianGirlPorn Video"
    stream_url = probe.get("stream_url") or ""
    if not stream_url:
        raise RuntimeError("Gagal mendapatkan link stream AsianGirlPorn.")

    is_audio = fmt_key == "mp3"
    ext_name = "mp3" if is_audio else "mp4"
    out_path = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.{ext_name}")

    if not metadata_ready and status_msg_id:
        from handlers.dl.progress import edit_status
        await edit_status(
            bot, chat_id, status_msg_id,
            "<b>Downloading AsianGirlPorn...</b>", label="AsianGirlPorn",
        )

    work_dir = os.path.join(TMP_DIR, f"asiangirl_{uuid.uuid4().hex[:8]}")
    os.makedirs(work_dir, exist_ok=True)
    tmp_out = os.path.join(work_dir, f"full.{ext_name}")

    from curl_cffi import requests as curl_requests
    session = curl_requests.Session(impersonate="chrome",
                                    headers={"User-Agent": UA, "Referer": url})

    try:
        t0 = time.monotonic()
        info = await asyncio.to_thread(extractor.parse_hls, stream_url, session)
        log.info(
            "AsianGirlPorn HLS parsed | segs=%d duration=%ds",
            len(info["segments"]), info["duration"],
        )

        seg_files = await extractor.download_segments_aes(
            seg_urls=info["segments"],
            key=info["key"],
            iv=info["iv"],
            session=session,
            work_dir=work_dir,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title_text=title,
        )

        await asyncio.to_thread(
            extractor.concat_segments, seg_files,
            os.path.join(work_dir, "joined.mp4"), work_dir,
        )

        if is_audio:
            await asyncio.to_thread(
                extractor.extract_audio,
                os.path.join(work_dir, "joined.mp4"), tmp_out, title,
            )
        else:
            shutil.move(os.path.join(work_dir, "joined.mp4"), tmp_out)

        size = os.path.getsize(tmp_out)
        if size > MAX_TG_SIZE:
            raise FileSizeLimitExceeded(
                f"File exceeds 2GB limit ({size / 1024 ** 3:.2f} GB). "
                "Download canceled."
            )

        shutil.move(tmp_out, out_path)
        log.info(
            "AsianGirlPorn done | title=%s size=%.1fMB elapsed=%.1fs",
            title, size / 1048576, time.monotonic() - t0,
        )

        result = {
            "path": out_path,
            "title": title,
            "thumb": probe.get("poster"),
            "remux_done": True,
        }
        if is_audio:
            result["artist"] = "AsianGirl.Porn"
        return result

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if os.path.exists(tmp_out) and not os.path.exists(out_path):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
