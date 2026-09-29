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

    def scrape_pixeldrain_variants(html_text: str) -> list:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        out_vars = []
        for row in soup.find_all(class_="nk-download-row"):
            name_div = row.find(class_="nk-download-name")
            name = name_div.get_text(strip=True).lower() if name_div else ""
            h = 0
            if "1080" in name: h = 1080
            elif "720" in name: h = 720
            elif "480" in name: h = 480
            elif "360" in name: h = 360
            
            for a in row.find_all("a", href=True):
                if "pixeldrain" in a.get_text(strip=True).lower():
                    ouo_url = a["href"]
                    # parse size if possible or leave 0
                    out_vars.append({
                        "height": h or 480,
                        "bandwidth": h * 1000 if h else 480000,
                        "url": ouo_url,
                        "format_id": str(h or 480),
                        "type": "pixeldrain_ouo"
                    })
        return out_vars

    def _resolve_pixeldrain(ouo_url: str) -> tuple[str, int]:
        bypassed = ext.bypass_ouo(ouo_url)
        if not bypassed or "pixeldrain.com/u/" not in bypassed:
            return "", 0
        pid = bypassed.split("/u/")[-1].split("?")[0].split("/")[0]
        api_url = f"https://pixeldrain.com/api/file/{pid}"
        sz = ext._probe_direct_size(api_url, {"User-Agent": ext.UA})
        return api_url, sz

    # Hanya host embed yang benar-benar jalur HLS mandiri (streampoi) yang
    # errornya layak disurface ke user. Embed lain (playmogo/DoodStream butuh
    # Turnstile, widget discord, dsb.) selalu gagal dengan pesan "packer tidak
    # ditemukan" yang menyesatkan -> jangan dipakai sebagai pesan akhir.
    primary_err = None
    primary_host = None
    last_err = None
    
    # Coba HLS embeds (streampoi) dulu
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
            log.debug("Nekopoi embed gagal | embed=%s err=%r", emb, e)
            last_err = e
            if is_primary and primary_err is None:
                primary_err, primary_host = e, emb_host
            continue

    # Jika HLS gagal, fallback ke download link (Pixeldrain)
    pd_variants = scrape_pixeldrain_variants(post.get("raw_html") or "")
    if pd_variants:
        log.info("Nekopoi HLS tidak tersedia, fallback ke download link Pixeldrain")
        variants = _dedup_by_height(pd_variants)
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
            "master": "pixeldrain_fallback",
            "variants": variants,
            "res_list": res_list,
        }
        cache_probe(raw_url, probe)
        return probe

    raise primary_err or last_err or RuntimeError("Gagal mengambil stream Nekopoi dari semua embed & download link")


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

        if chosen.get("type") == "pixeldrain_ouo":
            bypassed = ext.bypass_ouo(chosen["url"])
            if not bypassed or "pixeldrain.com/u/" not in bypassed:
                raise RuntimeError("Gagal mem-bypass link Pixeldrain Nekopoi")
            pid = bypassed.split("/u/")[-1].split("?")[0].split("/")[0]
            direct_url = f"https://pixeldrain.com/api/file/{pid}"
            headers = {"User-Agent": ext.UA}

            if fmt_key == "mp3":
                tmp_mp4 = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_neko_tmp.mp4")
                final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_nekopoi.mp3")
                try:
                    await ext.download_direct_mp4(direct_url, tmp_mp4, bot, chat_id, status_msg_id, title, label, headers)
                    await asyncio.to_thread(ext._run_ffmpeg, [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i", tmp_mp4, "-vn", "-acodec", "libmp3lame", "-q:a", "2", final_path,
                    ])
                finally:
                    if os.path.exists(tmp_mp4):
                        try:
                            os.remove(tmp_mp4)
                        except OSError:
                            pass
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
            await ext.download_direct_mp4(direct_url, final_path, bot, chat_id, status_msg_id, title, label, headers)
            size = os.path.getsize(final_path)
            if size > MAX_TG_SIZE:
                raise FileSizeLimitExceeded("Video exceeds 2GB limit. Download canceled.")
            log.info("Nekopoi Pixeldrain sukses | title=%r label=%s size=%.2fMB", title, label, size / 1024 / 1024)
            return {"path": final_path, "title": title}

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
