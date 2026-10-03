"""Downloader Nekopoi (nekopoi.care)

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

from .constants import EMBED_HOST_STREAMPOI, EMBED_HOST_DOOD

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


def _list_download_hosts(html_text: str) -> list[str]:
    """Daftar label host unduh di `.nk-download-row` yang BUKAN Pixeldrain / Mp4Upload.

    Dipakai hanya untuk pesan error yang jujur ketika sebuah post tidak punya
    jalur yang didukung (tidak ada streampoi, tidak ada Pixeldrain, tidak ada
    Mp4Upload).
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return []
    try:
        soup = BeautifulSoup(html_text or "", "html.parser")
    except Exception:
        return []
    seen: list[str] = []
    for row in soup.find_all(class_="nk-download-row"):
        for a in row.find_all("a", href=True):
            label = a.get_text(strip=True)
            low = label.lower()
            if not label or "pixel" in low or "mp4upload" in low:
                continue
            if label not in seen:
                seen.append(label)
    return seen


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

            # Tiap baris punya DUA link Pixeldrain: shortener langsung
            # (`linkpoi.me/<id>`, teks "PixelDrain") dan versi via ouo
            # (`ouo.io/<id>`, teks "Pixel[ouo]"). Filter "pixeldrain" TIDAK
            # menangkap yang ouo (teksnya "Pixel[ouo]"), jadi pakai "pixel"
            # supaya keduanya masuk, lalu utamakan host ouo.io yang terbukti
            # tembus (linkpoi sering 500 / butuh JS).
            hrefs = [a["href"] for a in row.find_all("a", href=True)
                     if "pixel" in a.get_text(strip=True).lower()]

            # Juga kumpulkan link Mp4Upload (teks label "Mp4Upload" -> "mp4")
            # sebagai cadangan per tinggi ketika Pixeldrain gagal di-bypass.
            mp4_hrefs = [a["href"] for a in row.find_all("a", href=True)
                         if "mp4upload" in a.get_text(strip=True).lower()]

            if not hrefs and not mp4_hrefs:
                continue

            def _is_ouo(u: str) -> bool:
                host = (urlsplit(u).hostname or "").lower()
                return host.endswith("ouo.io") or host.endswith("ouo.press")

            chosen_pd = ""
            if hrefs:
                chosen_pd = next((u for u in hrefs if _is_ouo(u)), hrefs[0])
            chosen_mp4 = ""
            if mp4_hrefs:
                chosen_mp4 = next((u for u in mp4_hrefs if _is_ouo(u)), mp4_hrefs[0])

            # Baris tanpa Pixeldrain tapi ada Mp4Upload -> Mp4Upload jadi
            # varian utama tinggi itu (post tetap terunduh).
            if chosen_pd:
                out_vars.append({
                    "height": h or 480,
                    "bandwidth": h * 1000 if h else 480000,
                    "url": chosen_pd,
                    "alt_url": chosen_mp4,
                    "alt_type": "mp4upload_ouo" if chosen_mp4 else "",
                    "format_id": str(h or 480),
                    "type": "pixeldrain_ouo",
                })
            else:
                out_vars.append({
                    "height": h or 480,
                    "bandwidth": h * 1000 if h else 480000,
                    "url": chosen_mp4,
                    "format_id": str(h or 480),
                    "type": "mp4upload_ouo",
                })
        return out_vars

    def _resolve_pixeldrain(ouo_url: str) -> tuple[str, int]:
        bypassed = ext.resolve_shortener(ouo_url)
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
        if not is_primary:
            continue
        log.info("Nekopoi try HLS | embed=%s", emb)
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
            log.info("Nekopoi HLS ready | master=%s", master)
            return probe
        except RuntimeError as e:
            log.info("Nekopoi HLS failed | embed=%s err=%s", emb_host, e)
            last_err = e
            if is_primary and primary_err is None:
                primary_err, primary_host = e, emb_host
            continue

    # Fallback 2: kumpulkan varian dari DoodStream (direct MP4) + Pixeldrain (ouo),
    # lalu GABUNG per tinggi.
    #
    # Pixeldrain didahulukan PER TINGGI: delay bypass ouo (~16-20s) jauh lebih
    # murah daripada throttle CDN DoodStream (~81 KB/s setelah burst awal).
    # DoodStream hanya mengisi tinggi yang tidak tersedia di Pixeldrain,
    # sehingga semua resolusi tetap tersedia, termasuk post tanpa Pixeldrain.
    dood_variants: list = []
    for emb in embeds:
        emb_host = (urlsplit(emb).hostname or "").lower()
        is_dood = any(emb_host == d or emb_host.endswith("." + d) for d in EMBED_HOST_DOOD)
        if not is_dood:
            continue
        try:
            info = ext.probe_doodstream(emb, referer=raw_url)
        except Exception as e:
            log.debug("DoodStream embed gagal | embed=%s err=%r", emb, e)
            continue
        if info:
            dood_variants.append(info)

    pd_variants = scrape_pixeldrain_variants(post.get("raw_html") or "")

    merged: list = []
    seen_heights: set = set()
    # Pixeldrain dulu (62-100 MB/s), baru DoodStream untuk tinggi yang absen.
    for v in pd_variants:
        h = int(v.get("height") or 0)
        if h and h not in seen_heights:
            seen_heights.add(h)
            merged.append(v)
    for v in dood_variants:
        h = int(v.get("height") or 0)
        if h and h not in seen_heights:
            seen_heights.add(h)
            merged.append(v)

    if merged:
        variants = _dedup_by_height(merged)
        res_list = [
            {
                "height": int(v["height"]),
                "format_id": v["format_id"],
                "has_audio": True,
                "filesize": 0,
                "total_size": int(v.get("size") or 0),
            }
            for v in variants
        ]
        has_dood = any(v.get("type") == "doodstream_direct" for v in variants)
        has_pd = any(v.get("type") == "pixeldrain_ouo" for v in variants)
        if has_dood and has_pd:
            master_tag = "pixeldrain+doodstream_fallback"
            log.info("Nekopoi HLS tidak tersedia, fallback ke Pixeldrain + DoodStream")
        elif has_pd:
            master_tag = "pixeldrain_fallback"
            log.info("Nekopoi HLS tidak tersedia, fallback ke download link Pixeldrain")
        else:
            master_tag = "doodstream_fallback"
            log.info("Nekopoi HLS tidak tersedia, fallback ke embed DoodStream")
        probe = {
            "title": title,
            "thumbnail": post.get("thumbnail") or "",
            "master": master_tag,
            "variants": variants,
            "res_list": res_list,
        }
        cache_probe(raw_url, probe)
        return probe

    # Tidak ada streampoi (primary) dan tidak ada link Pixeldrain.
    # JANGAN angkat error embed non-utama (playmogo/videobin/discord) —
    # teksnya "Skrip packer tidak ditemukan di embed" yang menyesatkan dan
    # bikin seolah parser HLS-nya rusak, padahal parser baik-baik saja: memang
    # tidak ada satu pun jalur unduh yang didukung di post ini. Sebutkan host
    # apa saja yang tersedia supaya user tahu ini post yang memang tidak
    # didukung (host-nya pun sering sudah mati semua).
    if primary_err is None:
        hosts = _list_download_hosts(post.get("raw_html") or "")
        names = ", ".join(hosts) if hosts else "tidak terdeteksi"
        raise RuntimeError(
            "Post Nekopoi ini tidak punya jalur unduh yang didukung: "
            "tidak ada HLS (streampoi), DoodStream (playmogo), Pixeldrain, ataupun Mp4Upload. "
            f"Host yang tersedia di post ({names}) belum didukung scraper."
        )

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
    """Unduh post Nekopoi (HLS streamruby)."""
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
            "Nekopoi download start | title=%r label=%s fmt=%s format_id=%s variants=%s src=%s",
            title, label, fmt_key, format_id, [v.get("height") for v in variants], chosen.get("type"),
        )

        if chosen.get("type") == "doodstream_direct":
            # Direct MP4 dari CDN DoodStream. Referer WAJIB = halaman embed:
            # CDN menolak permintaan tanpa referer playmogo.
            direct_url = chosen["url"]
            headers = {
                "User-Agent": ext.UA,
                "Referer": chosen.get("referer") or "",
            }

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
            log.info("Nekopoi DoodStream sukses | title=%r label=%s size=%.2fMB", title, label, size / 1024 / 1024)
            return {"path": final_path, "title": title}

        if chosen.get("type") in ("pixeldrain_ouo", "mp4upload_ouo"):
            # Urutan sumber per tinggi:
            #   1. Pixeldrain (kalau baris punya) — CDN 62-100 MB/s.
            #   2. Mp4Upload (kolom `alt_url`, atau varian utama bila baris tak
            #      punya Pixeldrain) — cadangan saat bypass ouo Pixeldrain gagal
            #      atau file Pixeldrain sudah mati. CDN mp4upload stabil & cepat.
            # Semua lewat shortener ouo.io, jadi bypass-nya blocking -> thread.
            srcs: list[tuple[str, str]] = []
            if chosen.get("type") == "pixeldrain_ouo" and chosen.get("url"):
                srcs.append(("pixeldrain", chosen["url"]))
            if chosen.get("alt_url"):
                srcs.append(("mp4upload", chosen["alt_url"]))
            if chosen.get("type") == "mp4upload_ouo" and chosen.get("url"):
                srcs.append(("mp4upload", chosen["url"]))

            if not srcs:
                raise RuntimeError("Tidak ada sumber unduh yang tersedia untuk resolusi ini")

            direct_url = ""
            src_headers: dict = {"User-Agent": ext.UA}
            src_kind = ""
            last_src_err: Exception | None = None
            for kind, ouo_url in srcs:
                label_kind = "Pixeldrain" if kind == "pixeldrain" else "Mp4Upload"
                log.info("Nekopoi try %s | url=%s", label_kind, ouo_url)
                try:
                    bypassed = await asyncio.to_thread(ext.resolve_shortener, ouo_url)
                    if kind == "pixeldrain":
                        if bypassed and "pixeldrain.com/u/" in bypassed:
                            pid = bypassed.split("/u/")[-1].split("?")[0].split("/")[0]
                            direct_url = f"https://pixeldrain.com/api/file/{pid}"
                            src_headers = {"User-Agent": ext.UA}
                            src_kind = "Pixeldrain"
                            log.info("Nekopoi Pixeldrain ready | pid=%s url=%s", pid, direct_url)
                            break
                        log.info("Nekopoi Pixeldrain failed (%s), fallback ke Mp4Upload", bypassed)
                    else:
                        if bypassed and "mp4upload.com" in bypassed:
                            cdn, _sz = await asyncio.to_thread(ext.mp4upload_to_direct_cdn, bypassed)
                            if cdn:
                                direct_url = cdn
                                src_headers = {"User-Agent": ext.UA, "Referer": "https://www.mp4upload.com/"}
                                src_kind = "Mp4Upload"
                                log.info("Nekopoi Mp4Upload ready | cdn=%s size=%s", cdn, _sz)
                                break
                        log.info("Nekopoi Mp4Upload failed | url=%s", ouo_url)
                except Exception as e:
                    last_src_err = e
                    log.info("Nekopoi try %s failed | err=%r", label_kind, e)

            if not direct_url:
                if last_src_err and chosen.get("type") == "mp4upload_ouo":
                    raise RuntimeError("Gagal mem-bypass link Mp4Upload Nekopoi") from last_src_err
                raise RuntimeError("Gagal mem-bypass link Pixeldrain Nekopoi")

            if src_kind == "Mp4Upload":
                log.info("Nekopoi Mp4Upload sukses | title=%r label=%s", title, label)

            if fmt_key == "mp3":
                tmp_mp4 = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_neko_tmp.mp4")
                final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_nekopoi.mp3")
                try:
                    await ext.download_direct_mp4(direct_url, tmp_mp4, bot, chat_id, status_msg_id, title, label, src_headers)
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
            await ext.download_direct_mp4(direct_url, final_path, bot, chat_id, status_msg_id, title, label, src_headers)
            size = os.path.getsize(final_path)
            if size > MAX_TG_SIZE:
                raise FileSizeLimitExceeded("Video exceeds 2GB limit. Download canceled.")
            log.info("Nekopoi %s sukses | title=%r label=%s size=%.2fMB", src_kind, title, label, size / 1024 / 1024)
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
