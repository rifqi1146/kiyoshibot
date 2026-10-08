"""Downloader Pawchive (pawchive.pw)

FLOW DOWNLOADER PAWCHIVE
------------------------
1. URL Matching:
   - `is_pawchive_url(url)` mencocokkan host pawchive.pw dan memastikan path mengandung '/post/'.

2. Status & Inisialisasi:
   - Buat direktori sementara kerja (work_dir di TMP_DIR).
   - Tampilkan status Telegram 'Scraping pawchive.pw...'.

3. Ekstraksi Metadata & Daftar Media:
   - Panggil `extractor.scrape_post(raw_url)` di thread pool.
   - Ambil judul, creator, daftar video, dan daftar gambar/lampiran.
   - Bersihkan dan sanitasi judul dengan `sanitize_filename(title, 100)`.

4. Pemilihan Media (Video vs Album Gambar):
   - Jika post TIDAK memiliki video tetapi memiliki gambar:
     Panggil `extractor.download_album` untuk mengunduh semua gambar menjadi album Telegram.
   - Jika post memiliki video:
     Pilih resolusi terbaik dengan `extractor.pick_best_video(videos)` (prioritas 1080p > 720p > normal).
     Cek batas ukuran MAX_TG_SIZE (2GB).

5. Unduh & Konversi:
   - Mode MP3 (`fmt_key == "mp3"`):
     Unduh source video lalu ekstrak trek audio dengan ffmpeg libmp3lame ke format .mp3.
     Cover album diambil dari thumbnail post (`download_thumb`), diisi ke `thumb` + `artist`.
   - Mode Video:
     Unduh stream langsung dari file.pawchive.pw ke final_path.
     Progress di-throttle 3s sekali (PAWCHIVE_PROGRESS_INTERVAL) + speed/ETA dinamis,
     plus backoff RetryAfter supaya tidak kena flood control Telegram.

6. Return Payload & Cleanup:
   - Kembalikan dict `{"path": final_path, "title": title, ...}` siap kirim.
   - Blok `finally` memastikan direktori kerja sementara selalu dibersihkan (shutil.rmtree).
     Album gambar sengaja ditulis ke TMP_DIR (bukan work_dir) karena work_dir dihapus di sini.
"""
import os
import uuid
import shutil
import logging
import asyncio
from urllib.parse import urlparse

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded


def _import_extractor():
    """Import modul extractor secara lazy supaya package tetap bisa di-import
    walau dependensi (curl_cffi/bs4) belum tersedia saat import awal."""
    from . import extractor
    return extractor


log = logging.getLogger(__name__)


def is_pawchive_url(url: str) -> bool:
    """Cocok untuk halaman post Pawchive (mirip Kemono), mis.
    https://pawchive.pw/patreon/user/97687196/post/142646269"""
    path = urlparse((url or "").strip())
    host = (path.hostname or "").lower()
    if host != "pawchive.pw" and not host.endswith(".pawchive.pw"):
        return False
    return "/post/" in (path.path or "")


async def pawchive_download(
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
    """Unduh post Pawchive (arsip Patreon).

    Alur:
      1. Ambil metadata + daftar media lewat extractor (API).
      2. Pilih video terbaik (1080p > resolusi tertinggi > lainnya).
      3. Unduh langsung dari file.pawchive.pw ke TMP_DIR (batas 2GB).
      4. Kalau tidak ada video (post gambar): kirim album gambar.
      5. fmt_key == "mp3" -> ekstrak audio via ffmpeg.
    """
    del format_id, has_audio, known_size

    ext = _import_extractor()
    work_dir = os.path.join(TMP_DIR, f"paw_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    final_path = None
    try:
        if not metadata_ready:
            await ext._safe_edit_status(bot, chat_id, status_msg_id, "<b>Scraping pawchive.pw...</b>")

        post = await asyncio.to_thread(ext.scrape_post, raw_url)
        title = sanitize_filename(post.get("title") or "Pawchive", 100)

        videos = post.get("videos") or []
        images = post.get("images") or []

        if fmt_key != "mp3":
            # Persiapkan list media gabungan
            final_videos = ext.resolve_videos(videos) if videos else []
            all_media = []
            for v in final_videos:
                all_media.append({"url": v["url"], "name": v.get("name"), "type": "video"})
            for img in images:
                all_media.append({"url": img["url"], "name": img.get("name"), "type": "photo"})

            if len(all_media) > 1:
                # Jika ada lebih dari 1 media, kirim sebagai album campuran
                album_tag = uuid.uuid4().hex[:8]
                return await ext.download_album(
                    all_media, title, bot, chat_id, status_msg_id, TMP_DIR, album_tag,
                    item_label="media"
                )

        if not videos and images:
            # Fallback jika cuma ada 1 gambar
            album_tag = uuid.uuid4().hex[:8]
            return await ext.download_album(
                images, title, bot, chat_id, status_msg_id, TMP_DIR, album_tag
            )

        if not videos:
            raise RuntimeError("No media to download in this post")

        item = ext.pick_best_video(videos)
        url = item["url"]
        size = int(item.get("size") or 0)
        if size > MAX_TG_SIZE:
            raise FileSizeLimitExceeded(
                f"Video exceeds 2GB limit ({size / 1024 / 1024 / 1024:.2f} GB). Download canceled."
            )

        if fmt_key == "mp3":
            raw_path = os.path.join(work_dir, "source.mp4")
            await ext.download_to_file(url, raw_path, bot, chat_id, status_msg_id, title)
            final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_pawchive.mp3")
            await asyncio.to_thread(ext.extract_audio, raw_path, final_path)
            result = {"path": final_path, "title": title}
            cover = await asyncio.to_thread(
                ext.download_thumb,
                post.get("thumbnail"),
                os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_paw_thumb.jpg"),
            )
            if cover:
                result["thumb"] = cover
                result["artist"] = title
            return result

        ext_name = ext.extension_for(item.get("name"), ".mp4")
        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_pawchive{ext_name}")
        await ext.download_to_file(url, final_path, bot, chat_id, status_msg_id, title)

        log.info(
            "Pawchive sukses | title=%r quality=%s size=%.2fMB",
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
