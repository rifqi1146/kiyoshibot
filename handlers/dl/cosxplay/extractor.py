"""Scraper CosXplay (cosxplay.com).

FLOW SCRAPER
------------
1. Terima URL post CosXplay -> `scrape_post(url)`.
2. Ambil HTML post dengan `curl_cffi` (impersonate Chrome). Jika HTTP 404, throw `FileNotFoundError`.
3. Deteksi apakah URL ini adalah kategori/koleksi (cek class `archive` atau `category` di `<body>`). Jika ya, throw `ValueError`.
4. Parse metadata JSON-LD (tipe `VideoObject`) -> ekstrak `name`, `duration` (ISO 8601), `thumbnailUrl`, `contentUrl`.
5. Parse tag `<video>` untuk mengekstrak resolusi (`_high.mp4`, `_low.mp4`).
6. Kumpulkan semua varian resolusi. Lakukan `HEAD` request untuk setiap varian untuk mendapatkan `Content-Length`.
7. Kembalikan dict berisi `title`, `duration`, `thumbnail`, dan `res_list` (yang sesuai standar format resolusi Telegram).

FLOW DOWNLOADER
---------------
1. Terima URL dan resolusi spesifik (`format_id`) -> `download_video(url, format_id, out_path, ...)`.
2. Cari URL asli resolusi yang dipilih dengan mengekstrak JSON-LD / `<video>` lagi.
3. Lakukan `GET stream=True` ke URL file (CDN nosofiles.com) menggunakan `curl_cffi` dengan `Referer: https://cosxplay.com/`.
4. Pakai `TransferStats` untuk memantau progress ukuran file, menghitung `speed` dan `ETA`.
5. Tulis ke file disk per chunk.
6. Edit pesan status secara berkala dengan interval `COSXPLAY_PROGRESS_INTERVAL`. Batasi ukuran file max 2GB (`FileSizeLimitExceeded`).
7. Return dict path hasil unduhan ke `nekopoi_download` / `cosxplay_download`.
"""
import os
import re
import json
import time
import asyncio
import logging
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded
from handlers.dl.progress import TransferStats, edit_status

from .constants import UA, HTTP_TIMEOUT, COSXPLAY_PROGRESS_INTERVAL

log = logging.getLogger(__name__)


def _parse_iso8601_duration(duration_str: str) -> int:
    if not duration_str:
        return 0
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", duration_str.strip())
    if not m:
        # Fallback manual parsing
        d = re.search(r"(\d+)D", duration_str)
        h = re.search(r"(\d+)H", duration_str)
        mi = re.search(r"(\d+)M", duration_str)
        s = re.search(r"(\d+)S", duration_str)
        days = int(d.group(1)) if d else 0
        hours = int(h.group(1)) if h else 0
        minutes = int(mi.group(1)) if mi else 0
        seconds = int(s.group(1)) if s else 0
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _probe_format_size(url: str, session: curl_requests.Session) -> int:
    try:
        resp = session.head(url, headers={"Referer": "https://cosxplay.com/"}, timeout=10)
        if resp.status_code in (200, 206):
            return int(resp.headers.get("Content-Length") or 0)
    except Exception as e:
        log.debug("CosXplay probe size HEAD failed: %r", e)
    return 0


def scrape_post(url: str) -> dict:
    session = curl_requests.Session(impersonate="chrome", headers={"User-Agent": UA})
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    
    if resp.status_code == 404:
        raise FileNotFoundError(f"Video tidak ditemukan (HTTP 404): {url}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} saat mengakses CosXplay")
        
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.find("body")
    body_classes = body.get("class", []) if body else []
    if "archive" in body_classes or "category" in body_classes:
        raise ValueError("URL ini adalah halaman kategori/koleksi, bukan video spesifik.")
        
    video_meta = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            graph = data.get("@graph", [data]) if isinstance(data, dict) else []
            for item in graph:
                if isinstance(item, dict) and item.get("@type") == "VideoObject":
                    video_meta = item
                    break
        except Exception:
            continue

    title = video_meta.get("name")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
        if not title and soup.title:
            title = re.sub(r"\s*\|\s*CosXplay\.com\s*$", "", soup.title.get_text(strip=True), flags=re.I)
    title = sanitize_filename(title or "CosXplay Video", 100)

    duration = _parse_iso8601_duration(video_meta.get("duration", ""))

    thumbnail = ""
    if video_meta.get("thumbnailUrl"):
        t = video_meta["thumbnailUrl"]
        thumbnail = t[0] if isinstance(t, list) and t else str(t)
    video_tag = soup.find("video")
    if not thumbnail and video_tag and video_tag.get("poster"):
        thumbnail = video_tag.get("poster")

    formats = []
    seen_urls = set()
    if video_tag:
        for src_tag in video_tag.find_all("source"):
            src = src_tag.get("src")
            if not src or src in seen_urls:
                continue
            seen_urls.add(src)
            label = "High" if "_high.mp4" in src else ("Low" if "_low.mp4" in src else "Standard")
            h = 1080 if label == "High" else 240
            fsize = _probe_format_size(src, session)
            formats.append({
                "format_id": label.lower(),
                "url": src,
                "label": label,
                "height": h,
                "has_audio": True,
                "filesize": fsize,
                "total_size": fsize,
                "ext": "mp4",
                "fps": "-"
            })

    if not formats and video_meta.get("contentUrl"):
        src = video_meta["contentUrl"]
        fsize = _probe_format_size(src, session)
        formats.append({
            "format_id": "high",
            "url": src,
            "label": "High",
            "height": 1080,
            "has_audio": True,
            "filesize": fsize,
            "total_size": fsize,
            "ext": "mp4",
            "fps": "-"
        })

    if not formats:
        raise RuntimeError("Tidak ada varian video yang ditemukan di halaman ini.")

    return {
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "variants": formats,
        "res_list": sorted(formats, key=lambda x: x["height"], reverse=True)
    }


async def download_video(url: str, format_id: str, out_path: str, bot, chat_id, status_msg_id, title: str, notify: bool = True):
    post = await asyncio.to_thread(scrape_post, url)
    variants = post.get("variants") or []
    target = next((v for v in variants if v["format_id"] == format_id), None)
    if not target and variants:
        target = variants[0]
    if not target:
        raise RuntimeError("Gagal mendapatkan link download video.")
        
    dl_url = target["url"]
    headers = {"User-Agent": UA, "Referer": "https://cosxplay.com/"}
    
    def _get():
        resp = curl_requests.get(dl_url, headers=headers, impersonate="chrome", stream=True, timeout=HTTP_TIMEOUT)
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {resp.status_code} saat mengunduh video.")
        return resp
        
    resp = await asyncio.to_thread(_get)
    try:
        total = int(resp.headers.get("Content-Length") or 0)
    except Exception:
        total = 0
        
    if total > MAX_TG_SIZE:
        resp.close()
        raise FileSizeLimitExceeded(f"File exceeds 2GB limit ({total/1024**3:.2f} GB). Download canceled.")
        
    stats = TransferStats(total)
    
    def _write():
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                stats.sample(stats.downloaded + len(chunk))
                if stats.downloaded > MAX_TG_SIZE:
                    resp.close()
                    raise FileSizeLimitExceeded("File exceeds 2GB limit. Download canceled.")
        resp.close()
        
    if notify and status_msg_id:
        await stats.emit(
            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            title=title, kind="CosXplay download", label=f"CosXplay {target.get('label')}",
            log_interval=10.0, edit_interval=0.0  # Force first edit immediately
        )
        
    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                title=title, kind="CosXplay download", label=f"CosXplay {target.get('label')}",
                log_interval=10.0, edit_interval=COSXPLAY_PROGRESS_INTERVAL
            )
        await asyncio.sleep(0.5)
        
    await write_task
    stats.log_done("CosXplay download", label=f"CosXplay {target.get('label')}")
    return {"path": out_path, "title": title}
