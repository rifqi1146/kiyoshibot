"""Extractor generic untuk situs WordPress/RetroTube yang menanamkan embed JW Player
lulustream (mis. bokepcrot.*, lendirqu.stream).

Alur:
  1. Ambil halaman post -> judul (og:title), thumbnail (og:image), dan SEMUA kandidat
     URL embed (itemprop="embedUrl" + <iframe src/data-src>). Host lulustream
     (lulust.com / lulustream.com) diprioritaskan karena server lain (mis. miaw.lol)
     memakai player ter-obfuscate + video decoy.
  2. Coba tiap kandidat -> unpack JS packer (Dean Edwards) -> dapat master.m3u8, atau
     mp4 langsung (bukan decoy).
  3. CDN HLS-nya menolak TLS non-browser (403 untuk urllib/yt-dlp/ffmpeg). Jadi
     master/variant m3u8 dan segmen .ts diunduh via curl_cffi (impersonate="chrome"),
     lalu digabung dengan ffmpeg concat demuxer -> MP4 (atau MP3 kalau fmt_key=mp3).

Catatan: semua panggilan curl_cffi bersifat blocking, dijalankan lewat asyncio.to_thread.
"""
import os
import re
import time
import json
import uuid
import base64
import shutil
import logging
import subprocess

import asyncio

from urllib.parse import urljoin, urlsplit, urlunsplit

from curl_cffi import requests as curl_requests

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded, progress_bar

log = logging.getLogger(__name__)

DEBUG_RETROTUBE = os.getenv("RETROTUBE_DEBUG", "0").strip().lower() in ("1", "true", "on", "yes")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

RETROTUBE_DOMAINS = (
    "bokepcrot.gives",
    "bokepcrot.land",
    "bokepcrot.quest",
    "bokepcrot.com",
    "bokepcrot.net",
    "bokepcrot.xyz",
    "lendirqu.stream",
    "lendirqu.wtf",
    "lendirqu.com",
    "bokepindoh.design",
    "bokepindoh.xxx",
    "bokepinfo.today",
    "bokepinfo.info",
    "indobocil.com",
    "lendirqu.surf",
    "bocilterbaru.surf",
    "rajabocil.surf",
    "abgindoterbaru.com",
    "kangencoli.com",
    "ksatriabokep.com",
    "pemburubokep.com",
    "becekku.live",
    "lordbokep.com",
    "bokepnoz.co",
    "bokepbrut.co",
    "bokepcluk.com",
    "bokepjret.net",
    "bokeplik.com",
    "bokeplot.com",
    "bokepmun.com",
    "bokeprit.in",
    "bokepsut.in",
    "bokeptod.pro",
    "bokepud.in",
    "growbokep.co",
)

# Host yang embed-nya didukung khusus (lulustream, mumu, voe/clone). Diprioritaskan.
_PREFERRED_HOSTS = (
    "lulust.com",
    "lulustream.com",
    "luluvdo.com",
    "luluvid.com",
    "mumu.watch",
    "voe.sx",
    "jeremyparticipantanything.com",
    "miaw.lol",
    "lordfile.site",
    "nozstream.site",
    "colistream.site",
    "jretfile.site",
    "likstream.site",
    "filendung.site",
    "munfile.site",
    "ritfile.site",
    "sutfile.site",
    "ngicstream.site",
    "domfile.site",
    "growfile.site",
)

# Host/file yang jelas-jelas bukan video asli (decoy), di-skip.
_DECOY_HOSTS = ("test-videos.co.uk",)

# Grup domain mirror: situs yang sama di beberapa domain (isi post & path identik),
# dipakai sebagai fallback saat embed di domain asal terproteksi/gagal.
_MIRROR_GROUPS = (
    ("lendirqu.stream", "lendirqu.wtf", "lendirqu.com"),
    (
        "bokepcrot.gives",
        "bokepcrot.land",
        "bokepcrot.quest",
        "bokepcrot.com",
        "bokepcrot.net",
        "bokepcrot.xyz",
    ),
    ("bokepindoh.design", "bokepindoh.xxx"),
    ("bokepinfo.today", "bokepinfo.info"),
    (
        "indobocil.com",
        "lendirqu.surf",
        "bocilterbaru.surf",
        "rajabocil.surf",
        "abgindoterbaru.com",
        "kangencoli.com",
        "ksatriabokep.com",
        "pemburubokep.com",
    ),
)

_SEG_CONCURRENCY = int(os.getenv("RETROTUBE_SEG_CONCURRENCY", "5"))
_SEG_RETRIES = int(os.getenv("RETROTUBE_SEG_RETRIES", "3"))
_HTTP_TIMEOUT = int(os.getenv("RETROTUBE_HTTP_TIMEOUT", "30"))
_FFMPEG_TIMEOUT = int(os.getenv("RETROTUBE_FFMPEG_TIMEOUT", "300"))

# Interval edit pesan progress (detik), adaptif terhadap kecepatan download.
# Server lambat (< _FAST_SPEED_BPS) diedit lebih jarang agar tidak "flicker".
_FAST_INTERVAL = float(os.getenv("RETROTUBE_FAST_INTERVAL", "5"))
_SLOW_INTERVAL = float(os.getenv("RETROTUBE_SLOW_INTERVAL", "10"))
_FAST_SPEED_BPS = float(os.getenv("RETROTUBE_FAST_SPEED_BPS", "1000000"))  # 1 MB/s

_PACKER_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)",
    re.S,
)
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _dbg(msg, *args):
    if DEBUG_RETROTUBE:
        log.warning("RTDBG | " + msg, *args)


def _host(url: str) -> str:
    try:
        return (url or "").split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    except Exception:
        return ""


def is_retrotube_url(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in RETROTUBE_DOMAINS)


async def _safe_edit_status(bot, chat_id, status_msg_id, text: str):
    if bot is None or chat_id is None or not status_msg_id:
        return
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=text, parse_mode="HTML")
    except Exception as e:
        _dbg("edit status failed | %r", e)


def _to_base(num: int, base: int) -> str:
    if num == 0:
        return "0"
    out = ""
    while num:
        out = _BASE36[num % base] + out
        num //= base
    return out


def _unpack_eval(packed: str, radix: int, count: int, words: list) -> str:
    for i in range(count - 1, -1, -1):
        if words[i]:
            packed = re.sub(r"\b" + re.escape(_to_base(i, radix)) + r"\b", lambda m, v=words[i]: v, packed)
    return packed


def _deobfuscate_voe_json(html_text: str):
    """Bongkar obfuscation script JSON dari player VOE (seperti miaw.lol)
    menggunakan ROT13 -> replace patterns -> b64 -> shift(3) -> reverse -> b64 -> JSON.
    """
    m = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return None, None
    try:
        arr = json.loads(m.group(1).strip())
        if not (isinstance(arr, list) and arr and isinstance(arr[0], str)):
            return None, None
        obf = arr[0]
        out = []
        for ch in obf:
            o = ord(ch)
            if 65 <= o <= 90:
                out.append(chr(((o - 65 + 13) % 26) + 65))
            elif 97 <= o <= 122:
                out.append(chr(((o - 97 + 13) % 26) + 97))
            else:
                out.append(ch)
        s1 = "".join(out)
        for p in ['@$', '^^', '~@', '%?', '*~', '!!', '#&']:
            s1 = s1.replace(p, '')
        s3 = base64.b64decode(s1 + '=' * ((4 - len(s1) % 4) % 4)).decode('utf-8', 'replace')
        s4 = "".join(chr(ord(c) - 3) for c in s3)
        s5 = s4[::-1]
        s6 = base64.b64decode(s5 + '=' * ((4 - len(s5) % 4) % 4)).decode('utf-8', 'replace')
        cfg = json.loads(s6)
        return cfg.get("source"), cfg.get("direct_access_url")
    except Exception as e:
        _dbg("VOE deobfuscate gagal | %r", e)
        return None, None


def _collect_embed_candidates(html_text: str) -> list:
    """Kumpulkan semua kandidat URL embed, host lulustream diprioritaskan."""
    seen = []
    order = []

    def add(u):
        u = (u or "").strip()
        if not u or not u.lower().startswith(("http://", "https://")):
            return
        if u in seen:
            return
        seen.append(u)
        order.append(u)

    for m in re.finditer(r'itemprop="embedUrl"\s+content="([^"]+)"', html_text, re.I):
        add(m.group(1))
    for m in re.finditer(r'<iframe[^>]+(?:src|data-src)="(https?://[^"]+)"', html_text, re.I):
        add(m.group(1))

    pref = [u for u in order if any(_host(u) == h or _host(u).endswith("." + h) for h in _PREFERRED_HOSTS)]
    rest = [u for u in order if u not in pref]
    return pref + rest


def _scrape_post(url: str) -> tuple:
    """-> (title, thumb_url, [embed_candidates]) dari halaman post."""
    r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=_HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} saat mengambil halaman")
    html_text = r.text

    title = None
    for pat in (
        r'<meta\s+property="og:title"\s+content="([^"]+)"',
        r'<meta\s+content="([^"]+)"\s+property="og:title"',
        r"<title>([^<]+)</title>",
    ):
        m = re.search(pat, html_text, re.I)
        if m:
            title = m.group(1).strip()
            break
    title = title or "Video"

    thumb = None
    for pat in (
        r'<meta\s+property="og:image"\s+content="([^"]+)"',
        r'<meta\s+content="([^"]+)"\s+property="og:image"',
    ):
        m = re.search(pat, html_text, re.I)
        if m:
            thumb = m.group(1).strip()
            break

    candidates = _collect_embed_candidates(html_text)
    _dbg("post scraped | title=%r candidates=%s", title, candidates)
    return title, thumb, candidates


def _mirror_urls(raw_url: str) -> list:
    """Kembalikan URL post yang sama di domain mirror (path identik), tanpa domain asal."""
    try:
        parts = urlsplit(raw_url)
    except Exception:
        return []
    host = (parts.netloc or "").lower()
    if not host:
        return []
    for group in _MIRROR_GROUPS:
        if any(host == g or host.endswith("." + g) for g in group):
            return [
                urlunsplit((parts.scheme, g, parts.path, parts.query, parts.fragment))
                for g in group
                if g != host
            ]
    return []


def _resolve_embed(embed_url: str, referer: str):
    """-> (kind, media_url, thumb) dengan kind di {'hls','mp4'}, atau (None,None,None)."""
    
    # Bypass DDoS-Guard untuk voe.sx dengan melakukan rewrite ke domain clone-nya.
    # Clone (seperti miaw.lol) tidak dilindungi DDG, sehingga script JSON bisa di-extract.
    embed_url = re.sub(r'https?://(?:www\.)?voe\.sx/e/', 'https://jeremyparticipantanything.com/e/', embed_url)

    text = None
    for attempt in range(2):
        try:
            r = curl_requests.get(
                embed_url,
                headers={"User-Agent": UA, "Referer": referer or embed_url},
                impersonate="chrome",
                timeout=_HTTP_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 200:
                text = r.text
                break
            _dbg("resolve non-200 | %s %s", embed_url, r.status_code)
        except Exception as e:
            _dbg("resolve fetch gagal (attempt %s) | %s %r", attempt + 1, embed_url, e)
            time.sleep(1.0)
    if not text:
        return None, None, None
    m = _PACKER_RE.search(text)
    if m:
        try:
            text = _unpack_eval(m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|"))
        except Exception as e:
            _dbg("unpack gagal | %s %r", embed_url, e)

    # VOE / kloningnya (miaw.lol): config JSON ter-obfuscate -> m3u8 (source) / mp4.
    voe_hls, voe_mp4 = _deobfuscate_voe_json(text)
    if voe_hls:
        return "hls", voe_hls, None
    if voe_mp4:
        return "mp4", voe_mp4, None

    # mumu.watch: MASTER_URL disimpan plaintext di JS dengan slash ter-escape (\/).
    if "mumu.watch" in embed_url or "m-cdn.video" in text:
        mm = re.search(r'MASTER_URL\s*=\s*"([^"]+)"', text)
        if mm:
            return "hls", mm.group(1).replace("\\/", "/"), None
        mm2 = re.search(r'https?:\\/\\/[^"\']+?master\.m3u8', text)
        if mm2:
            return "hls", mm2.group(0).replace("\\/", "/"), None

    # HTML5 <video>/<source> (mis. lordfile.site): mp4 langsung, URL bisa berisi
    # spasi mentah di nama file -> encode agar bisa diunduh.
    for m in re.finditer(r'<(?:source|video)\b[^>]*\bsrc=["\']([^"\']+)["\']', text, re.I):
        u = m.group(1).strip()
        if re.search(r"\.(?:mp4|m3u8|webm)(?:\?|#|$)", u, re.I):
            u = u.replace(" ", "%20")
            return ("hls" if ".m3u8" in u.lower() else "mp4"), u, None

    m3u8 = re.findall(r'file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', text)
    if not m3u8:
        m3u8 = re.findall(r'https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*', text)
    if m3u8:
        thumbs = re.findall(r'image\s*:\s*["\'](https?://[^"\']+)["\']', text)
        return "hls", m3u8[0], (thumbs[0] if thumbs else None)

    mp4 = re.findall(r'file\s*:\s*["\'](https?://[^"\']+\.mp4[^"\']*)["\']', text)
    if not mp4:
        mp4 = re.findall(r'https?://[^"\'\s\\]+\.mp4[^"\'\s\\]*', text)
    for u in mp4:
        if any(_host(u) == d or _host(u).endswith("." + d) for d in _DECOY_HOSTS):
            continue
        return "mp4", u, None

    _dbg("no media | %s", embed_url)
    return None, None, None


def _fetch_segments(master_url: str, referer: str) -> list:
    """Ambil daftar URL segmen .ts dari master -> variant playlist (kualitas terbaik)."""
    h = {"User-Agent": UA, "Referer": referer or master_url}
    
    def _get_playlist(url: str):
        last_err = None
        for attempt in range(3):
            try:
                r = curl_requests.get(url, headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT)
                if r.status_code == 200:
                    return r.text
                last_err = f"HTTP {r.status_code}"
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
    if "#EXT-X-KEY" in master_text and "AES" in master_text.upper():
        raise RuntimeError("Playlist HLS terenkripsi (AES), belum didukung")

    if variant_uri:
        variant_url = urljoin(master_url, variant_uri)
        variant_text = _get_playlist(variant_url)
        if "#EXT-X-KEY" in variant_text and "AES" in variant_text.upper():
            raise RuntimeError("Playlist HLS terenkripsi (AES), belum didukung")
        _, segs = _parse(variant_text, variant_url)
        base = variant_url
    else:
        base = master_url

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


def _format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec <= 0:
        return "0 B/s"
    value = float(bytes_per_sec)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{int(value)} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB/s"


async def _download_segments(urls: list, referer: str, work_dir: str, bot, chat_id, status_msg_id, title_text) -> list:
    sem = asyncio.Semaphore(max(1, _SEG_CONCURRENCY))
    total = len(urls)
    done = 0
    total_bytes = 0
    lock = asyncio.Lock()
    emit_lock = asyncio.Lock()
    last_edit = 0.0
    last_pct = -1.0

    start_ts = time.monotonic()
    last_sample_bytes = 0
    last_sample_ts = start_ts
    current_speed = 0.0

    async def _emit(force: bool = False):
        nonlocal last_edit, last_pct, last_sample_bytes, last_sample_ts, current_speed
        if not status_msg_id:
            return

        now = time.monotonic()
        elapsed_sample = now - last_sample_ts
        # Update speed reading every 1s
        if elapsed_sample >= 1.0:
            current_speed = (total_bytes - last_sample_bytes) / elapsed_sample
            last_sample_bytes = total_bytes
            last_sample_ts = now
        elif current_speed == 0.0 and now > start_ts:
            current_speed = total_bytes / (now - start_ts)

        # Jika >= 1 MB/s, edit 5s sekali. Jika < 1 MB/s (lambat), edit 10s sekali.
        interval = _FAST_INTERVAL if current_speed >= _FAST_SPEED_BPS else _SLOW_INTERVAL

        if not force and (now - last_edit) < interval:
            return

        last_edit = now
        pct = done * 100.0 / max(1, total)
        last_pct = pct
        lines = [
            f"<b>{title_text}</b>",
            "",
            f"<code>{progress_bar(pct)}</code>",
        ]
        if current_speed > 0:
            lines.append(f"<code>Speed: {_format_speed(current_speed)}</code>")
        await _safe_edit_status(bot, chat_id, status_msg_id, "\n".join(lines))

    async def worker(idx: int, seg_url: str) -> str:
        nonlocal done, total_bytes
        seg_path = os.path.join(work_dir, f"seg_{idx:05d}.ts")
        async with sem:
            size = await asyncio.to_thread(_download_one_segment, seg_url, referer, seg_path)
        exceed = False
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
    r = curl_requests.get(url, headers=h, impersonate="chrome", timeout=_HTTP_TIMEOUT)
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"Gagal mengunduh file ({r.status_code})")
    with open(out_path, "wb") as f:
        f.write(r.content)
    if os.path.getsize(out_path) > MAX_TG_SIZE:
        raise FileSizeLimitExceeded("File melebihi batas 2GB")
    return os.path.getsize(out_path)


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


async def retrotube_download(
    raw_url,
    fmt_key,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
):
    del format_id, has_audio, known_size
    work_dir = os.path.join(TMP_DIR, f"rt_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    final_path = None
    try:
        if not metadata_ready:
            await _safe_edit_status(bot, chat_id, status_msg_id, "<b>Scraping website...</b>")

        # Scrape domain asal dulu. Mirror hanya di-scrape kalau domain asal
        # tidak menghasilkan embed preferred (hemat network dan jauh lebih cepat).
        title = None
        page_thumb = None
        candidate_pairs = []  # (embed_url, referer)
        seen_cands = set()

        def _has_preferred(pairs):
            return any(
                any(_host(p[0]) == h or _host(p[0]).endswith("." + h) for h in _PREFERRED_HOSTS)
                for p in pairs
            )

        for idx, page_url in enumerate([raw_url] + _mirror_urls(raw_url)):
            # Jika domain asal sudah menghasilkan preferred embed, stop (tidak perlu scrape mirror).
            if idx > 0 and _has_preferred(candidate_pairs):
                break
            try:
                t, thumb, cands = await asyncio.to_thread(_scrape_post, page_url)
            except Exception as e:
                _dbg("scrape gagal | %s %r", page_url, e)
                continue
            if idx == 0 or title is None:
                title = t
            if thumb and not page_thumb:
                page_thumb = thumb
            for c in cands:
                if c not in seen_cands:
                    seen_cands.add(c)
                    candidate_pairs.append((c, page_url))

        if not candidate_pairs:
            raise RuntimeError("URL video (embed) tidak ditemukan di halaman")

        # Dahulukan host yang dikenal andal (lulust, luluvdo, mumu) dari semua mirror
        pref = [p for p in candidate_pairs if any(_host(p[0]) == h or _host(p[0]).endswith("." + h) for h in _PREFERRED_HOSTS)]
        rest = [p for p in candidate_pairs if p not in pref]
        candidate_pairs = pref + rest

        title = sanitize_filename(title or "Video", 100)

        kind = media_url = embed_thumb = embed_url = None
        for cand, refers in candidate_pairs:
            k, mu, th = await asyncio.to_thread(_resolve_embed, cand, refers)
            if mu:
                kind, media_url, embed_thumb, embed_url = k, mu, th, cand
                break
        if not media_url:
            raise RuntimeError("Tidak ada sumber video yang bisa diunduh dari halaman ini")

        if kind == "hls":
            segments = await asyncio.to_thread(_fetch_segments, media_url, embed_url)
            seg_files = await _download_segments(segments, embed_url, work_dir, bot, chat_id, status_msg_id, title)
            if fmt_key == "mp3":
                final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp3")
                await asyncio.to_thread(_concat, seg_files, final_path, work_dir, True)
            else:
                final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp4")
                await asyncio.to_thread(_concat, seg_files, final_path, work_dir, False)
        else:
            # mp4 langsung
            raw_file = os.path.join(work_dir, "direct.mp4")
            await asyncio.to_thread(_download_direct, media_url, embed_url, raw_file)
            if fmt_key == "mp3":
                final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp3")
                await asyncio.to_thread(_extract_audio, raw_file, final_path)
            else:
                final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp4")
                shutil.move(raw_file, final_path)

        result = {"path": final_path, "title": title}

        if fmt_key == "mp3":
            thumb_path = await asyncio.to_thread(
                _download_thumb, page_thumb or embed_thumb, os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_thumb.jpg")
            )
            if thumb_path:
                result["thumb"] = thumb_path
                result["artist"] = title

        log.info(
            "RetroTube sukses | title=%r size=%.2fMB kind=%s fmt=%s",
            title, os.path.getsize(final_path) / 1024 / 1024, kind, fmt_key,
        )
        return result
    except FileSizeLimitExceeded:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    except Exception:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
