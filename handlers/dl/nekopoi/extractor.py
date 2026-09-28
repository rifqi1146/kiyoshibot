"""Scraper Nekopoi (nekopoi.care) — mandiri tanpa yt-dlp.

FLOW SCRAPER NEKOPOI
--------------------
1. `scrape_post(url)` -> ambil halaman post (curl_cffi, impersonate Chrome).
2. Baca `og:title` (judul) dan `og:image` (thumbnail).
3. Kumpulkan semua `<iframe src>` -> daftar embed; host streampoi diprioritaskan
   (jalur HLS mandiri, terbukti), playmogo/DoodStream (Turnstile) tetap di
   belakang dan untuk sekarang memang gagal -> RuntimeError jelas.
4. `probe_stream(emb_url, referer=post_url)` -> buka embed -> cari skrip packer
   Dean Edwards `eval(function(p,a,c,k,e,d){...})` -> unpack dengan
   `handlers.dl.retrotube.packer._unpack_eval` (Python murni, tanpa node)
   -> baca `file:"...master.m3u8"` dari kode hasil unpack.
5. `list_resolutions(master_url, referer)` -> parse `#EXT-X-STREAM-INF` ->
   daftar varian `[{height, width, bandwidth, url, format_id}]` tertinggi
   dulu. Tinggi dari `RESOLUTION=<w>x<h>`; kalau kosong dipetakan dari sufix
   path (`_l`=360, `_n`=480, `_h`/`_o`=720, `_v`=1080) lalu fallback bandwidth.
   Playlist ter-encrypt (EXT-X-KEY) ditolak -> belum didukung.
6. `download_variant(...)`:
   a. `list_variant_segments` -> segmen `.ts` relatif di-resolve ke absolut;
      ditolak kalau playlist ternyata dekoy (m3u8 di URL segmen / .html).
   b. Unduh paralel `SEG_CONCURRENCY` worker, retry `SEG_RETRIES`,
      timeout `SEG_TIMEOUT`; progress polling byte tiap `PROGRESS_POLL`,
      edit status maksimal tiap `PROGRESS_INTERVAL`, ETA dari rata-rata byte
      per segmen; backoff saat kena RetryAfter (flood control).
   c. Tulis `concat.txt` -> `ffmpeg -f concat -safe 0 -c copy -movflags
      +faststart` -> MP4 -> bersihkan segmen sementara.
7. `download_audio(...)`: segmen -> concat ke .ts sementara -> ffmpeg
   `-vn -acodec libmp3lame -q:a 2` -> MP3.
8. `download_thumb(url, out)` -> unduh og:image (untuk cover MP3).
9. Referer untuk segmen/playlist = origin embed (mis. `https://streampoi.com/`,
   terbukti diterima CDN streamruby; token `?t=` terikat IP+exp).
10. master/variant 404 (file sudah dihapus di CDN) -> RuntimeError berisi
    kode HTTP; TIDAK ada fallback yt-dlp.
"""
import os
import re
import time
import html as html_mod
import asyncio
import logging
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests as curl_requests

from handlers.dl.utils import sanitize_filename, progress_bar, format_size, format_speed, format_eta
from handlers.dl.retrotube.packer import _PACKER_RE, _unpack_eval

from .constants import (
    UA,
    _HTTP_TIMEOUT,
    DEBUG_NEKOPOI,
    EMBED_HOST_STREAMPOI,
    SEG_CONCURRENCY,
    SEG_RETRIES,
    SEG_TIMEOUT,
    PROGRESS_POLL,
    PROGRESS_MIN_INTERVAL,
    PROGRESS_MAX_INTERVAL,
    PROGRESS_TARGET_EDITS,
    FFMPEG_TIMEOUT,
    VARIANT_LABEL,
)

log = logging.getLogger(__name__)


def _dbg(msg, *args):
    if DEBUG_NEKOPOI:
        log.warning("NEKOPOIDBG | " + msg, *args)


def _flood_wait(error) -> float:
    """Deteksi RetryAfter / 'Flood control exceeded' -> detik yang harus ditunggu."""
    text = str(error or "")
    m = re.search(r"[Rr]etry in (\d+)", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*seconds", text)
    if m:
        return float(m.group(1))
    return 0.0


async def safe_edit_status(bot, chat_id, status_msg_id, text: str) -> float:
    if not status_msg_id:
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
            log.warning("Nekopoi progress flood control | wait=%.0fs", wait)
        return wait


def _meta(html_text: str, prop: str) -> str:
    for pat in (
        rf'<meta\s+property="{prop}"\s+content="([^"]*)"',
        rf'<meta\s+content="([^"]*)"\s+property="{prop}"',
    ):
        m = re.search(pat, html_text, re.I)
        if m and m.group(1):
            return html_mod.unescape(m.group(1).strip())
    return ""


def _collect_embeds(html_text: str) -> list:
    """Daftar iframe embed; streampoi (jalur HLS mandiri) diprioritaskan."""
    seen: set = set()
    order: list = []
    for m in re.finditer(r"<iframe[^>]+(?:src|data-src)=[\"'](https?://[^\"']+)[\"']", html_text, re.I):
        u = m.group(1).strip()
        if u in seen:
            continue
        seen.add(u)
        order.append(u)

    def host(u: str) -> str:
        return (urlsplit(u).hostname or "").lower()

    pref, rest = [], []
    for u in order:
        h = host(u)
        if any(h == d or h.endswith("." + d) for d in EMBED_HOST_STREAMPOI):
            pref.append(u)
        else:
            rest.append(u)
    return pref + rest


def scrape_post(url: str) -> dict:
    """-> {title, thumbnail, embeds:[url]}"""
    r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=_HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} saat mengambil halaman Nekopoi")
    html_text = r.text
    title = _meta(html_text, "og:title")
    if not title:
        m = re.search(r"<title>([^<]+)</title>", html_text, re.I)
        title = html_mod.unescape(m.group(1)).strip() if m else "Nekopoi"
    thumb = _meta(html_text, "og:image")
    embeds = _collect_embeds(html_text)
    _dbg("post scraped | title=%r embeds=%s", title, len(embeds))
    return {"title": title, "thumbnail": thumb, "embeds": embeds}


def _get(url: str, referer: str = "") -> str:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(3):
        try:
            r = curl_requests.get(url, headers=headers, impersonate="chrome", timeout=_HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except Exception as e:
            last = repr(e)
        time.sleep(1.0)
    raise RuntimeError(f"Gagal mengambil sumber Nekopoi ({last}) | {urlsplit(url).hostname}")


def embed_origin(emb_url: str) -> str:
    """https://streampoi.com/embed-x.html -> https://streampoi.com/"""
    p = urlsplit(emb_url)
    return f"{p.scheme}://{p.netloc}/"


def probe_stream(emb_url: str, referer: str) -> str:
    """Buka embed -> unpack packer -> URL master.m3u8."""
    text = _get(emb_url, referer=referer)
    m = _PACKER_RE.search(text)
    if not m:
        raise RuntimeError("Skrip packer tidak ditemukan di embed")
    unpacked = _unpack_eval(m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|"))
    fm = re.search(r"file\s*:\s*\"([^\"]+)\"", unpacked) or re.search(r"file\s*:\s*'([^']+)'", unpacked)
    if not fm:
        raise RuntimeError("URL master.m3u8 tidak ditemukan di embed")
    master = fm.group(1)
    _dbg("master found | %s", master)
    return master


def _variant_label(uri: str, bandwidth: int) -> int:
    """Tinggi varian fallback kalau RESOLUTION tidak ada di playlist."""
    m = re.search(r"_([lnhov])(?:/|$)", urlsplit(uri).path)
    if m and m.group(1) in VARIANT_LABEL:
        return VARIANT_LABEL[m.group(1)]
    if bandwidth >= 900000:
        return 720
    if bandwidth >= 600000:
        return 480
    if bandwidth >= 300000:
        return 360
    return 240


def _reject_encrypted(text: str, what: str):
    if "#EXT-X-KEY" in text:
        raise RuntimeError(f"Playlist {what} HLS terenkripsi (AES), belum didukung")


def _parse_master(master_url: str, text: str) -> list:
    """-> [{height, width, bandwidth, url, format_id}] urut tertinggi dulu."""
    variants, pending = [], None
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("#EXT-X-STREAM-INF"):
            mb = re.search(r"BANDWIDTH=(\d+)", ln)
            mr = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
            pending = {
                "bandwidth": int(mb.group(1)) if mb else 0,
                "width": int(mr.group(1)) if mr else 0,
                "height": int(mr.group(2)) if mr else 0,
            }
            continue
        if ln.startswith("#"):
            continue
        if pending is None:
            continue
        uri = urljoin(master_url, ln)
        height = pending["height"] or _variant_label(uri, pending["bandwidth"])
        variants.append({
            "height": int(height),
            "width": pending["width"],
            "bandwidth": pending["bandwidth"],
            "url": uri,
            "format_id": str(int(height)),
        })
        pending = None
    variants.sort(key=lambda v: (v["height"], v["bandwidth"]), reverse=True)
    return variants


def list_resolutions(master_url: str, referer: str = "") -> list:
    text = _get(master_url, referer=referer)
    _reject_encrypted(text, "master")
    variants = _parse_master(master_url, text)
    if not variants:
        raise RuntimeError("Tidak ada varian resolusi di playlist Nekopoi")
    _dbg("resolutions | count=%s heights=%s", len(variants), [v["height"] for v in variants])
    return variants


def _is_decoy_seg(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".html")


def list_variant_segments(variant_url: str, referer: str = "") -> list:
    """Playlist varian -> daftar URL segmen .ts absolut."""
    text = _get(variant_url, referer=referer)
    _reject_encrypted(text, "variant")
    segs = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        seg_url = urljoin(variant_url, ln)
        if _is_decoy_seg(seg_url):
            raise RuntimeError("Playlist varian Nekopoi tidak valid (dekoy)")
        segs.append(seg_url)
    if not segs:
        raise RuntimeError("Tidak ada segmen video di playlist Nekopoi")
    return segs


def _download_segment(url: str, referer: str, out_path: str) -> int:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(SEG_RETRIES):
        try:
            r = curl_requests.get(url, headers=headers, impersonate="chrome", timeout=SEG_TIMEOUT)
            if r.status_code == 200 and r.content:
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return len(r.content)
            last = f"HTTP {r.status_code}"
            if r.status_code == 404:
                break
        except Exception as e:
            last = repr(e)
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"Gagal mengunduh segmen Nekopoi ({last})")


def _progress_text(title: str, done_seg: int, total_seg: int, done_bytes: int,
                   total_bytes: int, speed_bps: float, eta_seconds: float | None, extra: str = "") -> str:
    lines = [f"<b>{html_mod.escape(sanitize_filename(title, 80))}</b>"]
    if extra:
        lines.append(f"<code>{html_mod.escape(extra)}</code>")
    lines.append("")
    pct = (done_seg * 100.0 / total_seg) if total_seg > 0 else 0.0
    lines.append(f"<code>{progress_bar(pct)}</code>")
    if total_bytes > 0:
        lines.append(f"<code>{format_size(done_bytes)} / {format_size(total_bytes)}</code>")
    elif done_bytes > 0:
        lines.append(f"<code>{format_size(done_bytes)}</code>")
    if speed_bps > 0:
        lines.append(f"<code>Speed: {format_speed(speed_bps)}</code>")
    if eta_seconds is not None and eta_seconds >= 0 and speed_bps > 0:
        lines.append(f"<code>ETA: {format_eta(eta_seconds)}</code>")
    return "\n".join(lines)


async def _download_segments(
    seg_urls: list,
    work_dir: str,
    referer: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    label: str = "",
) -> tuple:
    """Unduh semua segmen paralel -> (concat_txt_path, total_bytes)."""
    total = len(seg_urls)
    sem = asyncio.Semaphore(SEG_CONCURRENCY)
    state = {"done": 0, "bytes": 0, "total_est": 0}
    lock = asyncio.Lock()
    concat_path = os.path.join(work_dir, "concat.txt")

    async def _one(idx: int, u: str):
        out_path = os.path.join(work_dir, f"seg_{idx:05d}.ts")
        async with sem:
            size = await asyncio.to_thread(_download_segment, u, referer, out_path)
        async with lock:
            state["done"] += 1
            state["bytes"] += size
            state["total_est"] = int(state["bytes"] / state["done"] * total)

    tasks = [asyncio.ensure_future(_one(i, u)) for i, u in enumerate(seg_urls)]

    flood_until = 0.0
    last_edit = -10.0
    last_sample_bytes = 0
    last_sample_ts = time.time()
    start_ts = time.time()
    extra = f"{label} · " if label else ""

    # Interval edit ADAPTIF: server Nekopoi lambat & file besar -> jangan
    # spam Telegram (429). Pakai kecepatan RATA-RATA sejak mulai (stabil,
    # tidak loncat-loncat kayak sampling 0.7s):
    #   interval = clamp(estimasi_durasi_total / TARGET_EDITS, min, max)
    # Contoh: durasi 1000s -> 30s/edit (cap); durasi 150s -> 10s/edit;
    # durasi < 75s -> 5s/edit (floor).
    def _interval_for() -> float:
        total_bytes = state["total_est"] or 0
        avg_speed = state["bytes"] / max(time.time() - start_ts, 0.001)
        if avg_speed <= 0 or total_bytes <= 0:
            return PROGRESS_MIN_INTERVAL
        est_duration = total_bytes / avg_speed
        return max(PROGRESS_MIN_INTERVAL, min(PROGRESS_MAX_INTERVAL,
                                              est_duration / max(PROGRESS_TARGET_EDITS, 1)))

    if status_msg_id:
        await safe_edit_status(
            bot, chat_id, status_msg_id,
            _progress_text(title, 0, total, 0, 0, 0.0, None, extra.rstrip(" · ")),
        )
        last_edit = time.time()

    finished_count = 0
    while finished_count < total:
        await asyncio.sleep(PROGRESS_POLL)
        finished_count = sum(1 for t in tasks if t.done())

        now = time.time()
        elapsed = max(now - last_sample_ts, 0.001)
        speed_bps = max(state["bytes"] - last_sample_bytes, 0) / elapsed
        last_sample_bytes = state["bytes"]
        last_sample_ts = now

        # Tampilkan speed rata-rata (stabil) alih-alih sampling 0.7s,
        # sementara ETA tetap dari sisa estimasi / rata-rata.
        avg_speed = state["bytes"] / max(now - start_ts, 0.001)
        remaining = max(state["total_est"] - state["bytes"], 0)
        eta = (remaining / avg_speed) if avg_speed > 0 else None

        if status_msg_id and now >= flood_until and (now - last_edit >= _interval_for() or last_edit < 0):
            wait = await safe_edit_status(
                bot, chat_id, status_msg_id,
                _progress_text(title, state["done"], total, state["bytes"],
                               state["total_est"], avg_speed, eta, extra.rstrip(" · ")),
            )
            last_edit = now
            if wait:
                flood_until = now + wait

    errors = [t.exception() for t in tasks if t.done() and not t.cancelled() and t.exception()]
    if errors:
        raise RuntimeError(str(errors[0]))

    # Path ditulis sebagai basename karena demuxer concat ffmpeg me-resolve
    # path relatif terhadap lokasi file concat itu sendiri (bukan CWD).
    with open(concat_path, "w", encoding="utf-8") as f:
        for i in range(total):
            f.write(f"file 'seg_{i:05d}.ts'\n")
    return concat_path, state["bytes"]


def _run_ffmpeg(cmd: list) -> None:
    import subprocess
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=FFMPEG_TIMEOUT)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg gagal: {(res.stderr or '').strip()[-400:]}")


def _cleanup_segments(work_dir: str, count: int, concat_path: str):
    try:
        for i in range(count):
            p = os.path.join(work_dir, f"seg_{i:05d}.ts")
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(concat_path):
            os.remove(concat_path)
    except OSError:
        pass


async def download_variant(
    variant_url: str,
    out_path: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    label: str = "",
    referer: str = "",
) -> int:
    """Unduh varian HLS -> remux MP4 (copy, tanpa re-encode). Return ukuran."""
    seg_dir = out_path + ".segs"
    os.makedirs(seg_dir, exist_ok=True)
    try:
        seg_urls = await asyncio.to_thread(list_variant_segments, variant_url, referer)
        _dbg("segments | %s", len(seg_urls))
        concat_path, _ = await _download_segments(
            seg_urls, seg_dir, referer, bot, chat_id, status_msg_id, title, label
        )
        await safe_edit_status(
            bot, chat_id, status_msg_id,
            _progress_text(title, len(seg_urls), len(seg_urls), 0, 0, 0.0, None,
                           (f"{label} · " if label else "") + "Muxing..."),
        )
        await asyncio.to_thread(_run_ffmpeg, [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_path,
            "-c", "copy", "-movflags", "+faststart", out_path,
        ])
        if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
            raise RuntimeError("Gagal merangkai video Nekopoi (file kosong)")
        return os.path.getsize(out_path)
    finally:
        import shutil
        shutil.rmtree(seg_dir, ignore_errors=True)


async def download_audio(
    variant_url: str,
    out_path: str,
    title: str,
    label: str = "",
    referer: str = "",
    bot=None,
    chat_id=None,
    status_msg_id=None,
) -> int:
    """Varian HLS -> MP3 (libmp3lame) tanpa re-encode video."""
    seg_dir = out_path + ".segs"
    os.makedirs(seg_dir, exist_ok=True)
    tmp_ts = out_path + ".src.ts"
    try:
        seg_urls = await asyncio.to_thread(list_variant_segments, variant_url, referer)
        concat_path, _ = await _download_segments(
            seg_urls, seg_dir, referer, bot, chat_id, status_msg_id, title, label
        )
        await asyncio.to_thread(_run_ffmpeg, [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_path,
            "-c", "copy", tmp_ts,
        ])
        await asyncio.to_thread(_run_ffmpeg, [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", tmp_ts, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path,
        ])
        if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
            raise RuntimeError("Gagal membuat MP3 dari Nekopoi")
        return os.path.getsize(out_path)
    finally:
        import shutil
        if os.path.exists(tmp_ts):
            try:
                os.remove(tmp_ts)
            except OSError:
                pass
        shutil.rmtree(seg_dir, ignore_errors=True)


def download_thumb(url: str, out_path: str) -> str | None:
    if not url:
        return None
    try:
        r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=_HTTP_TIMEOUT)
        if r.status_code == 200 and r.content:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception as e:
        log.debug("Nekopoi thumb download failed | url=%s err=%r", url, e)
    return None
