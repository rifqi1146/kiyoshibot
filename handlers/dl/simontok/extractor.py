"""Scraper Simontok (simontok.study)

FLOW SCRAPER
------------
1. `scrape_post(url)`: ambil halaman post → judul + URL embed (iframe
   putarin.*/puterin.* — ejaan & TLD berrotasi).
2. `resolve_hls(embed_url)`: halaman embed memuat `window.__PX` = blob terenkripsi
   AES-256-GCM. Kunci TIDAK ada di HTML — diambil sekali-pakai dari
   `<origin>/api/pk?n=<nonce>`. Dekripsi menghasilkan config player JSON
   berisi `file` (URL `/api/hls?t=...`) dan `image` (poster).
   **Origin selalu diambil dinamis dari URL embed** — TLD putarin berotasi
   (biz/xyz/...), memakai origin yang salah membuat `/api/pk` membalas 403.
3. `parse_segments(m3u8_url, referer)`: playlist media HLS (tanpa `#EXT-X-STREAM-INF`,
   tanpa `#EXT-X-KEY`) → daftar segmen `.ts` + total durasi dari `#EXTINF`.

FLOW DOWNLOADER
---------------
1. Segmen diunduh paralel (`SEG_CONCURRENCY`) dengan retry `SEG_RETRIES`.
2. Progress pakai `TransferStats` + `render_progress_text` standar: proyeksi total
   byte di-update tiap segmen dan dijaga monoton naik (`max(est, total_bytes, 1)`),
   bar memakai rasio segmen, dipaksa 100.0 saat semua segmen selesai.
3. Remux `ffmpeg -f concat -c copy` → mp4. Mode `mp3` → `libmp3lame`.
4. Batas `MAX_TG_SIZE` dicek selama streaming segmen.
"""
import os
import re
import json
import time
import uuid
import base64
import logging
import asyncio
import subprocess
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests as curl_requests
from Cryptodome.Cipher import AES

from handlers.dl.constants import MAX_TG_SIZE, TMP_DIR
from handlers.dl.progress import (
    PROGRESS_LOG_INTERVAL,
    TransferStats,
    edit_status,
    render_progress_text,
)
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename

from .constants import (
    EMBED_HOST_MARKERS,
    FFMPEG_TIMEOUT,
    HTTP_TIMEOUT,
    PROGRESS_INTERVAL,
    SEG_CONCURRENCY,
    SEG_RETRIES,
    SEG_TIMEOUT,
    UA,
)

log = logging.getLogger(__name__)

POST_RE = re.compile(r"^https?://(?:www\.)?simontok\.study/([^/?#]+)/?$")
IFRAME_RE = re.compile(r'<iframe[^>]+src="([^"]+)"', re.I)
PX_RE = re.compile(r"window\.__PX=(\{.*?\})")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
EXTINF_RE = re.compile(r"#EXTINF\s*:\s*([\d.]+)", re.I)
EXTMAP_RE = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')


def _origin(url: str) -> str:
    p = urlsplit((url or "").strip())
    return f"{p.scheme}://{p.netloc}"


def _new_session():
    return curl_requests.Session(impersonate="chrome", headers={"User-Agent": UA})


def scrape_post(url: str) -> dict:
    """Ambil halaman post simontok.study → title + embed URL."""
    if not POST_RE.match((url or "").strip()):
        raise ValueError(f"Bukan URL post simontok.study: {url}")

    r = _new_session().get(url.strip(), timeout=HTTP_TIMEOUT)
    if r.status_code == 404:
        raise FileNotFoundError(f"Video tidak ditemukan (HTTP 404): {url}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} saat mengakses simontok.study")

    html = r.text
    title = ""
    m = TITLE_RE.search(html)
    if m:
        title = re.sub(r"\s*[-|]\s*Simontok.*$", "", m.group(1), flags=re.I).strip()

    embed_url = ""
    for cand in IFRAME_RE.findall(html):
        if any(marker in cand for marker in EMBED_HOST_MARKERS):
            embed_url = cand
            break
    if not embed_url:
        raise RuntimeError("Iframe embed putarin/puterin tidak ditemukan di halaman post")

    return {
        "title": sanitize_filename(title or "Simontok Video", 100),
        "embed_url": embed_url,
    }


def _fetch_pk(session, origin: str, nonce: str, referer: str) -> bytes:
    r = session.get(
        f"{origin}/api/pk?n={nonce}",
        timeout=HTTP_TIMEOUT,
        headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"GET /api/pk HTTP {r.status_code}")
    text = (r.text or "").strip()
    if len(text) < 64:
        raise RuntimeError("Respon /api/pk bukan kunci hex yang valid")
    return bytes.fromhex(text[:64])


def resolve_hls(embed_url: str) -> dict:
    """Decrypt config player (AES-256-GCM) → URL m3u8 + poster."""
    origin = _origin(embed_url)
    last_err = None
    for attempt in range(2):
        session = _new_session()
        try:
            r = session.get(embed_url, timeout=HTTP_TIMEOUT,
                            headers={"Referer": "https://simontok.study/"})
            if r.status_code != 200:
                raise RuntimeError(f"GET embed HTTP {r.status_code}")

            m = PX_RE.search(r.text)
            if not m:
                raise RuntimeError("window.__PX tidak ditemukan di halaman embed")
            px = json.loads(m.group(1))
            if not px.get("n") or not px.get("d"):
                raise RuntimeError("config embed tidak punya nonce/ciphertext")

            key = _fetch_pk(session, origin, px["n"], embed_url)
            blob = base64.b64decode(px["d"])
            if len(blob) <= 28:
                raise RuntimeError("ciphertext embed terlalu pendek")
            # Layout GCM: nonce/IV 12 byte | ciphertext | tag 16 byte.
            iv, ct, tag = blob[:12], blob[12:-16], blob[-16:]
            plain = AES.new(key, AES.MODE_GCM, nonce=iv).decrypt_and_verify(ct, tag)
            player = json.loads(plain.decode("utf-8"))

            file_url = player.get("file") or player.get("key") or ""
            if not file_url:
                raise RuntimeError("config player tidak punya file/key")
            if file_url.startswith("/"):
                file_url = origin + file_url
            return {
                "m3u8_url": file_url,
                "poster": player.get("image") or "",
                "origin": origin,
            }
        except Exception as e:
            last_err = e
            log.debug("resolve_hls attempt=%s gagal | origin=%s err=%r", attempt + 1, origin, e)
            time.sleep(1.0)
    raise RuntimeError(f"Gagal membuka config player Simontok ({last_err})")


def parse_segments(m3u8_url: str, referer: str) -> dict:
    """Ambil playlist media → daftar URL segmen + total durasi (detik)."""
    r = _new_session().get(
        m3u8_url,
        timeout=HTTP_TIMEOUT,
        headers={"Referer": referer, "Accept": "application/vnd.apple.mpegurl,*/*"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"GET m3u8 HTTP {r.status_code}")
    if "#EXT-X-KEY" in r.text:
        raise RuntimeError("Playlist HLS terenkripsi (#EXT-X-KEY) — belum didukung")

    duration = sum(float(d) for d in EXTINF_RE.findall(r.text))
    init_seg = EXTMAP_RE.search(r.text)
    if init_seg:
        raise RuntimeError("Playlist HLS fMP4 (#EXT-X-MAP) — belum didukung")

    base = m3u8_url.rsplit("/", 1)[0] + "/"
    segs = []
    for raw in r.text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("//"):
            line = "https:" + line
        elif not line.startswith("http"):
            line = urljoin(base, line)
        segs.append(line)
    if not segs:
        raise RuntimeError("Tidak ada segmen di playlist")

    return {"segments": segs, "duration": int(duration)}


def _download_one_segment(url: str, referer: str, out_path: str) -> int:
    headers = {"User-Agent": UA, "Referer": referer}
    last_err = None
    for attempt in range(SEG_RETRIES):
        try:
            r = curl_requests.get(url, headers=headers, impersonate="chrome",
                                  timeout=SEG_TIMEOUT)
            if r.status_code == 200 and r.content:
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return len(r.content)
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"Gagal mengunduh segmen ({last_err})")


async def download_segments(
    seg_urls: list,
    referer: str,
    work_dir: str,
    bot,
    chat_id,
    status_msg_id,
    title_text: str,
) -> list:
    """Unduh semua segmen paralel dengan progress standar downloader lain."""
    sem = asyncio.Semaphore(max(1, SEG_CONCURRENCY))
    total = len(seg_urls)
    done = 0
    total_bytes = 0
    lock = asyncio.Lock()
    emit_lock = asyncio.Lock()
    stats = TransferStats()

    async def _emit(force: bool = False):
        nonlocal last_edit
        now = time.monotonic()
        stats.sample(total_bytes, now=now)
        if done > 0 and total > 0:
            # Proyeksi total byte dari rata-rata segmen yang sudah selesai,
            # dijaga monoton naik supaya bar/persen tidak pernah >100%.
            est = int(total_bytes / done * total)
            stats.total = max(est, total_bytes, 1)

        if stats.should_log(PROGRESS_LOG_INTERVAL, now=now):
            stats.log("Simontok download", label=title_text)
        if not status_msg_id or (not force and (now - last_edit) < PROGRESS_INTERVAL):
            return

        last_edit = now
        pct = 100.0 if done >= total else (done * 100.0 / total if total else 0.0)
        text = render_progress_text(
            title_text,
            downloaded=stats.downloaded,
            total=stats.total,
            speed_bps=stats.speed_bps,
            eta_seconds=stats.eta_seconds,
            pct=pct,
        )
        await edit_status(bot, chat_id, status_msg_id, text, label="Simontok download")

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

    last_edit = -10.0
    async with emit_lock:
        await _emit(force=True)
    try:
        results = await asyncio.gather(*(worker(i, u) for i, u in enumerate(seg_urls)))
    except BaseException:
        # Edit pesan final (gagal/100%) tetap dikirim sebelum error merambat.
        async with emit_lock:
            await _emit(force=True)
        raise
    async with emit_lock:
        await _emit(force=True)
    stats.log_done("Simontok download", label=title_text, size=total_bytes)
    return list(results)


def concat_segments(seg_files: list, out_path: str, work_dir: str) -> str:
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
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timeout setelah {FFMPEG_TIMEOUT}s") from e
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path


def extract_audio(src_path: str, out_path: str) -> str:
    """Ekstrak audio ke MP3 (libmp3lame) untuk mode mp3."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", src_path,
        "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path,
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timeout setelah {FFMPEG_TIMEOUT}s") from e
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path


def has_video_stream(path: str) -> bool:
    """True kalau file hasil remux memuat stream video yang dikenal ffmpeg.

    Beberapa post simontok memakai codec video yang tidak dikenal build ffmpeg
    (stream terbaca sebagai `bin_data`, ffprobe: "Unsupported codec with id").
    Remux `-c copy` menghasilkan mp4 audio-only. Tanpa cek ini, file rusak
    dikirim diam-diam ke user.
    """
    try:
        res = subprocess.run(
            ["ffprobe", "-hide_banner", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60,
        )
    except Exception:
        return True  # ffprobe gagal -> biarkan download lanjut
    return any(line.strip() for line in (res.stdout or "").splitlines())


def download_thumb(url: str, out_path: str) -> str | None:
    if not url:
        return None
    try:
        r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=20)
        if r.status_code == 200 and r.content:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception as e:
        log.debug("download_thumb Simontok gagal | %r", e)
    return None


def new_work_dir() -> str:
    path = os.path.join(TMP_DIR, f"simontok_{uuid.uuid4().hex[:10]}")
    os.makedirs(path, exist_ok=True)
    return path
