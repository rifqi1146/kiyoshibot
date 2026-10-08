"""Scraper PunishWorld (punishworld.com) — video MP4 langsung, tanpa packer.

FLOW SCRAPER
------------
1. Fetch halaman post via curl_cffi (impersonate Chrome).
2. Parse `<video>` tag → ambil `<source>` berkualitas tertinggi (prefer `high`).
3. Ambil poster/thumbnail dari atribut `poster` atau `og:image`.
4. Judul dari `<h1>` atau `og:title`.
5. `download_to_file` stream MP4 langsung dari CDN dengan progress bar (3s interval)
   dan backoff RetryAfter.
6. `extract_audio` via ffmpeg untuk mode mp3.
"""
import os
import re
import html as html_mod
import logging
import asyncio
import time
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.progress import TransferStats, render_progress_text
from handlers.dl.utils import (
    sanitize_filename,
    progress_bar,
    format_size,
    format_speed,
    format_eta,
    FileSizeLimitExceeded,
)
from .constants import UA, HTTP_TIMEOUT, PROGRESS_MIN_INTERVAL

log = logging.getLogger(__name__)


def _meta(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return (tag.get("content") or "").strip() if tag else ""


async def _safe_edit_status(bot, chat_id, status_msg_id, text: str) -> float:
    if not status_msg_id or not bot:
        return 0.0
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
            log.warning("PunishWorld progress flood control | wait=%.0fs", wait)
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


def _progress_text(title: str, downloaded: int, total: int, speed_bps: float, eta) -> str:
    return render_progress_text(
        sanitize_filename(title, 60),
        downloaded=downloaded,
        total=total,
        speed_bps=speed_bps,
        eta_seconds=eta,
    )


def _content_length(resp) -> int:
    try:
        return int(resp.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        return 0


# ─────────────────────────── scrape ───────────────────────────

def scrape_post(url: str) -> dict:
    """Parse halaman post PunishWorld.

    Return dict:
        title    : str
        thumbnail: str (URL)
        sources  : list[{url, label, type, size}]  — urut dari kualitas tertinggi
    """
    r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch PunishWorld page ({r.status_code})")

    soup = BeautifulSoup(r.text, "html.parser")

    # Judul
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    if not title:
        title = _meta(soup, "og:title")
    if not title:
        title = "PunishWorld"

    # Thumbnail
    thumb = _meta(soup, "og:image")
    if not thumb:
        vid_tag = soup.find("video")
        if vid_tag and vid_tag.get("poster"):
            thumb = vid_tag["poster"]

    # Video sources dari <video> tag
    sources = []
    vid = soup.find("video")
    if vid:
        for src in vid.find_all("source"):
            src_url = src.get("src", "")
            src_type = src.get("type", "")
            src_label = (src.get("title") or "").strip().lower()
            if src_url:
                sources.append({"url": src_url, "label": src_label or "default", "type": src_type})

    # Fallback: JWPlayer/KVS-style sources: [{file:"...",label:"..."}]
    if not sources:
        text = r.text
        for m in re.finditer(r'\{file\s*:\s*"([^"]+)"\s*,\s*label\s*:\s*"([^"]*)"', text):
            sources.append({"url": m.group(1), "label": m.group(2).strip().lower(), "type": ""})

    # Urutkan: high > low > default
    priority = {"high": 0, "1080p": 0, "720p": 1, "low": 2, "480p": 2, "default": 3}
    sources.sort(key=lambda s: priority.get(s["label"], 9))

    if not sources:
        raise RuntimeError("No video found on PunishWorld page")

    log.info("PunishWorld scraped | title=%r sources=%d thumb=%s", title, len(sources), bool(thumb))
    return {"title": title, "thumbnail": thumb, "sources": sources}


def pick_best(sources: list) -> dict:
    """Ambil source terbaik (sudah diurutkan oleh scrape_post)."""
    return sources[0]


# ─────────────────────────── download ───────────────────────────

async def download_to_file(
    url: str,
    out_path: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
) -> int:
    """MP4 ke `out_path` dengan progress bar + limit 2GB.

    Mesin utama: aria2c multi-koneksi. Fallback: streaming curl_cffi.
    """
    headers = {"User-Agent": UA, "Referer": "https://punishworld.com/"}
    interval = PROGRESS_MIN_INTERVAL

    # Probe ukuran untuk pre-check limit 2GB.
    try:
        from curl_cffi import requests as _probe_req
        _probe = await asyncio.to_thread(
            _probe_req.head, url,
            headers=headers, impersonate="chrome", timeout=HTTP_TIMEOUT,
        )
        _probe_total = int(_probe.headers.get("Content-Length") or 0)
        if _probe_total > MAX_TG_SIZE:
            raise FileSizeLimitExceeded(
                f"File exceeds 2GB limit ({_probe_total / 1024 / 1024 / 1024:.2f} GB). Download canceled."
            )
    except FileSizeLimitExceeded:
        raise
    except Exception:
        _probe_total = 0

    try:
        from handlers.dl.aria2 import download_aria2
        ok = await download_aria2(
            url, out_path,
            headers=headers, total_size=_probe_total,
            kind="PunishWorld download", label="PunishWorld MP4", title=title,
            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id, notify=bool(status_msg_id),
            edit_interval=interval, timeout=HTTP_TIMEOUT * 12,
        )
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return os.path.getsize(out_path)
    except FileSizeLimitExceeded:
        raise
    except Exception as e:
        log.warning("PunishWorld aria2c exception, fallback ke streaming | err=%r", e)

    def _get():
        resp = curl_requests.get(
            url, headers=headers, impersonate="chrome",
            timeout=HTTP_TIMEOUT, stream=True,
        )
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"Failed to download PunishWorld ({resp.status_code})")
        return resp

    resp = await asyncio.to_thread(_get)
    total = _content_length(resp)
    if total > MAX_TG_SIZE:
        resp.close()
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

    stats = TransferStats(total=total)
    text = render_progress_text(
        sanitize_filename(title, 60),
        downloaded=0,
        total=total,
        speed_bps=0.0,
        eta_seconds=None,
    )
    await _safe_edit_status(bot, chat_id, status_msg_id, text)

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    flood_until = 0.0
    last_edit = -10.0
    try:
        while True:
            await asyncio.sleep(0.7)
            if write_task.done():
                break
            downloaded = status["downloaded"]
            if downloaded <= 0:
                continue
            stats.sample(downloaded)
            now = time.monotonic()
            if stats.should_edit(interval, now=now):
                text = stats.telegram_text(
                    sanitize_filename(title, 60),
                    extra="",
                )
                flood_wait = await _safe_edit_status(bot, chat_id, status_msg_id, text)
                last_edit = now
                if flood_wait:
                    flood_until = now + flood_wait
        exc = write_task.exception()
        if exc:
            raise exc
    finally:
        if not write_task.done():
            write_task.cancel()

    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError("Failed to download PunishWorld file (empty)")

    downloaded = os.path.getsize(out_path)
    stats.sample(downloaded)
    text = render_progress_text(
        sanitize_filename(title, 60),
        downloaded=downloaded,
        total=total,
        speed_bps=0.0,
        eta_seconds=None,
    )
    await _safe_edit_status(bot, chat_id, status_msg_id, text)
    stats.log_done("PunishWorld download", label=sanitize_filename(title, 40), size=downloaded)
    return downloaded


# ─────────────────────────── thumb & audio ───────────────────────────

def download_thumb(url: str, out_path: str) -> str | None:
    if not url:
        return None
    try:
        r = curl_requests.get(
            url, headers={"User-Agent": UA, "Referer": "https://punishworld.com/"},
            impersonate="chrome", timeout=20,
        )
        if r.status_code == 200 and r.content:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception as e:
        log.debug("download_thumb failed | %r", e)
    return None


def extract_audio(src_path: str, out_path: str) -> str:
    """Ekstrak audio dari video ke MP3 via ffmpeg."""
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600)
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg failed: {(res.stderr or '').strip()[-400:]}")
    return out_path
