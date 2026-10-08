"""Scraper DarknessPorn (darknessporn.com).

FLOW SCRAPER
------------
1. Terima URL post DarknessPorn -> `scrape_post(url)`.
2. Ambil HTML post (curl_cffi, impersonate Chrome). HTTP 404 -> `FileNotFoundError`.
3. Baca metadata JSON-LD `VideoObject`: `name`, `duration`, `thumbnailUrl`,
   `contentUrl`. Fallback judul: `<h1>` lalu `<title>`.
4. Baca tag `<video>` -> setiap `<source src>` dengan `title="high"` / `title="low"`
   (URL CDN `*.nosofiles.com` + tanda tangan `?verify=<epoch>-<hash>`).
   Fallback: `contentUrl` JSON-LD (setara varian high).
5. `HEAD`/`GET Range` tiap varian untuk `Content-Length`.
6. Return `{title, duration, thumbnail, variants, res_list}`.

CDN `xcdn1.nosofiles.com` (sama dengan CosXplay) melayani `video/mp4`
**tanpa Referer wajib**; kecepatan uji ~55 MB/s. URL `?verify=` bertanggal
(epoch) — JANGAN di-cache lama, ambil ulang halaman tiap unduh.

FLOW DOWNLOADER
---------------
1. `download_video(url, format_id, out_path, ...)` -> pilih varian (high/low).
2. GET `stream=True` ke CDN, tulis per chunk.
3. `TransferStats` untuk progress bar Telegram + speed/ETA + batas `MAX_TG_SIZE`.
4. Return dict `{"path", "title"}`; pemanggil (`main.py`) menangani mp3.
"""
import os
import re
import json
import logging
import asyncio
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded
from handlers.dl.progress import TransferStats, edit_status

from .constants import UA, HTTP_TIMEOUT, DARKNESSPORN_PROGRESS_INTERVAL

log = logging.getLogger(__name__)

_TITLE_SUFFIX_RE = re.compile(r"\s*[-|]\s*Darknessporn\.com\s*$", re.I)


def _parse_iso8601_duration(duration_str: str) -> int:
    """`P0DT0H1M6S` -> 66 detik. Toleran terhadap variasi ISO 8601."""
    if not duration_str:
        return 0
    m = re.match(
        r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
        duration_str.strip(),
    )
    if not m:
        d = re.search(r"(\d+)D", duration_str)
        h = re.search(r"(\d+)H", duration_str)
        mi = re.search(r"(\d+)M", duration_str)
        s = re.search(r"(\d+)S", duration_str)
        return (
            (int(d.group(1)) if d else 0) * 86400
            + (int(h.group(1)) if h else 0) * 3600
            + (int(mi.group(1)) if mi else 0) * 60
            + (int(s.group(1)) if s else 0)
        )
    return (
        int(m.group(1) or 0) * 86400
        + int(m.group(2) or 0) * 3600
        + int(m.group(3) or 0) * 60
        + int(m.group(4) or 0)
    )


def _probe_format_size(url: str, session: curl_requests.Session) -> int:
    """Content-Length varian CDN. Tanpa HEAD (beberapa CDN tolak HEAD),
    pakai GET `Range: bytes=0-0` yang selalu balas `Content-Range`."""
    try:
        resp = session.get(
            url,
            headers={"User-Agent": UA, "Referer": "https://darknessporn.com/", "Range": "bytes=0-0"},
            timeout=HTTP_TIMEOUT,
        )
        cr = resp.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        if resp.status_code in (200, 206):
            return int(resp.headers.get("Content-Length") or 0)
    except Exception as e:
        log.debug("DarknessPorn probe size failed: %r", e)
    return 0


def _variant_label(src: str, tag_label: str) -> str:
    if tag_label:
        return tag_label
    low = (src or "").lower()
    if "_high.mp4" in low:
        return "high"
    if "_low.mp4" in low:
        return "low"
    return "high"


def scrape_post(url: str) -> dict:
    session = curl_requests.Session(impersonate="chrome", headers={"User-Agent": UA})
    resp = session.get(url, timeout=HTTP_TIMEOUT)

    if resp.status_code == 404:
        raise FileNotFoundError(f"Video not found (HTTP 404): {url}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} saat mengakses DarknessPorn")

    soup = BeautifulSoup(resp.text, "html.parser")

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
            title = _TITLE_SUFFIX_RE.sub("", soup.title.get_text(strip=True))
    title = sanitize_filename(title or "DarknessPorn Video", 100)

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

    def _add(src: str, tag_label: str):
        src = (src or "").strip().replace("&amp;", "&")
        if not src or not src.startswith("http") or src in seen_urls:
            return
        seen_urls.add(src)
        label = _variant_label(src, tag_label)
        height = 1080 if label == "high" else 240
        fsize = _probe_format_size(src, session)
        formats.append({
            "format_id": label,
            "url": src,
            "label": label.capitalize(),
            "height": height,
            "has_audio": True,
            "filesize": fsize,
            "total_size": fsize,
            "ext": "mp4",
            "fps": "-",
        })

    if video_tag:
        for src_tag in video_tag.find_all("source"):
            _add(src_tag.get("src"), (src_tag.get("title") or "").lower())
        if not formats and video_tag.get("src"):
            _add(video_tag.get("src"), "")

    if not formats and video_meta.get("contentUrl"):
        _add(video_meta["contentUrl"], "high")

    if not formats:
        raise RuntimeError("No video variants found on this page.")

    return {
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "variants": formats,
        "res_list": sorted(formats, key=lambda x: x["height"], reverse=True),
    }


async def download_video(
    url: str, format_id: str, out_path: str, bot, chat_id,
    status_msg_id, title: str, notify: bool = True,
):
    post = await asyncio.to_thread(scrape_post, url)
    variants = post.get("variants") or []
    target = next((v for v in variants if v["format_id"] == format_id), None)
    if not target and variants:
        target = variants[0]
    if not target:
        raise RuntimeError("Failed to get video download link.")

    dl_url = target["url"]
    headers = {"User-Agent": UA, "Referer": "https://darknessporn.com/"}
    total_hint = int(target.get("filesize") or 0)
    if total_hint > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total_hint / 1024 ** 3:.2f} GB). Download canceled."
        )

    # Mesin utama: aria2c multi-koneksi.
    try:
        from handlers.dl.aria2 import download_aria2
        ok = await download_aria2(
            dl_url, out_path,
            headers=headers, total_size=total_hint,
            kind="DarknessPorn download", label=f"DarknessPorn {target.get('label')}",
            title=title, bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            notify=notify, edit_interval=DARKNESSPORN_PROGRESS_INTERVAL,
            timeout=HTTP_TIMEOUT * 12,
        )
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return {"path": out_path, "title": title}
    except FileSizeLimitExceeded:
        raise
    except Exception as e:
        log.warning("DarknessPorn aria2c exception, fallback ke streaming | err=%r", e)

    # Fallback: streaming langsung.
    def _get():
        resp = curl_requests.get(
            dl_url, headers=headers, impersonate="chrome",
            stream=True, timeout=HTTP_TIMEOUT,
        )
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
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total / 1024 ** 3:.2f} GB). Download canceled."
        )

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
            title=title, kind="DarknessPorn download",
            label=f"DarknessPorn {target.get('label')}",
            log_interval=DARKNESSPORN_PROGRESS_INTERVAL,
            edit_interval=DARKNESSPORN_PROGRESS_INTERVAL,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                title=title, kind="DarknessPorn download",
                label=f"DarknessPorn {target.get('label')}",
                log_interval=DARKNESSPORN_PROGRESS_INTERVAL,
                edit_interval=DARKNESSPORN_PROGRESS_INTERVAL,
            )
        await asyncio.sleep(0.5)

    await write_task
    stats.log_done("DarknessPorn download", label=f"DarknessPorn {target.get('label')}")
    return {"path": out_path, "title": title}
