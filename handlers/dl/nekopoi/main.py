"""Downloader Nekopoi (nekopoi.care) — full mandiri, TANPA yt-dlp.

FLOW DOWNLOADER
---------------
1. `is_nekopoi_url(url)`: host nekopoi.* + path post (slug) -> dipakai router
   (`supports_resolution_picker`, `service.download_non_tiktok`, label platform).

2. `probe_nekopoi(raw_url)` (sinkron, dipanggil via `asyncio.to_thread`):
   a. `extractor.scrape_post` -> judul, thumbnail, daftar iframe embed.
   b. embed prio streampoi -> `extractor.probe_stream` (unpack Dean Edwards)
      -> master.m3u8.
   c. `extractor.list_resolutions` -> varian resolusi tertinggi dulu,
      **di-dedup per tinggi** (2 varian 720p -> ambil bandwidth terbesar)
      karena picker Telegram & DL_CACHE router di-key oleh tinggi (int).
   d. Hasil di-cache di `_VARIANT_CACHE[url]` (TTL 5 menit) supaya callback
      resolusi (yang hanya menerima `format_id` = tinggi) bisa memakai URL
      varian + token yang sama dengan yang ditampilkan ke user.
   e. `format_id` = tinggi (str) karena `res_map`/callback hanya membawa satu
      nilai string; URL varian penuh diambil dari cache, bukan dari format_id.

3. `nekopoi_download(...)`:
   - fmt video: ambil varian sesuai `format_id` (fallback: varian terbaik)
     -> `extractor.download_variant` (segmen .ts paralel -> concat -> ffmpeg
     -c copy) -> {"path", "title"}.
   - fmt mp3: varian terbaik -> `extractor.download_audio` (libmp3lame).

4. Cache miss (mis. cache router sudah expired / worker retry setelah flood):
   `nekopoi_download` re-probe halaman post untuk token segar -> unduh.

5. Batas MAX_TG_SIZE dicek setelah file jadi; `FileSizeLimitExceeded` dipakai
   supaya pesan errornya konsisten dengan modul downloader lain.

6. Tidak ada fallback ke yt-dlp di manapun. Kalau master/variant 404 (file
   sudah dihapus di CDN) -> RuntimeError dengan pesan yang jelas.
"""
import os
import time
import uuid
import shutil
import logging
import asyncio
from urllib.parse import urlparse, urlsplit

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded

from .constants import EMBED_HOST_STREAMPOI

log = logging.getLogger(__name__)

# {post_url: {"ts": float, "probe": {...}}} — TTL 5 menit.
# Dipakai untuk mengikat pilihan resolusi user (format_id=tinggi) ke URL varian
# beserta token streamruby yang sudah tervalidasi, tanpa request berulang.
_VARIANT_CACHE: dict = {}
_CACHE_TTL = 300.0


def _import_extractor():
    """Import extractor secara lazy supaya package tetap bisa di-import walau
    dependensi (curl_cffi) belum tersedia saat import awal."""
    from . import extractor
    return extractor


def is_nekopoi_url(url: str) -> bool:
    """Cocok untuk halaman post Nekopoi, mis.
    https://nekopoi.care/koutetsu-no-majo-annerose-episode-2-subtitle-indonesia/"""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("nekopoi.care", "nekopoi.best", "nekopoi.pw", "nekopoi.win"):
        return False
    return bool((parsed.path or "").strip("/"))


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


def _dedup_by_height(variants: list) -> list:
    """Picker Telegram hanya mengenal tinggi (int) -> simpan varian dengan
    bandwidth tertinggi per tinggi supaya tidak ada tombol duplikat."""
    best: dict = {}
    for v in variants:
        height = int(v.get("height") or 0)
        if height <= 0:
            continue
        cur = best.get(height)
        if cur is None or int(v.get("bandwidth") or 0) > int(cur.get("bandwidth") or 0):
            best[height] = v
    out = list(best.values())
    out.sort(key=lambda v: int(v.get("height") or 0), reverse=True)
    for v in out:
        v["format_id"] = str(int(v.get("height") or 0))
    return out


def probe_nekopoi(raw_url: str) -> dict:
    """-> {title, thumbnail, variants:[...], res_list:[...]}

    `variants` = varian lengkap (dengan url + token); `res_list` = format yang
    dipahami `router._show_resolution_picker` (height/format_id/has_audio/
    filesize/total_size).
    """
    ext = _import_extractor()
    post = ext.scrape_post(raw_url)
    title = sanitize_filename(post.get("title") or "Nekopoi", 100)
    embeds = post.get("embeds") or []
    if not embeds:
        raise RuntimeError("Tidak ada stream embed di halaman Nekopoi")

    # Hanya host embed yang benar-benar jalur HLS mandiri (streampoi) yang
    # errornya layak disurface ke user. Embed lain (playmogo/DoodStream butuh
    # Turnstile, widget discord, dsb.) selalu gagal dengan pesan "packer tidak
    # ditemukan" yang menyesatkan -> jangan dipakai sebagai pesan akhir.
    primary_err = None
    primary_host = None
    last_err = None
    for emb in embeds:
        emb_host = (urlsplit(emb).hostname or "").lower()
        is_primary = any(emb_host == d or emb_host.endswith("." + d) for d in EMBED_HOST_STREAMPOI)
        try:
            master = ext.probe_stream(emb, referer=raw_url)
            variants = _dedup_by_height(ext.list_resolutions(master))
            if not variants:
                raise RuntimeError("Tidak ada varian resolusi di playlist Nekopoi")
            res_list = [
                {
                    "height": int(v["height"]),
                    "format_id": v["format_id"],
                    "has_audio": True,
                    "filesize": 0,
                    "total_size": 0,
                }
                for v in variants
            ]
            probe = {
                "title": title,
                "thumbnail": post.get("thumbnail") or "",
                "master": master,
                "variants": variants,
                "res_list": res_list,
            }
            cache_probe(raw_url, probe)
            return probe
        except RuntimeError as e:
            log.warning("Nekopoi embed gagal | embed=%s err=%r", emb, e)
            last_err = e
            if is_primary and primary_err is None:
                primary_err, primary_host = e, emb_host
            continue
    raise primary_err or last_err or RuntimeError("Gagal mengambil stream Nekopoi dari semua embed")


def _pick_variant(variants: list, format_id: str | None) -> dict:
    if format_id:
        for v in variants:
            if str(v.get("format_id")) == str(format_id):
                return v
        for v in variants:
            if str(int(v.get("height") or 0)) == str(format_id):
                return v
    return max(variants, key=lambda v: (int(v.get("height") or 0), int(v.get("bandwidth") or 0)))


async def nekopoi_download(
    raw_url,
    fmt_key,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
    engine: str | None = None,
):
    """Unduh post Nekopoi (HLS streamruby) mandiri tanpa yt-dlp."""
    del has_audio, known_size, engine
    ext = _import_extractor()
    work_dir = os.path.join(TMP_DIR, f"neko_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    final_path = None
    try:
        probe = peek_probe(raw_url)
        if not probe:
            if not metadata_ready:
                await ext.safe_edit_status(bot, chat_id, status_msg_id, "<b>Scraping Nekopoi...</b>")
            probe = await asyncio.to_thread(probe_nekopoi, raw_url)

        variants = probe.get("variants") or []
        if not variants:
            raise RuntimeError("Tidak ada varian stream di halaman Nekopoi")
        chosen = _pick_variant(variants, format_id)
        title = sanitize_filename(probe.get("title") or "Nekopoi", 100)
        label = f"{int(chosen.get('height') or 0)}p"
        log.info(
            "Nekopoi download start | title=%r label=%s fmt=%s format_id=%s variants=%s",
            title, label, fmt_key, format_id, [v.get("height") for v in variants],
        )

        if fmt_key == "mp3":
            final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_nekopoi.mp3")
            await ext.download_audio(
                chosen["url"], final_path, title, label,
                bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            )
            size = os.path.getsize(final_path)
            if size > MAX_TG_SIZE:
                raise FileSizeLimitExceeded("Audio exceeds 2GB limit. Download canceled.")
            result = {"path": final_path, "title": title}
            cover = await asyncio.to_thread(
                ext.download_thumb,
                probe.get("thumbnail"),
                os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_neko_thumb.jpg"),
            )
            if cover:
                result["thumb"] = cover
                result["artist"] = title
            return result

        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_nekopoi.mp4")
        await ext.download_variant(chosen["url"], final_path, bot, chat_id, status_msg_id, title, label)
        size = os.path.getsize(final_path)
        if size > MAX_TG_SIZE:
            raise FileSizeLimitExceeded("Video exceeds 2GB limit. Download canceled.")
        log.info("Nekopoi sukses | title=%r label=%s size=%.2fMB", title, label, size / 1024 / 1024)
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
