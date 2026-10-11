"""Downloader Anichin (anichin.moe) — Donghua Sub Indo.

FLOW DOWNLOADER
---------------
1. `is_anichin_url(url)`: host `anichin.moe` + path episode
   (`/<slug>-episode-<N>-subtitle-indonesia/`).
2. `probe_anichin(url)`: scrape halaman episode -> daftar mirror resolusi
   (dari `div.soraurlx`) -> `res_list` untuk resolution picker. Di-cache
   `_PROBE_CACHE` (TTL 5 menit) supaya callback resolusi memakai link yang sama.
3. `anichin_download(...)`:
   a. Ambil mirror sesuai `format_id` (label resolusi, mis. "720p").
   b. Resolve chain: shortener -> `mirrored.to/multilinks` ->
      `mirrored.to/files/<uid>/..._links` -> `/mirstats.php` -> POST `/out_url`
      -> link file host asli (GoogleDrive/Buzzheavier/...).
   c. Unduh:
      - GoogleDrive -> `_download_gdrive` (endpoint `drive.usercontent.google.com`).
      - Buzzheavier -> CDN langsung (`buzzheavier.com/<id>/download`).
      - host lain -> streaming `curl_cffi` biasa dengan Referer host.
   d. `fmt_key == "mp3"` -> ekstrak audio ffmpeg `libmp3lame -q:a 2`.
4. Semua langkah MURNI HTTP (curl_cffi). TIDAK memakai yt-dlp.
"""
import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from urllib.parse import urlparse

from handlers.dl.constants import MAX_TG_SIZE, TMP_DIR
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename

from .constants import ANICHIN_HOSTS, UA
from . import extractor

log = logging.getLogger(__name__)

_PROBE_CACHE: dict = {}
_CACHE_TTL = 300.0


def is_anichin_url(url: str) -> bool:
    """Halaman episode Anichin: host anichin.moe + slug episode.

    Halaman series (`/<slug>/`) juga diterima (diunduh lewat episode pertama).
    """
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in tuple(h.lstrip("www.") for h in ANICHIN_HOSTS):
        return False
    path = (parsed.path or "").strip("/")
    if not path:
        return False
    # Buang halaman non-video.
    if path.split("/")[0] in ("anime", "ongoing", "completed", "upcoming-donghua", "drop",
                              "genres", "az-lists", "schedule", "bookmark", "blog",
                              "privacy-policy-2", "riwayat-menonton", "anichin-vip", "anichin-cafe"):
        return False
    return True


def _prune_cache() -> None:
    now = time.time()
    for key in [k for k, v in list(_PROBE_CACHE.items()) if now - v.get("ts", 0) > _CACHE_TTL]:
        _PROBE_CACHE.pop(key, None)


def cache_probe(url: str, probe: dict) -> None:
    _prune_cache()
    _PROBE_CACHE[(url or "").strip()] = {"ts": time.time(), "probe": probe}


def peek_probe(url: str) -> dict | None:
    item = _PROBE_CACHE.get((url or "").strip())
    if not item:
        return None
    if time.time() - item.get("ts", 0) > _CACHE_TTL:
        _PROBE_CACHE.pop((url or "").strip(), None)
        return None
    return item.get("probe")


def _scrape_series_latest_episode(url: str) -> dict | None:
    """Halaman series -> episode TERBARU (`div.eplister ul li` pertama).

    Dipakai `probe_anichin` supaya URL series tidak berakhir
    "No valid resolutions available". -> {episode_url, title}
    """
    import re as _re
    from bs4 import BeautifulSoup

    code, raw, _ = extractor._get(url)
    if code != 200 or not raw:
        return None
    soup = BeautifulSoup(raw, "html.parser")
    ul = soup.select_one("div.eplister ul")
    if not ul:
        return None
    li = ul.select_one("li")
    if not li:
        return None
    a = li.select_one("a[href]")
    if not a:
        return None
    href = a["href"].strip()
    if href.startswith("/"):
        href = "https://anichin.moe" + href
    title = li.select_one("div.epl-title")
    return {
        "episode_url": href,
        "title": _re.sub(r"\s+", " ", title.get_text(" ", strip=True)) if title else "",
    }


def probe_anichin(raw_url: str) -> dict:
    """-> {title, thumbnail, mirrors, res_list}"""
    url = (raw_url or "").strip()
    cached = peek_probe(url)
    if cached:
        return cached

    post = extractor.scrape_post(url)
    mirrors = post.get("mirrors") or []

    # Kalau user menempel URL SERIES (`/<slug>/`), halaman itu cuma daftar
    # episode -> tidak ada mirror. Ambil episode TERBARU lalu probe itu,
    # supaya "No valid resolutions available" tidak muncul untuk URL series.
    if not mirrors:
        try:
            detail = _scrape_series_latest_episode(url)
        except Exception as e:
            log.debug("Anichin series fallback gagal | url=%s err=%r", url, e)
            detail = None
        if detail and detail.get("episode_url"):
            ep_url = detail["episode_url"]
            log.info("Anichin series URL -> latest episode | from=%s to=%s", url, ep_url)
            ep_post = extractor.scrape_post(ep_url)
            mirrors = ep_post.get("mirrors") or []
            if mirrors:
                post = ep_post
                url = ep_url

    if not mirrors:
        raise RuntimeError(
            "Tidak ada link unduh (mirror) di halaman Anichin ini. "
            "Kalau ini halaman series, buka salah satu episode-nya."
        )

    res_list = []
    seen = set()
    for m in mirrors:
        label = (m.get("label") or "").strip()
        low = label.lower()

        # Label "Multi": shortener-nya langsung menunjuk `mirrored.to/multilinks/<hash>`
        # yang berisi BEBERAPA resolusi (1080p/720p/480p/360p). Expand jadi
        # resolusi nyata supaya picker terisi; kalau gagal, fallback ke label asli.
        if low == "multi":
            try:
                expanded = extractor.list_resolutions(m.get("url") or "", referer=url)
            except Exception as e:
                log.debug("Anichin expand Multi gagal | url=%s err=%r", m.get("url"), e)
                expanded = []
            if expanded:
                for r in expanded:
                    key = (r.get("format_id") or "").lower()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    res_list.append({
                        "height": r.get("height") or 480,
                        "format_id": r.get("format_id") or r.get("label") or "480p",
                        "label": r.get("label") or r.get("format_id") or "480p",
                        "has_audio": True,
                        "filesize": 0,
                        "total_size": 0,
                        "mirror_url": m.get("url"),
                    })
                continue

        # "VIP" bukan resolusi (link Telegram promosi).
        if low in ("vip", ""):
            continue
        # "4K" tidak punya digit tinggi -> map manual.
        if low in ("4k", "2160p"):
            height = 2160
        else:
            hm = re.search(r"(\d{3,4})", label)
            height = int(hm.group(1)) if hm else 0
        if not height:
            continue
        key = low
        if key in seen:
            continue
        seen.add(key)
        res_list.append({
            "height": height,
            "format_id": label,
            "label": label,
            "has_audio": True,
            "filesize": 0,
            "total_size": 0,
            "mirror_url": m.get("url"),
        })

    res_list.sort(key=lambda x: x.get("height") or 0, reverse=True)
    probe = {
        "title": post.get("title") or "Anichin Video",
        "thumbnail": post.get("thumbnail") or "",
        "mirrors": mirrors,
        "servers": post.get("servers") or [],
        "res_list": res_list,
    }
    cache_probe(url, probe)
    # Kalau URL asli adalah halaman series, simpan juga di key URL series supaya
    # probe kedua tidak resolve ulang (download worker memakai `raw_url` asli).
    if (raw_url or "").strip() != url:
        cache_probe((raw_url or "").strip(), probe)
    return probe


def _gdrive_id(url: str) -> str:
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def _download_gdrive(gid: str, out_path: str) -> int:
    """Unduh dari Google Drive (endpoint `drive.usercontent.google.com`)."""
    import curl_cffi.requests as curl_requests

    h = {"User-Agent": UA}
    url = f"https://drive.usercontent.google.com/download?id={gid}&export=download"
    r = curl_requests.get(url, headers=h, impersonate="chrome", timeout=120, stream=True)
    ct = (r.headers.get("content-type") or "").lower()

    if "text/html" in ct:
        text = r.content.decode("utf-8", "ignore")
        if "quota" in text.lower():
            raise RuntimeError("Kuota Google Drive terlampaui.")
        m = re.search(r'name="confirm"\s+value="([^"]+)"', text)
        if not m:
            m = re.search(r'href="(/uc\?export=download[^"]*confirm=[^"]+)"', text)
            if m:
                r = curl_requests.get(
                    "https://drive.google.com" + m.group(1).replace("&amp;", "&"),
                    headers=h, impersonate="chrome", timeout=120, stream=True,
                )
            else:
                raise RuntimeError("Google Drive butuh konfirmasi yang tidak dikenali.")
        else:
            r = curl_requests.get(
                "https://drive.usercontent.google.com/download",
                params={"id": gid, "export": "download", "confirm": m.group(1)},
                headers=h, impersonate="chrome", timeout=120, stream=True,
            )
            ct = (r.headers.get("content-type") or "").lower()

    if r.status_code != 200 or "html" in ct:
        raise RuntimeError(f"Google Drive tidak mengembalikan video ({r.status_code}, {ct or 'no ct'}).")

    total = 0
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                total += len(chunk)
                if total > MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("File melebihi batas 2GB.")
    return total


def _download_stream(url: str, out_path: str, referer: str = "") -> int:
    """Streaming HTTP biasa (host selain Google Drive)."""
    import curl_cffi.requests as curl_requests

    with_headers = {"User-Agent": UA}
    if referer:
        with_headers["Referer"] = referer
    r = curl_requests.get(url, headers=with_headers, impersonate="chrome", timeout=120, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"Host file membalas HTTP {r.status_code}.")
    ct = (r.headers.get("content-type") or "").lower()
    if "html" in ct:
        raise RuntimeError(f"Host file mengembalikan HTML, bukan video ({ct}).")
    total = 0
    try:
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=512 * 1024):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
                    if total > MAX_TG_SIZE:
                        raise FileSizeLimitExceeded("File melebihi batas 2GB.")
    finally:
        try:
            r.close()
        except Exception:
            pass
    return total


async def anichin_download(
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
    """Unduh episode Anichin. Lihat FLOW DOWNLOADER di docstring modul."""
    del has_audio, known_size, engine
    url = (raw_url or "").strip()

    probe = peek_probe(url)
    if not probe:
        probe = await asyncio.to_thread(probe_anichin, url)
        cache_probe(url, probe)

    title = probe.get("title") or "Anichin Video"
    mirrors = probe.get("mirrors") or []
    if not mirrors:
        raise RuntimeError("Tidak ada link unduh di halaman ini.")

    want = (format_id or "").strip().lower()
    target = None
    for m in mirrors:
        if (m.get("label") or "").strip().lower() == want:
            target = m
            break
    if not target:
        target = mirrors[0]

    if not metadata_ready and status_msg_id:
        from handlers.dl.progress import edit_status
        await edit_status(
            bot, chat_id, status_msg_id,
            f"<b>Resolving {target.get('label') or 'video'}...</b>", label="Anichin",
        )

    candidates = await asyncio.to_thread(
        extractor.resolve_all_candidates, target.get("url"), target.get("label") or "", url
    )
    if not candidates:
        raise RuntimeError("Gagal me-resolve link unduh (shortener/mirror mati).")

    log.info(
        "Anichin candidates | title=%r label=%s hosts=%s",
        title, target.get("label"), [c.get("host") for c in candidates],
    )

    work_dir = os.path.join(TMP_DIR, f"ac_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    temp_video = os.path.join(work_dir, f"{uuid.uuid4().hex[:8]}.mp4")

    try:
        last_err = None
        for cand in candidates:
            file_url = cand["url"]
            host = cand.get("host") or ""
            try:
                if "drive.google" in file_url:
                    gid = _gdrive_id(file_url)
                    if not gid:
                        raise RuntimeError("Google Drive id tidak ditemukan.")
                    await asyncio.to_thread(_download_gdrive, gid, temp_video)
                else:
                    direct = await asyncio.to_thread(extractor.resolve_direct_file, file_url)
                    await asyncio.to_thread(_download_stream, direct, temp_video, cand.get("files_url") or url)

                if not os.path.exists(temp_video) or os.path.getsize(temp_video) == 0:
                    raise RuntimeError("File hasil unduhan kosong.")
                log.info("Anichin host sukses | host=%s label=%s", host, cand.get("label"))
                break
            except Exception as e:
                last_err = e
                log.warning("Anichin host gagal | host=%s err=%r", host, e)
                if os.path.exists(temp_video):
                    try:
                        os.remove(temp_video)
                    except OSError:
                        pass
                continue
        else:
            raise RuntimeError(f"Semua host gagal. Terakhir: {last_err}")

        label = target.get("label") or "video"
        if fmt_key == "mp3":
            mp3_path = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp3")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", temp_video,
                "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                mp3_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0 or not os.path.exists(mp3_path):
                raise RuntimeError("Gagal mengekstrak MP3 dari video.")
            return {
                "path": mp3_path,
                "title": title,
                "duration": 0,
                "thumb": probe.get("thumbnail"),
                "artist": "Anichin",
            }

        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(temp_video, out_video)
        hm = re.search(r"(\d+)", label)
        height = int(hm.group(1)) if hm else 480
        width = 1920 if height >= 1080 else (1280 if height >= 720 else (854 if height >= 480 else 640))
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
