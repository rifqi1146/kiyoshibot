"""Downloader Simontok (simontok.study)

FLOW DOWNLOADER
---------------
1. `is_simontok_url(url)`: Cocokkan host simontok.study + slug post.
2. `simontok_download(...)`: Scrape post → resolve HLS → unduh segmen paralel
   dengan progress → remux mp4 → video meta (durasi dari #EXTINF).
3. Mode `mp3`: audio diekstrak (libmp3lame). Draft pesan status ditangani
   berurutan dengan standard progress downloader lain.
"""
import os
import shutil
import logging
import asyncio
from urllib.parse import urlparse

from handlers.dl.constants import TMP_DIR
from handlers.dl.progress import edit_status
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded

from . import extractor as ext
from .constants import SIMONTOK_HOSTS

log = logging.getLogger(__name__)


def is_simontok_url(url: str) -> bool:
    """True jika url adalah post simontok.study."""
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in SIMONTOK_HOSTS:
            return False
        path = (parsed.path or "").strip("/")
        return bool(path)
    except Exception:
        return False


async def simontok_download(
    raw_url,
    fmt_key,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
):
    del format_id, has_audio, known_size

    url = (raw_url or "").strip()
    work_dir = ext.new_work_dir()
    final_path = None

    try:
        if not metadata_ready:
            await edit_status(
                bot, chat_id, status_msg_id,
                "<b>Scraping Simontok metadata...</b>", label="Simontok download",
            )

        post = await asyncio.to_thread(ext.scrape_post, url)
        title = sanitize_filename(post.get("title") or "Simontok Video", 100)

        hls = await asyncio.to_thread(ext.resolve_hls, post["embed_url"])
        segs = await asyncio.to_thread(ext.parse_segments, hls["m3u8_url"], hls["origin"] + "/")
        seg_urls = segs["segments"] or []
        if not seg_urls:
            raise RuntimeError("Tidak ada segmen di playlist")

        referer = hls["origin"] + "/"
        seg_files = await ext.download_segments(
            seg_urls, referer, work_dir, bot, chat_id, status_msg_id, title,
        )

        tmp_mp4 = os.path.join(work_dir, "out.mp4")
        await asyncio.to_thread(ext.concat_segments, seg_files, tmp_mp4, work_dir)

        if not os.path.exists(tmp_mp4) or os.path.getsize(tmp_mp4) <= 0:
            raise RuntimeError("Gagal menggabungkan video (file kosong)")

        out_ext = "mp3" if fmt_key == "mp3" else "mp4"
        final_path = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.{out_ext}")
        if fmt_key == "mp3":
            await asyncio.to_thread(ext.extract_audio, tmp_mp4, final_path)
            result = {"path": final_path, "title": title, "duration": segs["duration"]}
            cover = await asyncio.to_thread(
                ext.download_thumb, hls["poster"], os.path.join(work_dir, "thumb.jpg"),
            )
            if cover:
                result["thumb"] = cover
                result["artist"] = title
            return result

        # Codec video post ini tak dikenal ffmpeg (stream terbaca `bin_data`) ->
        # remux audio-only. Tolak lebih dulu, jangan kirim file rusak diam-diam.
        if not await asyncio.to_thread(ext.has_video_stream, tmp_mp4):
            raise RuntimeError(
                "Video post ini rusak atau tidak didukung (stream video memakai codec "
                "yang tidak bisa didekode oleh server). Silakan coba post lain."
            )

        shutil.move(tmp_mp4, final_path)
        log.info(
            "Simontok sukses | title=%r duration=%ss segs=%d size=%.2fMB",
            title, segs["duration"], len(seg_urls),
            os.path.getsize(final_path) / 1024 / 1024,
        )
        return {"path": final_path, "title": title, "duration": segs["duration"]}

    except FileSizeLimitExceeded:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    except Exception:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
