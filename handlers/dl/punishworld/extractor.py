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
    label = sanitize_filename(title, 60)
    pct = (downloaded * 100.0 / total) if total > 0 else 0.0
    bar = progress_bar(pct)
    size_str = f"{format_size(downloaded)} / {format_size(total)}" if total else format_size(downloaded)
    lines = [f"<b>{html_mod.escape(label)}</b>", ""]
    lines.append(f"<code>{bar}</code>")
    lines.append(f"<code>{size_str}</code>")
    if speed_bps and speed_bps > 0:
        lines.append(f"<code>Speed: {format_speed(speed_bps)}</code>")
    if eta is not None:
        lines.append(f"<code>ETA: {format_eta(eta)}</code>")
    return "\n".join(lines)


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
        raise RuntimeError(f"Gagal mengambil halaman PunishWorld ({r.status_code})")

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
        raise RuntimeError("Tidak ada video ditemukan di halaman PunishWorld")

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
    """Stream MP4 ke `out_path` dengan progress bar + limit 2GB."""
    headers = {"User-Agent": UA, "Referer": "https://punishworld.com/"}
    interval = PROGRESS_MIN_INTERVAL

    def _get():
        resp = curl_requests.get(
            url, headers=headers, impersonate="chrome",
            timeout=HTTP_TIMEOUT, stream=True,
        )
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"Gagal mengunduh PunishWorld ({resp.status_code})")
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
            downloaded = status["downloaded"]
            if downloaded <= 0:
                continue
            now = time.time()
            elapsed = max(now - last_sample_ts, 0.001)
            speed_bps = max(downloaded - last_sample_size, 0) / elapsed
            eta = (
                ((total - downloaded) / speed_bps)
                if total > 0 and speed_bps > 0 and downloaded <= total
                else None
            )
            if now >= flood_until and (now - last_edit >= interval or last_edit < 0):
                flood_wait = await _safe_edit_status(
                    bot, chat_id, status_msg_id,
                    _progress_text(title, downloaded, total, speed_bps, eta),
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
        raise RuntimeError("Gagal mengunduh file PunishWorld (kosong)")

    downloaded = os.path.getsize(out_path)
    await _safe_edit_status(
        bot, chat_id, status_msg_id,
        _progress_text(title, downloaded, total, 0.0, None),
    )
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
        raise RuntimeError(f"ffmpeg gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path
