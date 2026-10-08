"""Downloader PunishWorld (punishworld.com).

FLOW DOWNLOADER
---------------
1. is_punishworld_url() memverifikasi domain.
2. Scrape metadata + sources (ambil yang resolusinya highest).
3. Jika MP3: download MP4, ekstrak audio, pasang thumb.
4. Jika MP4: langsung kirim ke user (worker router.py yg upload).
"""
import os
import uuid
import shutil
import logging
import asyncio
from urllib.parse import urlparse

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded

log = logging.getLogger(__name__)


def _import_extractor():
    from . import extractor
    return extractor


def is_punishworld_url(url: str) -> bool:
    """True jika url adalah punishworld.com"""
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower()
        if host == "punishworld.com" or host.endswith(".punishworld.com"):
            return bool(parsed.path) and parsed.path != "/"
        return False
    except Exception:
        return False


async def punishworld_download(
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

    ext = _import_extractor()
    work_dir = os.path.join(TMP_DIR, f"punish_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    final_path = None

    try:
        if not metadata_ready:
            await ext._safe_edit_status(bot, chat_id, status_msg_id, "<b>Scraping punishworld.com...</b>")

        post = await asyncio.to_thread(ext.scrape_post, raw_url)
        title = sanitize_filename(post.get("title") or "PunishWorld", 100)
        sources = post.get("sources") or []

        if not sources:
            raise RuntimeError("No video to download in this post")

        item = ext.pick_best(sources)
        url = item["url"]

        if fmt_key == "mp3":
            raw_path = os.path.join(work_dir, "source.mp4")
            await ext.download_to_file(url, raw_path, bot, chat_id, status_msg_id, title)
            final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_punish.mp3")
            await asyncio.to_thread(ext.extract_audio, raw_path, final_path)
            result = {"path": final_path, "title": title}
            cover = await asyncio.to_thread(
                ext.download_thumb,
                post.get("thumbnail"),
                os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_punish_thumb.jpg"),
            )
            if cover:
                result["thumb"] = cover
                result["artist"] = title
            return result

        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_punish.mp4")
        await ext.download_to_file(url, final_path, bot, chat_id, status_msg_id, title)

        log.info(
            "PunishWorld sukses | title=%r quality=%s size=%.2fMB",
            title, item.get("label"), os.path.getsize(final_path) / 1024 / 1024,
        )
        return {"path": final_path, "title": title}

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
