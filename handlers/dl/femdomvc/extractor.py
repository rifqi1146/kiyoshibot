"""Scraper FemdomVC (femdomvc.com).

FLOW SCRAPER
------------
1. Terima URL post FemdomVC -> `scrape_post(url)`.
2. Ambil HTML post (curl_cffi, impersonate Chrome). HTTP 404 -> `FileNotFoundError`.
3. Baca metadata:
   - Judul: `<h1>` post -> fallback `og:title` -> fallback `<title>`.
   - Durasi: `meta[property="video:duration"]` (detik).
   - Dimensi: `og:video:width`, `og:video:height`.
   - Poster: `og:image` -> fallback preview screenshot.
4. Temukan direct MP4 download link dari tab download:
   - Tag `<a href="...">` dengan path `/get_file/1/...` dan `download=true` / `.mp4`.
   - Pola: `https://www.femdomvc.com/get_file/1/<hash>/<sub_id>/<id>/<id>.mp4/?download_filename=...&download=true...`
5. HEAD request / GET Range untuk verifikasi `Content-Length`.
6. Return dict `{title, duration, thumbnail, width, height, download_url, filesize}`.

FLOW DOWNLOADER
---------------
1. `download_video(url, out_path, ...)` -> ambil download link via `scrape_post`.
2. GET `stream=True` ke CDN dengan `Referer: url` dan `User-Agent: UA`.
3. `TransferStats` untuk progress bar Telegram + speed/ETA + batas `MAX_TG_SIZE`.
4. Return dict `{"path", "title"}`; pemanggil (`main.py`) menangani mp3.
"""
import os
import re
import shutil
import logging
import asyncio
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded
from handlers.dl.progress import TransferStats

from .constants import UA, HTTP_TIMEOUT, FEMDOMVC_PROGRESS_INTERVAL, FEMDOMVC_ARIA2_CONNS

log = logging.getLogger(__name__)

_TITLE_SUFFIX_RE = re.compile(r"\s*[-|]\s*FemdomVC\s*$", re.I)


def _probe_direct_size(url: str, referer: str, session: curl_requests.Session) -> int:
    """Ambil ukuran file via HEAD request atau Range: bytes=0-0 fallback."""
    headers = {"User-Agent": UA, "Referer": referer}
    try:
        resp = session.head(url, headers=headers, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            sz = int(resp.headers.get("Content-Length") or 0)
            if sz > 0:
                return sz
    except Exception as e:
        log.debug("FemdomVC HEAD probe failed: %r", e)

    try:
        resp = session.get(
            url,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=HTTP_TIMEOUT,
        )
        cr = resp.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        if resp.status_code in (200, 206):
            return int(resp.headers.get("Content-Length") or 0)
    except Exception as e:
        log.debug("FemdomVC Range probe failed: %r", e)
    return 0


def scrape_post(url: str) -> dict:
    session = curl_requests.Session(impersonate="chrome", headers={"User-Agent": UA})
    resp = session.get(url, timeout=HTTP_TIMEOUT)

    if resp.status_code == 404:
        raise FileNotFoundError(f"Video not found (HTTP 404): {url}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} saat mengakses FemdomVC")

    soup = BeautifulSoup(resp.text, "html.parser")

    # 1. Judul
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og_t = soup.find("meta", property="og:title")
        if og_t and og_t.get("content"):
            title = og_t.get("content").strip()
    if not title and soup.title:
        title = _TITLE_SUFFIX_RE.sub("", soup.title.get_text(strip=True))
    title = sanitize_filename(title or "FemdomVC Video", 100)

    # 2. Durasi (detik)
    duration = 0
    dur_meta = soup.find("meta", property="video:duration")
    if dur_meta and dur_meta.get("content"):
        try:
            duration = int(float(dur_meta.get("content")))
        except Exception:
            duration = 0

    # 3. Dimensi video
    w_meta = soup.find("meta", property="og:video:width")
    h_meta = soup.find("meta", property="og:video:height")
    try:
        width = int(w_meta.get("content") or 0) if w_meta else 1280
    except Exception:
        width = 1280
    try:
        height = int(h_meta.get("content") or 0) if h_meta else 720
    except Exception:
        height = 720

    # 4. Thumbnail / Poster
    thumbnail = ""
    img_meta = soup.find("meta", property="og:image")
    if img_meta and img_meta.get("content"):
        thumbnail = img_meta.get("content").strip()

    # 5. Direct MP4 link dari tab download
    # FemdomVC adalah KVS tube; tab download memiliki link:
    # <a href="https://www.femdomvc.com/get_file/1/.../id.mp4/?download_filename=...&download=true...">
    dl_url = ""
    for a in soup.find_all("a", href=re.compile(r"/get_file/1/.*\.mp4")):
        href = a.get("href") or ""
        if "download=true" in href or "download_filename=" in href:
            dl_url = href
            break
        if not dl_url and href.startswith("http"):
            dl_url = href

    # Fallback: regex langsung pada HTML bila bs4 melewatkan
    if not dl_url:
        m = re.search(
            r'href=["\'](https?://(?:www\.)?femdomvc\.com/get_file/1/[^"\']+\.mp4[^"\']*)["\']',
            resp.text,
            re.I,
        )
        if m:
            dl_url = m.group(1).replace("&amp;", "&")

    if not dl_url:
        raise RuntimeError(
            "No MP4 download link found on this FemdomVC page. "
            "Make sure the video is still active and publicly downloadable."
        )

    filesize = _probe_direct_size(dl_url, url, session)

    return {
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "width": width,
        "height": height,
        "download_url": dl_url,
        "filesize": filesize,
    }


async def _download_aria2c(
    dl_url: str,
    referer: str,
    out_path: str,
    total_size: int,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    notify: bool,
) -> bool:
    """Download paralel multi-koneksi via aria2c (14s untuk 70MB vs 4+ menit single stream)."""
    from handlers.dl.aria2 import _resolve_aria2
    aria2 = _resolve_aria2()
    if not aria2:
        return False

    out_dir = os.path.dirname(os.path.abspath(out_path))
    out_file = os.path.basename(out_path)

    cmd = [
        aria2,
        f"-x{FEMDOMVC_ARIA2_CONNS}",
        f"-s{FEMDOMVC_ARIA2_CONNS}",
        "-k1M",
        "--file-allocation=none",
        "--summary-interval=0",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        f"--header=User-Agent: {UA}",
        f"--header=Referer: {referer}",
        f"--dir={out_dir}",
        f"--out={out_file}",
        dl_url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    stats = TransferStats(total_size)

    async def _poll_progress():
        while proc.returncode is None:
            if os.path.exists(out_path):
                cur_size = os.path.getsize(out_path)
                stats.sample(cur_size)
                if cur_size > MAX_TG_SIZE:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise FileSizeLimitExceeded("File exceeds 2GB limit. Download canceled.")
                if notify and status_msg_id:
                    await stats.emit(
                        bot=bot,
                        chat_id=chat_id,
                        status_msg_id=status_msg_id,
                        title=title,
                        kind="FemdomVC download",
                        label="FemdomVC MP4",
                        log_interval=FEMDOMVC_PROGRESS_INTERVAL,
                        edit_interval=FEMDOMVC_PROGRESS_INTERVAL,
                    )
            await asyncio.sleep(0.5)

    poll_task = asyncio.create_task(_poll_progress())
    _, stderr_b = await proc.communicate()
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass

    if proc.returncode != 0 or not os.path.exists(out_path):
        err = (stderr_b or b"").decode("utf-8", errors="ignore").strip()
        log.warning("FemdomVC aria2c failed | code=%s err=%s", proc.returncode, err[:200])
        return False

    final_size = os.path.getsize(out_path)
    stats.sample(final_size)
    if notify and status_msg_id:
        await stats.emit(
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            kind="FemdomVC download",
            label="FemdomVC MP4",
            log_interval=FEMDOMVC_PROGRESS_INTERVAL,
            edit_interval=FEMDOMVC_PROGRESS_INTERVAL,
        )
    stats.log_done("FemdomVC download", label="FemdomVC MP4")
    return True


async def download_video(
    url: str,
    out_path: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    notify: bool = True,
) -> dict:
    post = await asyncio.to_thread(scrape_post, url)
    dl_url = post.get("download_url") or ""
    if not dl_url:
        raise RuntimeError("Failed to get FemdomVC download link.")

    total = int(post.get("filesize") or 0)
    if total > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total / 1024 ** 3:.2f} GB). Download canceled."
        )

    # 1. Coba aria2c paralel terlebih dahulu (CDN FemdomVC men-throttle koneksi tunggal ke ~250KB/s)
    try:
        ok = await _download_aria2c(
            dl_url=dl_url,
            referer=url,
            out_path=out_path,
            total_size=total,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            notify=notify,
        )
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return {"path": out_path, "title": title}
    except FileSizeLimitExceeded:
        raise
    except Exception as e:
        log.warning("FemdomVC aria2c exception, fallback ke streaming | err=%r", e)

    # 2. Fallback: streaming langsung via curl_cffi
    headers = {"User-Agent": UA, "Referer": url}

    def _get():
        resp = curl_requests.get(
            dl_url,
            headers=headers,
            impersonate="chrome",
            stream=True,
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {resp.status_code} saat mengunduh video FemdomVC.")
        return resp

    resp = await asyncio.to_thread(_get)
    try:
        hdr_total = int(resp.headers.get("Content-Length") or 0)
        if hdr_total > 0:
            total = hdr_total
    except Exception:
        pass

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
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            kind="FemdomVC download",
            label="FemdomVC MP4",
            log_interval=FEMDOMVC_PROGRESS_INTERVAL,
            edit_interval=FEMDOMVC_PROGRESS_INTERVAL,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                title=title,
                kind="FemdomVC download",
                label="FemdomVC MP4",
                log_interval=FEMDOMVC_PROGRESS_INTERVAL,
                edit_interval=FEMDOMVC_PROGRESS_INTERVAL,
            )
        await asyncio.sleep(0.5)

    await write_task
    stats.log_done("FemdomVC download", label="FemdomVC MP4")
    return {"path": out_path, "title": title}
