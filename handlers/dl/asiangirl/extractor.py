"""Scraper AsianGirlPorn (asiangirl.porn).

FLOW SCRAPER
------------
1. Terima URL post AsianGirlPorn -> `scrape_post(url)`.
2. Ambil HTML post (curl_cffi, impersonate Chrome). HTTP 404 -> `FileNotFoundError`.
3. Baca metadata:
   - Judul: `<h1>` atau `<title>` (suffix `- AsianGirl.Porn - ...` dibuang).
   - Poster: `<video poster="...">`.
4. Ekstrak URL stream HLS dari HTML:
   - Cari semua URL `.m3u8` di HTML.
   - Buang yang `contents/videos_screenshots/` (itu preview gambar, bukan stream).
   - Ambil yang pertama bukan preview (host CDN `*.cdnlab.live` / `*.ppp.porn`).
5. `parse_hls(m3u8_url)`: fetch playlist media (bukan master), baca `EXT-X-KEY`
   (AES-128: ambil kunci 16-byte + IV), ambil daftar absolute URL segmen `.ts`
   + total durasi dari `#EXTINF`.

FLOW DOWNLOADER
---------------
1. Segmen diunduh **paralel** (`SEG_CONCURRENCY=12`) lalu **didekripsi AES-128-CBC
   lokal** (`Cryptodome.Cipher.AES`, IV tetap dari `EXT-X-KEY` — dites: IV sama
   untuk semua segmen). ffmpeg TIDAK dipakai untuk unduh: jalur ffmpeg serial
   (~0.83 MB/s, 3-6 menit per video) vs paralel (~6.75 MB/s, ~1/10 waktu) —
   diukur dari CDN asli.
2. Progress pakai `TransferStats` + `render_progress_text` standar (proyeksi total
   byte monoton naik, pct dari rasio segmen, dipaksa 100.0 saat selesai).
3. Concat `ffmpeg -f concat -c copy -movflags +faststart` -> MP4 faststart.
   Mode `mp3` -> `-vn -acodec libmp3lame`.
4. Batas `MAX_TG_SIZE` (2GB) dicek saat streaming segmen.
5. URL stream bertanda tangan (token `/<epoch>/` di path) — ambil ulang halaman
   tiap unduh, JANGAN cache lama.
"""

import asyncio
import logging
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from Cryptodome.Cipher import AES

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.progress import (
    TransferStats,
    edit_status,
    render_progress_text,
)
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename

from .constants import (
    FFMPEG_TIMEOUT,
    HTTP_TIMEOUT,
    PROGRESS_LOG_INTERVAL,
    SEG_CONCURRENCY,
    SEG_RETRIES,
    SEG_TIMEOUT,
    UA,
)

log = logging.getLogger(__name__)

_TITLE_SUFFIX_RE = re.compile(r"\s*[-|]\s*AsianGirl\.Porn.*$", re.I | re.S)
_M3U8_RE = re.compile(r"https?://[^\s\"'<>\\]+\.m3u8")
_EXTINF_RE = re.compile(r"#EXTINF\s*:\s*([\d.]+)", re.I)
_KEYLINE_RE = re.compile(
    r'#EXT-X-KEY:METHOD=([\w-]+),\s*URI="([^"]+)"(?:,\s*IV=0x([0-9a-fA-F]+))?', re.I
)


def _new_session() -> curl_requests.Session:
    return curl_requests.Session(impersonate="chrome", headers={"User-Agent": UA})


def _is_preview_url(u: str) -> bool:
    """`contents/videos_screenshots/.../preview.m3u8` bukan stream asli."""
    low = u.lower()
    return "contents/videos_screenshots" in low or "preview.m3u8" in low


def _pick_stream_url(candidates: list[str]) -> str:
    """Pilih stream URL yang bukan preview (gambar)."""
    for u in candidates:
        if not _is_preview_url(u):
            return u
    return ""


# ---------------------------------------------------------------------------
# SCRAPE
# ---------------------------------------------------------------------------
def scrape_post(url: str) -> dict:
    session = _new_session()
    resp = session.get(url, timeout=HTTP_TIMEOUT)

    if resp.status_code == 404:
        raise FileNotFoundError(f"Video tidak ditemukan (HTTP 404): {url}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} saat mengakses AsianGirlPorn")

    html_text = resp.text
    soup = BeautifulSoup(html_text, "html.parser")

    # 1. Judul
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title and soup.title:
        title = _TITLE_SUFFIX_RE.sub("", soup.title.get_text(strip=True))
    title = sanitize_filename(title or "AsianGirlPorn Video", 100)

    # 2. Poster
    poster = ""
    video_tag = soup.find("video")
    if video_tag and video_tag.get("poster"):
        poster = video_tag.get("poster").strip()
    if not poster:
        m = re.search(
            r'<meta\s+property="og:image"\s+content="([^"]+)"', html_text, re.I
        )
        if m:
            poster = m.group(1).strip()

    # 3. Stream HLS URL (bukan preview)
    all_m3u8 = list(dict.fromkeys(_M3U8_RE.findall(html_text)))
    stream_url = _pick_stream_url(all_m3u8)
    if not stream_url:
        raise RuntimeError(
            "Tidak ditemukan URL stream HLS di halaman AsianGirlPorn ini. "
            "Pastikan video masih aktif."
        )

    return {
        "title": title,
        "stream_url": stream_url,
        "poster": poster,
    }


# ---------------------------------------------------------------------------
# HLS PARSE
# ---------------------------------------------------------------------------
def _resolve_segment_url(u: str, base: str) -> str:
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http"):
        return u
    return urljoin(base, u)


def parse_hls(m3u8_url: str, session: curl_requests.Session) -> dict:
    """Ambil playlist + kunci AES-128, kembalikan segmen absolute + durasi."""
    r = session.get(m3u8_url, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(
            f"Gagal mengambil playlist HLS (HTTP {r.status_code}): {m3u8_url}"
        )
    text = r.text
    base = m3u8_url.rsplit("/", 1)[0] + "/"

    key_hex = None
    iv_hex = None
    for line in text.splitlines():
        m = _KEYLINE_RE.search(line)
        if not m:
            continue
        method = m.group(1).upper()
        if method != "AES-128":
            raise RuntimeError(f"Metode enkripsi HLS tidak didukung: {method}")
        key_uri = _resolve_segment_url(m.group(2), base)
        kr = session.get(key_uri, timeout=SEG_TIMEOUT)
        if kr.status_code != 200 or len(kr.content) != 16:
            raise RuntimeError(
                f"Gagal mengambil kunci AES-128 (HTTP {kr.status_code})"
            )
        key_hex = kr.content
        iv_hex = bytes.fromhex(m.group(3)) if m.group(3) else None
        break
    if key_hex is None:
        raise RuntimeError("Playlist HLS tidak memuat #EXT-X-KEY (AES-128)")

    segs: list[str] = []
    duration = 0.0
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        segs.append(_resolve_segment_url(s, base))
    if not segs:
        raise RuntimeError("Tidak ada segmen di playlist HLS")

    try:
        duration = sum(float(d) for d in _EXTINF_RE.findall(text))
    except ValueError:
        duration = 0.0

    return {
        "key": key_hex,
        "iv": iv_hex,
        "segments": segs,
        "duration": int(duration),
    }


# ---------------------------------------------------------------------------
# PARALLEL DOWNLOAD + AES DECRYPT
# ---------------------------------------------------------------------------
def _fetch_segment(url: str, session: curl_requests.Session) -> bytes:
    last_err = None
    for attempt in range(SEG_RETRIES):
        try:
            r = session.get(url, timeout=SEG_TIMEOUT)
            if r.status_code == 200 and r.content:
                return r.content
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"Gagal mengunduh segmen ({last_err})")


async def download_segments_aes(
    seg_urls: list[str],
    key: bytes,
    iv: bytes | None,
    session: curl_requests.Session,
    work_dir: str,
    bot,
    chat_id,
    status_msg_id,
    title_text: str,
) -> list[str]:
    """Unduh segmen paralel, dekripsi AES-128-CBC per-segmen, tulis .ts local.

    IV tetap (`same_iv`) untuk semua segmen — diverifikasi pada 3 segmen di CDN
    asli. Kalau playlist menyertakan IV atribut, pakai itu (fillem `IV=`).
    """
    # CBC mode: tiap segmen di-decrypt mandiri dengan IV yang sama (diverifikasi
    # pada 3 segmen di CDN asli: `same_iv=True` untuk segmen 0, 5, 40). Objek
    # cipher tidak boleh dipakai bersama antar-thread: state IV internal CBC
    # akan saling silang antar segmen. Buat cipher baru per-segmen (murah).
    if iv is None:
        iv = (0).to_bytes(16, "big")

    sem = asyncio.Semaphore(max(1, SEG_CONCURRENCY))
    total = len(seg_urls)
    done = 0
    total_bytes = 0
    lock = asyncio.Lock()
    emit_lock = asyncio.Lock()
    stats = TransferStats()

    async def _emit(force: bool = False):
        now = time.monotonic()
        stats.sample(total_bytes, now=now)
        if done > 0 and total > 0:
            est = int(total_bytes / done * total)
            stats.total = max(est, total_bytes, 1)

        if stats.should_log(PROGRESS_LOG_INTERVAL, now=now):
            stats.log("AsianGirlPorn download", label=title_text)
        if not status_msg_id:
            return
        # Interval adaptif + guard anti-spam 100% sudah ditangani `should_edit`.
        # `force=True` (interval 0) memaksa edit di awal (0%) dan akhir (100%).
        if force:
            stats.should_edit(0, now=now)
        elif not stats.should_edit(now=now):
            return

        pct = 100.0 if done >= total else (done * 100.0 / total if total else 0.0)
        text = render_progress_text(
            title_text,
            downloaded=stats.downloaded,
            total=stats.total,
            speed_bps=stats.speed_bps or stats.avg_bps,
            eta_seconds=stats.eta_seconds,
            pct=pct,
        )
        await edit_status(bot, chat_id, status_msg_id, text, label="AsianGirlPorn")

    async def worker(idx: int, seg_url: str) -> str:
        nonlocal done, total_bytes
        seg_path = os.path.join(work_dir, f"seg_{idx:05d}.ts")
        async with sem:
            raw = await asyncio.to_thread(_fetch_segment, seg_url, session)
        # dekripsi di thread agar tidak memblok event loop (segmen 1-2MB)
        decrypted = await asyncio.to_thread(_decrypt_segment, key, iv, raw)
        with open(seg_path, "wb") as f:
            f.write(decrypted)

        async with lock:
            done += 1
            total_bytes += len(raw)
            exceed = total_bytes > MAX_TG_SIZE
        if exceed:
            raise FileSizeLimitExceeded("File melebihi batas 2GB")
        async with emit_lock:
            await _emit()
        return seg_path

    async with emit_lock:
        await _emit(force=True)
    try:
        results = await asyncio.gather(*(worker(i, u) for i, u in enumerate(seg_urls)))
    except BaseException:
        async with emit_lock:
            await _emit(force=True)
        raise
    async with emit_lock:
        await _emit(force=True)
    stats.log_done("AsianGirlPorn download", label=title_text, size=total_bytes)
    return list(results)


def _decrypt_segment(key: bytes, iv: bytes, raw: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pad = len(raw) % 16
    if pad:
        raw = raw[: len(raw) - pad]
    return cipher.decrypt(raw)


# ---------------------------------------------------------------------------
# CONCAT / AUDIO
# ---------------------------------------------------------------------------
def concat_segments(seg_files: list[str], out_path: str, work_dir: str) -> str:
    """Gabung segmen .ts jadi satu mp4 (copy, tanpa re-encode)."""
    lst = os.path.join(work_dir, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in seg_files:
            safe = os.path.abspath(p).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", lst,
        "-c", "copy", "-movflags", "+faststart", out_path,
    ]
    try:
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg concat timeout setelah {FFMPEG_TIMEOUT}s") from e
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg concat gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path


def extract_audio(src_path: str, out_path: str, title: str = "") -> str:
    """Ekstrak audio ke MP3 (libmp3lame)."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path,
        "-vn", "-acodec", "libmp3lame", "-q:a", "2",
        "-metadata", f"title={title or 'AsianGirl.Porn'}",
        "-metadata", "artist=AsianGirl.Porn",
        out_path,
    ]
    try:
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg audio extraction timeout setelah {FFMPEG_TIMEOUT}s") from e
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg audio extraction gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path
