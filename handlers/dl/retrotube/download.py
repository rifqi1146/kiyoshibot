import os
import re
import time
import logging
import subprocess
import asyncio
from urllib.parse import urljoin
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.progress import PROGRESS_LOG_INTERVAL, TransferStats, render_progress_text
from handlers.dl.utils import FileSizeLimitExceeded
from .constants import (
    UA,
    _HTTP_TIMEOUT,
    _FFMPEG_TIMEOUT,
    _SEG_CONCURRENCY,
    _SEG_RETRIES,
    _FAST_INTERVAL,
    _SLOW_INTERVAL,
    _FAST_SPEED_BPS,
    DEBUG_RETROTUBE,
)

log = logging.getLogger(__name__)


def _dbg(msg, *args):
    if DEBUG_RETROTUBE:
        log.warning("RTDBG | " + msg, *args)


_DECOY_SEG_MARKERS = (
    "tiktokcdn.com/ad-site",
    "/ad-site-i18n-",
)


def _is_decoy_seg(seg_url: str) -> bool:
    """Segmen palsu: iklan/statis dari CDN iklan TikTok (URL ber-ekstensi .image)."""
    path = seg_url.split("?", 1)[0]
    return any(m in seg_url for m in _DECOY_SEG_MARKERS) or path.endswith(".image")


def _is_decoy_playlist(text: str) -> bool:
    """Playlist decoy = SEMUA baris datanya segmen palsu (bukan video asli)."""
    data = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not data:
        return False
    return all(_is_decoy_seg(s) for s in data)


def _fetch_segments(master_url: str, referer: str) -> list:
    """Ambil daftar URL segmen .ts dari master -> variant playlist (kualitas terbaik)."""
    h = {"User-Agent": UA, "Referer": referer or master_url}

    def _get_playlist(url: str):
        last_err = None
        for attempt in range(3):
            try:
                r = curl_requests.get(url, headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT)
                if r.status_code == 200:
                    if _is_decoy_playlist(r.text):
                        raise RuntimeError("Playlist decoy terdeteksi (bukan video asli)")
                    return r.text
                last_err = f"HTTP {r.status_code}"
            except RuntimeError:
                raise
            except Exception as e:
                last_err = repr(e)
            time.sleep(1.0)
        raise RuntimeError(f"Gagal mengambil playlist setelah 3 percobaan ({last_err})")

    master_text = _get_playlist(master_url)

    def _parse(text, base_url):
        best_bw = -1
        best_uri = None
        pending_bw = None
        segs = []
        for raw in text.splitlines():
            ln = raw.strip()
            if not ln:
                continue
            if ln.startswith("#EXT-X-STREAM-INF"):
                mb = re.search(r"BANDWIDTH=(\d+)", ln)
                pending_bw = int(mb.group(1)) if mb else 0
                continue
            if ln.startswith("#"):
                continue
            if pending_bw is not None:
                if pending_bw > best_bw:
                    best_bw = pending_bw
                    best_uri = ln
                pending_bw = None
            else:
                segs.append(ln)
        return best_uri, segs

    variant_uri, segs = _parse(master_text, master_url)
    is_encrypted = ("#EXT-X-KEY" in master_text and "AES" in master_text.upper())

    if variant_uri:
        variant_url = urljoin(master_url, variant_uri)
        variant_text = _get_playlist(variant_url)
        if "#EXT-X-KEY" in variant_text and "AES" in variant_text.upper():
            is_encrypted = True
        _, segs = _parse(variant_text, variant_url)
        base = variant_url
    else:
        base = master_url

    if is_encrypted:
        # Jika HLS terenkripsi (AES), kembalikan penanda khusus supaya download dilakukan via ffmpeg
        return ["__AES_HLS__", base]

    urls = []
    for ln in segs:
        if ln.startswith(("http://", "https://")):
            urls.append(ln)
        else:
            urls.append(urljoin(base, ln))
    if not urls:
        raise RuntimeError("Tidak ada segmen video di playlist")
    _dbg("segments found | count=%s", len(urls))
    return urls


def _download_one_segment(url: str, referer: str, out_path: str) -> int:
    h = {"User-Agent": UA, "Referer": referer}
    last_err = None
    for attempt in range(_SEG_RETRIES):
        try:
            r = curl_requests.get(url, headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT)
            if r.status_code == 200 and r.content:
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return len(r.content)
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"Gagal mengunduh segmen ({last_err})")


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
    except Exception:
        pass


async def _download_segments(urls: list, referer: str, work_dir: str, bot, chat_id, status_msg_id, title_text) -> list:
    sem = asyncio.Semaphore(max(1, _SEG_CONCURRENCY))
    total = len(urls)
    done = 0
    total_bytes = 0
    lock = asyncio.Lock()
    emit_lock = asyncio.Lock()
    last_edit = -10.0
    stats = TransferStats()

    async def _emit(force: bool = False):
        nonlocal last_edit
        now = time.monotonic()
        stats.sample(total_bytes, now=now)

        if done > 0 and total > 0:
            # Proyeksi total byte dari rata-rata segmen yang sudah selesai.
            # Dijaga agar tidak pernah turun di bawah byte yang sudah terunduh
            # (monoton naik), supaya bar/persentase tidak loncat-loncat atau >100%.
            est = int(total_bytes / done * total)
            stats.total = max(est, total_bytes, 1)

        interval = _FAST_INTERVAL if stats.speed_bps >= _FAST_SPEED_BPS else _SLOW_INTERVAL

        if stats.should_log(PROGRESS_LOG_INTERVAL, now=now):
            stats.log("RetroTube download", label=title_text)
        if not status_msg_id or (not force and (now - last_edit) < interval):
            return

        last_edit = now
        if done >= total:
            # Semua segmen selesai: angka final, jangan biarkan >100%.
            pct = 100.0 if total > 0 else 0.0
        else:
            pct = (done * 100.0 / total) if total > 0 else 0.0
        text = render_progress_text(
            title_text,
            downloaded=stats.downloaded,
            total=stats.total,
            speed_bps=stats.speed_bps,
            eta_seconds=stats.eta_seconds,
            pct=pct,
        )
        await _safe_edit_status(bot, chat_id, status_msg_id, text)

    async def worker(idx: int, seg_url: str) -> str:
        nonlocal done, total_bytes
        seg_path = os.path.join(work_dir, f"seg_{idx:05d}.ts")
        async with sem:
            size = await asyncio.to_thread(_download_one_segment, seg_url, referer, seg_path)
        async with lock:
            done += 1
            total_bytes += size
            exceed = total_bytes > MAX_TG_SIZE
        if exceed:
            raise FileSizeLimitExceeded("File melebihi batas 2GB")
        async with emit_lock:
            await _emit()
        return seg_path

    async with emit_lock:
        await _emit(force=True)
    results = await asyncio.gather(*(worker(i, u) for i, u in enumerate(urls)))
    async with emit_lock:
        await _emit(force=True)
    stats.log_done("RetroTube download", label=title_text, size=total_bytes)
    return list(results)


def _concat(segment_files: list, out_path: str, work_dir: str, audio_only: bool = False) -> str:
    concat_list = os.path.join(work_dir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in segment_files:
            safe = os.path.abspath(p).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat_list]
    if audio_only:
        cmd += ["-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path]
    else:
        cmd += ["-c", "copy", "-movflags", "faststart", out_path]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=_FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timeout setelah {_FFMPEG_TIMEOUT}s") from e
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path


def _extract_audio(src_path: str, out_path: str) -> str:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=_FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timeout setelah {_FFMPEG_TIMEOUT}s") from e
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path


def _download_direct(url: str, referer: str, out_path: str) -> int:
    h = {"User-Agent": UA, "Referer": referer}
    r = curl_requests.get(url, headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT, stream=True)
    
    # Handle Google Drive virus scan confirmation
    if "drive.google.com" in url:
        ct = r.headers.get("content-type", "")
        if "text/html" in ct:
            head = r.content
            text = head.decode("utf-8", "ignore")
            m = re.search(r'href="(/uc\?export=download[^"]+confirm=[^"]+)"', text)
            if m:
                url2 = "https://drive.google.com" + m.group(1).replace("&amp;", "&")
                r = curl_requests.get(url2, headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT, stream=True)
                if r.status_code != 200 or not r.content:
                    raise RuntimeError(f"Gagal mengunduh GDrive setelah konfirmasi ({r.status_code})")
            else:
                if "quota" in text.lower() or "limit" in text.lower():
                    raise RuntimeError("Limit kuota Google Drive tercapai")
                raise RuntimeError("Gagal mem-bypass konfirmasi Google Drive")

    if r.status_code != 200:
        raise RuntimeError(f"Gagal mengunduh file ({r.status_code})")
    
    total = 0
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                total += len(chunk)
                if total > MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("File melebihi batas 2GB")
    if total <= 0:
        raise RuntimeError(f"Gagal mengunduh file (kosong)")
    return total


def _download_gdrive(gid: str, out_path: str) -> int:
    """Unduh video dari Google Drive lewat endpoint uc?export=download.

    Dipakai untuk player yang mem-proxy Google Drive (mis. fbplay.vip /embed/video/<GDRIVE_ID>)
    dan mengirim playlist HLS palsu (segmen PNG 1x1) saat streaming dari datacenter.
    """
    url = f"https://drive.usercontent.google.com/download?id={gid}&export=download"
    h = {"User-Agent": UA}
    r = curl_requests.get(url, headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT, stream=True)
    ct = (r.headers.get("content-type") or "").lower()

    # Halaman konfirmasi virus scan untuk file besar.
    if "text/html" in ct:
        text = r.content.decode("utf-8", "ignore")
        if "quota" in text.lower():
            raise RuntimeError("Kuota Google Drive terlampaui")
        token = None
        m = re.search(r'name="confirm"\s+value="([^"]+)"', text)
        if m:
            token = m.group(1)
        if not token:
            m = re.search(r'href="(/uc\?export=download[^"]*confirm=[^"]+)"', text)
            if m:
                r = curl_requests.get(
                    "https://drive.google.com" + m.group(1).replace("&amp;", "&"),
                    headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT, stream=True,
                )
                if r.status_code == 200 and r.content:
                    with open(out_path, "wb") as f:
                        f.write(r.content)
                    if os.path.getsize(out_path) > MAX_TG_SIZE:
                        raise FileSizeLimitExceeded("File melebihi batas 2GB")
                    return os.path.getsize(out_path)
                raise RuntimeError("Gagal mengunduh Google Drive (konfirmasi gagal)")
        else:
            r = curl_requests.get(
                "https://drive.usercontent.google.com/download",
                params={"id": gid, "export": "download", "confirm": token},
                headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT, stream=True,
            )
            ct = (r.headers.get("content-type") or "").lower()

    if r.status_code != 200 or "video" not in ct:
        raise RuntimeError(f"Google Drive tidak mengembalikan video ({r.status_code}, {ct or 'no ct'})")

    total = 0
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                total += len(chunk)
                if total > MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("File melebihi batas 2GB")
    if total <= 0:
        raise RuntimeError("Google Drive mengembalikan file kosong")
    return total


def _download_thumb(thumb_url: str, out_path: str):
    if not thumb_url:
        return None
    try:
        r = curl_requests.get(thumb_url, headers={"User-Agent": UA}, impersonate="chrome", timeout=15)
        if r.status_code == 200 and r.content:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception as e:
        _dbg("thumb gagal | %r", e)
    return None
