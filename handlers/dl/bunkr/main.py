"""Downloader Bunkr (bunkr.cr, bunkr.ph, bunkr.si, ...).

FLOW DOWNLOADER BUNKR
---------------------
1. URL Matching: `is_bunkr_url(url)` cocok host Bunkr wildcard (`bunkr.*`,
   `bunkrr.*`) dengan path `/a/<id>` (album) atau `/f/<id>` (file tunggal).

2. Album (`/a/<id>`):
   - `extractor.scrape_album` -> daftar item (gambar + video).
   - `extractor.download_album` mengunduh semuanya -> `{items:[{path,type}],title}`.
   - Kalau `fmt_key == "mp3"`: cari video di album, unduh, ekstrak audio.

3. File tunggal (`/f/<id>`):
   - `extractor.resolve_file` -> URL CDN bertanda-tangan.
   - Gambar (jpg/png/...) -> album Telegram satu item.
   - Video -> unduh penuh + progress; MP4 dikirim apa adanya.

4. Batas ukuran MAX_TG_SIZE (2GB) dijaga di `download_to_file`.
5. `fmt_key == "mp3"` -> unduh video lalu `extract_audio` (ffmpeg libmp3lame)...

Tidak ada fallback ke yt-dlp: rantai token CDN Bunkr hanya bisa dilewati lewat
endpoint `api/_001_v2` + `glb-apisign.cdn.cr` yang dipakai scraper ini.
"""
import os
import uuid
import shutil
import logging
import asyncio
from urllib.parse import urlsplit

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded

from . import extractor as ext
from .constants import BUNKR_KNOWN_HOSTS, _IMAGE_EXT, _VIDEO_EXT

log = logging.getLogger(__name__)


def _host_of(url: str) -> str:
    return (urlsplit((url or "").strip()).hostname or "").lower()


def _is_bunkr_host(host: str) -> bool:
    """Host Bunkr dikenal, atau wildcard `bunkr.*` / `bunkrr.*`."""
    if not host:
        return False
    for known in BUNKR_KNOWN_HOSTS:
        if host == known or host.endswith("." + known):
            return True
    for base in ("bunkr.", "bunkrr."):
        if host.startswith(base) and "." not in host[len(base):] and len(host) > len(base):
            return True
    for base in ("bunkr.", "bunkrr."):
        if host.startswith("www." + base) and "." not in host[len("www." + base):]:
            return True
    return False


def is_bunkr_url(url: str) -> bool:
    """Cocok untuk album (`/a/<id>`) atau file tunggal (`/f/<id>`) Bunkr."""
    p = urlsplit((url or "").strip())
    if not _is_bunkr_host((p.hostname or "").lower()):
        return False
    path = p.path or ""
    return "/a/" in path or "/f/" in path


def _is_album_url(url: str) -> bool:
    return "/a/" in (urlsplit((url or "").strip()).path or "")


def _is_image_name(name: str) -> bool:
    low = (name or "").lower()
    return low.endswith(_IMAGE_EXT)


async def bunkr_download(
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
    """Unduh album / file tunggal Bunkr. Lihat FLOW DOWNLOADER di atas."""
    del format_id, has_audio, known_size

    work_dir = os.path.join(TMP_DIR, f"bunkr_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    final_path = None
    try:
        if not metadata_ready:
            await ext._safe_edit_status(
                bot, chat_id, status_msg_id, "<b>Scraping bunkr...</b>"
            )

        # ---------- ALBUM ----------
        if _is_album_url(raw_url):
            album = await asyncio.to_thread(ext.scrape_album, raw_url)
            title = sanitize_filename(album.get("title") or "Bunkr", 100)
            items = album.get("items") or []

            videos = [it for it in items if it.get("type") == "video"]
            images = [it for it in items if it.get("type") == "image"]
            log.info(
                "Bunkr album | title=%r total=%d gambar=%d video=%d",
                title, len(items), len(images), len(videos),
            )

            if fmt_key == "mp3":
                if not videos:
                    raise RuntimeError("Tidak ada video di album untuk dijadikan MP3")
                info = await asyncio.to_thread(ext.resolve_file, videos[0]["page"])
                src = os.path.join(work_dir, "source" + ext.extension_for(info["name"], ".mp4"))
                await ext.download_to_file(
                    info["url"], src, bot, chat_id, status_msg_id, title,
                    notify=True, referer=info.get("referer") or "",
                )
                final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_bunkr.mp3")
                await asyncio.to_thread(ext.extract_audio, src, final_path)
                if os.path.getsize(final_path) > MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("Audio exceeds 2GB limit. Download canceled.")
                return {"path": final_path, "title": title}

            result = await ext.download_album(
                items, title, bot, chat_id, status_msg_id, TMP_DIR,
                tag="bunkr", item_label="media",
            )
            video_paths = [it["path"] for it in result["items"] if it.get("type") == "video"]
            log.info(
                "Bunkr album sukses | title=%r terkirim=%d video=%d",
                title, len(result["items"]), len(video_paths),
            )
            return result

        # ---------- FILE TUNGGAL ----------
        info = await asyncio.to_thread(ext.resolve_file, raw_url)
        name = info.get("name") or "Bunkr"
        title = sanitize_filename(os.path.splitext(name)[0] or "Bunkr", 100)
        log.info("Bunkr file tunggal | name=%r type=%s", name, ext.extension_for(name))

        if _is_image_name(name) and fmt_key != "mp3":
            return await ext.download_album(
                [{"page": raw_url, "name": name, "type": "image"}],
                title, bot, chat_id, status_msg_id, TMP_DIR,
                tag="bunkr", item_label="media",
            )

        if fmt_key == "mp3":
            src = os.path.join(work_dir, "source" + ext.extension_for(name, ".mp4"))
            await ext.download_to_file(
                info["url"], src, bot, chat_id, status_msg_id, title,
                notify=True, referer=info.get("referer") or "",
            )
            final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_bunkr.mp3")
            await asyncio.to_thread(ext.extract_audio, src, final_path)
            if os.path.getsize(final_path) > MAX_TG_SIZE:
                raise FileSizeLimitExceeded("Audio exceeds 2GB limit. Download canceled.")
            return {"path": final_path, "title": title}

        ext_name = ext.extension_for(name, ".mp4")
        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_bunkr{ext_name}")
        await ext.download_to_file(
            info["url"], final_path, bot, chat_id, status_msg_id, title,
            notify=True, referer=info.get("referer") or "",
        )
        size = os.path.getsize(final_path)
        if size > MAX_TG_SIZE:
            raise FileSizeLimitExceeded(
                f"Video exceeds 2GB limit ({size / 1024 / 1024 / 1024:.2f} GB). Download canceled."
            )
        log.info("Bunkr sukses | title=%r size=%.2fMB", title, size / 1024 / 1024)
        return {"path": final_path, "title": title}
    except Exception:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
