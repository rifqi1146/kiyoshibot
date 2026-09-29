"""Scraper Pawchive (arsip Patreon/Fanbox bergaya Kemono).

FLOW SCRAPER
------------
1. Terima URL post Pawchive  ->  `scrape_post(url)`.
2. Ambil HTML post (curl_cffi, impersonate Chrome) -> BeautifulSoup.
3. Baca:
     - judul            : `.post__title span` pertama
     - creator          : `.post__user-name`
     - video            : `.post__videos li` -> `<summary>` (nama) + `<source src>`
     - lampiran/gambar  : `a[download][href]` (nama dari atribut `download`)
4. Dedup lampiran berdasarkan URL.
5. Kalau tidak ada media sama sekali -> fallback ke API JSON
   `/api/v1/{service}/user/{user}/post/{post}` (`attachments[].path`).
6. Klasifikasi URL -> video (mp4/webm/mkv) atau gambar (jpg/png/gif/webp).
7. Pemilihan video:
     - Ada label resolusi (`1080p`, `720p`, ...) -> ambil tertinggi.
     - Hanya 1 file video -> file itu.
     - Beberapa file tanpa label resolusi -> semua dianggap bagian -> album.
8. `pick_best_video` mengembalikan dict {url, name, label, size}.
9. `download_to_file` men-stream dari `file.pawchive.pw` ke disk dengan
   pembatasan ukuran 2GB + progress bar di pesan status:
   writer jalan di thread (to_thread), loop polling baca ukuran file tiap 0.7s,
   edit status maksimal tiap 3s (env PAWCHIVE_PROGRESS_INTERVAL), hitung
   speed/ETA dinamis, backoff saat kena RetryAfter (flood control).
10. `download_album` dipakai untuk post gambar / multi-part (album Telegram).
"""
import os
import re
import html as html_mod
import logging
import asyncio
import time
from urllib.parse import urlparse, parse_qs, urlsplit

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import (
    sanitize_filename,
    progress_bar,
    format_size,
    format_speed,
    format_eta,
    FileSizeLimitExceeded,
)

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 45
# Interval edit progress (detik) — samakan skala dengan scraper lain (3s default).
PAWCHIVE_PROGRESS_INTERVAL = float(os.getenv("PAWCHIVE_PROGRESS_INTERVAL", "3"))
_VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov", ".m4v")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_FILE_HOST = "https://file.pawchive.pw"
_QUALITY_RE = re.compile(r"(\d{3,4})\s*p", re.I)
_VARIANT_RE = re.compile(r"normal|low|mid|high|full|light|heavy|mobile|source|源画", re.I)


async def _safe_edit_status(bot, chat_id, status_msg_id, text: str):
    if not status_msg_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return 0.0
    except Exception as e:
        wait = _flood_wait(e)
        if wait:
            log.warning("Pawchive progress flood control | wait=%.0fs", wait)
        return wait


def _flood_wait(error) -> float:
    """Deteksi RetryAfter / 'Flood control exceeded' -> detik tunggu."""
    text = str(error or "")
    m = re.search(r"[Rr]etr[yY] in (\d+)", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*seconds", text)
    if m:
        return float(m.group(1))
    return 0.0


def _progress_text(title: str, downloaded: int, total: int, speed_bps: float, eta_seconds: float | None) -> str:
    lines = [f"<b>{html_mod.escape(sanitize_filename(title, 80))}</b>", ""]
    if total > 0:
        lines.append(f"<code>{progress_bar(downloaded * 100.0 / total)}</code>")
        lines.append(f"<code>{format_size(downloaded)}/{format_size(total)} downloaded</code>")
    else:
        lines.append(f"<code>{format_size(downloaded)} downloaded</code>")
    if speed_bps > 0:
        lines.append(f"<code>Speed: {format_speed(speed_bps)}</code>")
    if eta_seconds is not None and eta_seconds >= 0 and total > 0 and speed_bps > 0:
        lines.append(f"<code>ETA: {format_eta(eta_seconds)}</code>")
    return "\n".join(lines)


def _ext_of(url: str) -> str:
    path = urlsplit(url or "").path
    name = os.path.basename(path).lower()
    for ext in _VIDEO_EXT + _IMAGE_EXT:
        if name.endswith(ext):
            return ext
    return ""


def _name_of(url: str, fallback: str = "") -> str:
    """Nama file asli dari query `?f=`, fallback ke basename path."""
    try:
        query = parse_qs(urlsplit(url or "").query)
        value = (query.get("f") or [""])[0]
        if value:
            return value
    except Exception:
        pass
    name = os.path.basename(urlsplit(url or "").path)
    return html_mod.unescape(name or fallback)


def extension_for(name: str | None, default: str = ".mp4") -> str:
    ext = _ext_of(name or "")
    return ext or default


def _is_video(url: str) -> bool:
    return _ext_of(url) in _VIDEO_EXT


def _is_image(url: str) -> bool:
    return _ext_of(url) in _IMAGE_EXT


def _quality_rank(name: str) -> int:
    m = _QUALITY_RE.search(name or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 1 if _VARIANT_RE.search(name or "") else 0


def resolve_videos(videos: list) -> list:
    """Mengembalikan daftar video final untuk post ini:
    - Jika multi-part (video berbeda-beda), kembalikan SEMUA video.
    - Jika ada varian resolusi, pilih varian terbaik untuk setiap video unik.
    """
    if not videos:
        return []
    if len(videos) == 1:
        return [videos[0]]

    # Cek apakah ada label varian (1080p, 720p, dsb)
    has_variants = any(_VARIANT_RE.search(v.get("name", "")) for v in videos)
    
    if not has_variants:
        # Tidak ada label varian -> semua adalah video berbeda (multi-part)!
        return videos

    # Jika ada label varian, kelompokkan berdasarkan base name
    groups = {}
    for v in videos:
        name = v.get("name", "")
        base = _VARIANT_RE.sub("", name).strip()
        groups.setdefault(base, []).append(v)

    result = []
    for base, vids in groups.items():
        ranked = sorted(vids, key=lambda x: _quality_rank(x.get("name", "")), reverse=True)
        result.append(ranked[0])
    
    return result

def pick_best_video(videos: list) -> dict:
    """Pilih video terbaik: resolusi tertinggi dulu, lalu label varian."""
    if not videos:
        raise RuntimeError("Tidak ada video di post ini")
    if len(videos) == 1:
        return videos[0]
    ranked = sorted(videos, key=lambda v: _quality_rank(v.get("name", "")), reverse=True)
    top = ranked[0]
    # Semua peringkat 0 dan tidak ada label varian -> kemungkinan multi-part.
    if _quality_rank(top.get("name", "")) == 0 and not any(
        _VARIANT_RE.search(v.get("name", "")) for v in videos
    ):
        return videos[0]
    return top


def _add(items: list, seen: set, url: str, name: str = ""):
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")) or url in seen:
        return
    seen.add(url)
    items.append({
        "url": url,
        "name": _name_of(url, name) or (name or ""),
        "ext": _ext_of(url),
    })


def _scrape_html(url: str, text: str) -> dict:
    soup = BeautifulSoup(text, "html.parser")

    title = ""
    title_el = soup.select_one(".post__title")
    if title_el:
        first_span = title_el.find("span")
        title = (first_span or title_el).get_text(" ", strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        title = (og.get("content") if og else "") or (soup.title.get_text(strip=True) if soup.title else "")
    title = title.replace(" | Pawchive", "").strip()

    creator = ""
    creator_el = soup.select_one(".post__user-name")
    if creator_el:
        creator = creator_el.get_text(" ", strip=True)

    videos, images = [], []
    seen = set()

    for li in soup.select(".post__videos li"):
        src = li.find("source")
        if not src or not src.get("src"):
            continue
        summary = li.find("summary")
        label = summary.get_text(" ", strip=True) if summary else ""
        _add(videos, seen, src.get("src"), label)

    for a in soup.select("a[download][href]"):
        href = a.get("href")
        label = a.get("download") or a.get_text(" ", strip=True)
        target = videos if _is_video(href) else (images if _is_image(href) else None)
        if target is not None:
            _add(target, seen, href, label)

    if not videos and not images:
        for img in soup.select(".post__files img[data-src], .post__files img[src]"):
            _add(images, seen, img.get("data-src") or img.get("src"))

    first_image = (images[0].get("url") if images else "") or ""
    for img in soup.select(".post__thumbnail img[data-src], .post__thumbnail img[src]"):
        first_image = img.get("data-src") or img.get("src") or first_image
        break
    if not first_image:
        og_image = soup.find("meta", property="og:image")
        first_image = (og_image.get("content") if og_image else "") or first_image

    return {
        "title": title,
        "creator": creator,
        "videos": videos,
        "images": images,
        "thumbnail": first_image,
    }


def _scrape_api(url: str) -> dict:
    m = re.search(r"/([^/]+)/user/([^/]+)/post/([^/?#]+)", urlparse(url).path)
    if not m:
        raise RuntimeError("URL post Pawchive tidak dikenali")
    service, user, post = m.group(1), m.group(2), m.group(3)
    api = f"https://pawchive.pw/api/v1/{service}/user/{user}/post/{post}"
    r = curl_requests.get(
        api,
        headers={"User-Agent": UA, "Accept": "application/json", "Referer": "https://pawchive.pw/"},
        impersonate="chrome",
        timeout=_HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} saat mengambil API Pawchive")
    data = r.json()

    videos, images = [], []
    seen = set()
    for att in (data.get("attachments") or []):
        path = att.get("path") or ""
        name = att.get("name") or ""
        if not path:
            continue
        file_url = f"{_FILE_HOST}/data{path}" if path.startswith("/") else f"{_FILE_HOST}/{path}"
        target = videos if _is_video(file_url) else (images if _is_image(file_url) else None)
        if target is not None:
            _add(target, seen, file_url, name)

    return {
        "title": (data.get("title") or "").strip(),
        "creator": "",
        "videos": videos,
        "images": images,
        "thumbnail": (images[0].get("url") if images else "") or "",
    }


def scrape_post(url: str) -> dict:
    """-> {title, creator, videos:[{url,name,ext}], images:[{url,name,ext}]}"""
    r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=_HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} saat mengambil halaman Pawchive")
    result = _scrape_html(url, r.text)
    if not result["videos"] and not result["images"]:
        log.warning("Pawchive HTML tidak menemukan media, fallback ke API | url=%s", url)
        api_result = _scrape_api(url)
        if not api_result["title"]:
            api_result["title"] = result["title"]
        if not api_result["creator"]:
            api_result["creator"] = result["creator"]
        result = api_result
    log.info(
        "Pawchive scrape | title=%r creator=%r videos=%s images=%s",
        result["title"], result["creator"], len(result["videos"]), len(result["images"]),
    )
    return result


def _content_length(resp) -> int:
    try:
        return int(resp.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        return 0


async def download_to_file(
    url: str,
    out_path: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    notify: bool = True,
) -> int:
    """Stream file Pawchive ke `out_path` dengan progress + limit 2GB.

    `notify=False` dipakai jalur album: unduh senyap tanpa sentuh pesan status,
    supaya tidak menembak edit message sekali per file (rawan flood control).
    Untuk file tunggal (`notify=True`), polling loop seperti scraper lain:
    baca ukuran file tiap 0.7s, edit status maksimal tiap PAWCHIVE_PROGRESS_INTERVAL
    (3s, bisa dioverride env), hitung speed/ETA dinamis, dan backoff saat kena
    RetryAfter (flood control).
    """
    headers = {"User-Agent": UA, "Referer": "https://pawchive.pw/"}
    interval = PAWCHIVE_PROGRESS_INTERVAL

    def _get():
        resp = curl_requests.get(
            url, headers=headers, impersonate="chrome", timeout=_HTTP_TIMEOUT, stream=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Gagal mengunduh Pawchive ({resp.status_code})")
        return resp

    resp = await asyncio.to_thread(_get)
    total = _content_length(resp)
    if total > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total / 1024 / 1024 / 1024:.2f} GB). Download canceled."
        )

    status = {"downloaded": 0}

    def _write():
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                status["downloaded"] += len(chunk)
                if status["downloaded"] > MAX_TG_SIZE:
                    resp.close()
                    raise FileSizeLimitExceeded("File exceeds 2GB limit. Download canceled.")
        try:
            resp.close()
        except Exception:
            pass

    if notify:
        await _safe_edit_status(bot, chat_id, status_msg_id, _progress_text(title, 0, total, 0.0, None))

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    flood_until = 0.0
    last_edit = -10.0
    last_sample_size = 0
    last_sample_ts = time.time()
    try:
        while True:
            await asyncio.sleep(0.7)
            if write_task.done():
                break
            if not notify:
                continue
            downloaded = status["downloaded"]
            if downloaded <= 0:
                continue
            now = time.time()
            elapsed = max(now - last_sample_ts, 0.001)
            speed_bps = max(downloaded - last_sample_size, 0) / elapsed
            eta = ((total - downloaded) / speed_bps) if total > 0 and speed_bps > 0 and downloaded <= total else None
            if now >= flood_until and (now - last_edit >= interval or last_edit < 0):
                flood_wait = await _safe_edit_status(
                    bot, chat_id, status_msg_id, _progress_text(title, downloaded, total, speed_bps, eta)
                )
                last_edit = now
                if flood_wait:
                    flood_until = now + flood_wait
            last_sample_size = downloaded
            last_sample_ts = now
        exc = write_task.exception()
        if exc:
            raise exc
    finally:
        if not write_task.done():
            write_task.cancel()

    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError("Gagal mengunduh file Pawchive (kosong)")

    downloaded = os.path.getsize(out_path)
    if notify:
        await _safe_edit_status(bot, chat_id, status_msg_id, _progress_text(title, downloaded, total, 0.0, None))
    return downloaded


def download_thumb(url: str, out_path: str) -> str | None:
    if not url:
        return None
    try:
        r = curl_requests.get(
            url,
            headers={"User-Agent": UA, "Referer": "https://pawchive.pw/"},
            impersonate="chrome",
            timeout=20,
        )
        if r.status_code == 200 and r.content:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception as e:
        log.debug("Pawchive download_thumb failed | %r", e)
    return None


def extract_audio(src_path: str, out_path: str) -> str:
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600)
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path


def _album_interval(total: int) -> float:
    """Interval edit progress album (detik) — makin banyak foto makin jarang.

    Sedikit foto (<=5) -> 3s, banyak foto (>=50) -> 5s, sisanya linear di antaranya.
    """
    if total <= 5:
        return 3.0
    if total >= 50:
        return 5.0
    return 3.0 + 2.0 * (total - 5) / 45.0


async def download_album(medias_list: list, title: str, bot, chat_id, status_msg_id, out_dir: str, tag: str = "paw", media_type: str = "photo", item_label: str = "foto") -> dict:
    """Unduh semua media (gambar atau video) menjadi album.

    Pesan status hanya menampilkan jumlah media dan progres (misal '1/30 foto'),
    diedit sekali per interval `_album_interval(total)` (3-5 detik, menyesuaikan
    jumlah media: sedikit media -> 3s, banyak media -> 5s) supaya aman dari rate limit.
    File diletakkan di `out_dir` (pakai TMP_DIR, bukan work_dir sementara) karena
    folder kerja sementara dihapus di blok `finally` sebelum album dikirim.
    """
    medias = [m for m in (medias_list or []) if m.get("url")]
    total = len(medias)
    if not total:
        raise RuntimeError("Tidak ada media yang bisa diunduh di post ini")

    interval = _album_interval(total)
    escaped_title = html_mod.escape(sanitize_filename(title, 80))

    def _render_album_status(done_count: int) -> str:
        pct = (done_count * 100.0 / total) if total else 0.0
        return (
            f"<b>{escaped_title}</b>\n\n"
            f"<code>{progress_bar(pct)}</code>\n"
            f"<code>{done_count}/{total} {item_label}</code>"
        )

    flood_until = 0.0
    last_edit = 0.0
    if status_msg_id:
        wait = await _safe_edit_status(bot, chat_id, status_msg_id, _render_album_status(0))
        last_edit = time.time()
        if wait:
            flood_until = last_edit + wait

    items = []
    for idx, item in enumerate(medias, 1):
        m_type = item.get("type", media_type)
        def_ext = ".mp4" if m_type == "video" else ".jpg"
        prefix = "vid" if m_type == "video" else "img"
        name = f"{tag}_{prefix}_{idx:03d}{extension_for(item.get('name'), def_ext)}"
        out = os.path.join(out_dir, name)
        
        # Untuk multiple media, progress bar individu akan mengganggu (rate limit).
        # Jadi kita panggil download_to_file senyap (notify=False)
        await download_to_file(item.get("url"), out, bot, chat_id, status_msg_id, title, notify=False)
        items.append({"path": out, "type": m_type})
        now = time.time()
        if status_msg_id and now >= flood_until and (idx == total or now - last_edit >= interval):
            wait = await _safe_edit_status(bot, chat_id, status_msg_id, _render_album_status(idx))
            last_edit = now
            if wait:
                flood_until = now + wait

    return {"items": items, "title": sanitize_filename(title or "Pawchive", 100)}
