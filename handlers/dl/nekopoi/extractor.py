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
from urllib.parse import urljoin, urlsplit, urlparse

from curl_cffi import requests as curl_requests
from curl_cffi import CurlOpt

from bs4 import BeautifulSoup

from handlers.dl.utils import sanitize_filename, progress_bar, format_size, format_speed, format_eta, FileSizeLimitExceeded
from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.retrotube.packer import _PACKER_RE, _unpack_eval
from handlers.dl.progress import (
    render_progress_text,
    edit_status,
    log_progress,
    log_done,
    TransferStats,
    PROGRESS_EDIT_INTERVAL,
    PROGRESS_LOG_INTERVAL,
)

from .constants import (
    UA,
    _HTTP_TIMEOUT,
    DEBUG_NEKOPOI,
    FORCE_IPV4,
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

# Satu session bersama (thread-local curl handle) dengan IPRESOLVE_V4.
# Kunci IPv4 wajib: token streampoi memuat `i=<ip>` yang harus sama dengan IP
# koneksi ke CDN streamruby (lihat catatan di constants.FORCE_IPV4).
_session = None


def _http():
    """Session curl_cffi terkunci IPv4 (kalau FORCE_IPV4 aktif)."""
    global _session
    if _session is None:
        if FORCE_IPV4:
            _session = curl_requests.Session(
                impersonate="chrome",
                curl_options={CurlOpt.IPRESOLVE: 1},  # CURL_IPRESOLVE_V4
            )
        else:
            _session = curl_requests.Session(impersonate="chrome")
    return _session


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
    r = _http().get(url, headers={"User-Agent": UA}, timeout=_HTTP_TIMEOUT)
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
    return {"title": title, "thumbnail": thumb, "embeds": embeds, "raw_html": html_text}


def _get(
    url: str,
    referer: str = "",
    attempts: int = 3,
    timeout: float | None = None,
) -> str:
    """Ambil halaman (curl_cffi, impersonate Chrome, terkunci IPv4).

    `attempts`/`timeout` bisa diturunkan untuk jalur probe: embed streampoi yang
    origin-nya mati membalas HTTP 522 SETELAH ~40 detik tunggu Cloudflare, jadi
    default 3 percobaan x 45s = ~120 detik cuma buat nunggu host yang jelas
    sedang down (lihat `probe_stream`).
    """
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    to = _HTTP_TIMEOUT if timeout is None else timeout
    last = None
    for attempt in range(max(1, attempts)):
        try:
            r = _http().get(url, headers=headers, timeout=to)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
            # 5xx (522/520/524) = Cloudflare/origin mati, deterministik: retry
            # cuma mengulang tunggu ~40s. Berhenti di percobaan pertama.
            if r.status_code >= 500:
                break
        except Exception as e:
            last = repr(e)
        if attempt < attempts - 1:
            time.sleep(1.0)
    raise RuntimeError(f"Gagal mengambil sumber Nekopoi ({last}) | {urlsplit(url).hostname}")


def _probe_direct_size(url: str, headers: dict) -> int:
    try:
        r = _http().get(url, headers=headers, stream=True, timeout=_HTTP_TIMEOUT)
        sz = int(r.headers.get("Content-Length") or 0)
        r.close()
        return sz
    except Exception:
        return 0


async def download_direct_mp4(url: str, out_path: str, bot, chat_id, status_msg_id, title: str, label: str, headers: dict):
    """Unduh MP4 langsung (HTTP stream) -> out_path. Return ukuran."""
    def _do():
        r = _http().get(url, headers=headers, stream=True, timeout=_HTTP_TIMEOUT)
        if r.status_code not in (200, 206):
            r.close()
            raise RuntimeError(f"HTTP {r.status_code} saat mengunduh direct MP4")
        return r

    resp = await asyncio.to_thread(_do)
    try:
        total = int(resp.headers.get("Content-Length") or 0)
    except Exception:
        total = 0

    if total > MAX_TG_SIZE:
        resp.close()
        raise FileSizeLimitExceeded("Video exceeds 2GB limit. Download canceled.")

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

    # Edit pertama: paksa tampilkan 0% ke Telegram segera setelah Content-Length
    # diketahui, agar user langsung melihat progress bar (bukan diam di status lama).
    if status_msg_id:
        await stats.emit(
            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            title=title, kind="Nekopoi download", label=f"Nekopoi {label}",
            log_interval=0.0, edit_interval=0.0,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if status_msg_id:
            await stats.emit(
                bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                title=title, kind="Nekopoi download", label=f"Nekopoi {label}",
                log_interval=PROGRESS_LOG_INTERVAL,
                edit_interval=_adaptive_edit_interval(stats),
            )
        await asyncio.sleep(PROGRESS_POLL)

    await write_task

    # Edit terakhir: paksa 100% tampil ke Telegram sebelum pindah ke tahap remux/upload.
    if status_msg_id:
        await stats.emit(
            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            title=title, kind="Nekopoi download", label=f"Nekopoi {label}",
            log_interval=0.0, edit_interval=0.0,
        )

    stats.log_done("Nekopoi download", label=f"Nekopoi {label}")
    return os.path.getsize(out_path)



def _adaptive_edit_interval(stats: "TransferStats") -> float:
    """Interval edit ADAPTIF bersama untuk semua jalur Nekopoi.

    Interval = clamp(estimasi_durasi_total / PROGRESS_TARGET_EDITS, min, max)
    memakai kecepatan RATA-RATA sejak mulai (stabil, tidak loncat-loncat).
    Contoh: durasi 1000s -> 30s (cap); 150s -> 10s; <75s -> 5s (floor).

    PENTING: `PROGRESS_MAX_INTERVAL` adalah CAP dari interval adaptif, BUKAN
    interval tetap. Memakainya sebagai `edit_interval` langsung (bug lama di
    `download_direct_mp4`) membuat unduhan Pixeldrain yang selesai ~8 detik
    TIDAK PERNAH mengedit pesan status sama sekali -> progress bar di Telegram
    tidak bergerak walau log terminal jalan.
    """
    total_bytes = stats.total or 0
    avg_speed = stats.avg_bps
    if avg_speed <= 0 or total_bytes <= 0:
        return PROGRESS_MIN_INTERVAL
    est_duration = total_bytes / avg_speed
    return max(
        PROGRESS_MIN_INTERVAL,
        min(PROGRESS_MAX_INTERVAL, est_duration / max(PROGRESS_TARGET_EDITS, 1)),
    )


def embed_origin(emb_url: str) -> str:
    """https://streampoi.com/embed-x.html -> https://streampoi.com/"""
    p = urlsplit(emb_url)
    return f"{p.scheme}://{p.netloc}/"


def _bypass_ouo_form(url: str) -> str | None:
    """Jalur lama: ambil token dari form lalu POST /go/<id> -> /xreallcygo/<id>.

    Sudah tidak jalan sejak ouo.io memasang Cloudflare Turnstile (GET 403
    "Just a moment...", tidak ada form). Tetap dicoba dulu karena murah dan
    akan jalan lagi kalau challenge-nya dilepas.
    """
    tempurl = url.replace("ouo.press", "ouo.io")
    p = urlparse(tempurl)
    oid = tempurl.split('/')[-1]

    sess = curl_requests.Session(impersonate="chrome")
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        res = sess.get(tempurl, headers=headers, timeout=15)
        next_url = f"{p.scheme}://{p.hostname}/go/{oid}"

        for _ in range(3):
            if res.headers.get("Location"):
                return res.headers.get("Location")

            soup = BeautifulSoup(res.content, "html.parser")
            form = soup.form
            if not form:
                break

            inputs = form.find_all("input", {"name": re.compile(r"token$")})
            data = {inp.get("name"): inp.get("value") for inp in inputs}
            data["x-token"] = ""

            post_headers = headers.copy()
            post_headers["content-type"] = "application/x-www-form-urlencoded"
            post_headers["Referer"] = tempurl

            res = sess.post(
                next_url,
                data=data,
                headers=post_headers,
                allow_redirects=False,
                timeout=15
            )
            next_url = f"{p.scheme}://{p.hostname}/xreallcygo/{oid}"

        return res.headers.get("Location")
    except Exception as e:
        log.debug("Gagal bypass ouo.io (form) %s : %s", url, e)
        return None


def _bypass_ouo_stealth(url: str, timeout_ms: int = 60000) -> str | None:
    """Buka shortener di browser stealth Scrapling dan klik sampai redirect.

    Dipakai dua konteks:
    - `ouo.io`/`ouo.press` — menembus Cloudflare Turnstile. Alur halaman:
      `#btn-main` ("I'M A HUMAN") di `<id>` -> `/go/<id>` ("GET LINK") ->
      redirect ke host tujuan. Kedua tombol menunggu Turnstile selesai (class
      `disabled` dilepas), jadi tunggu sampai aktif lalu klik.
    - shortener lain (`linkpoi.me`, dsb.) — markupnya tak diketahui; klik
      kandidat tombol/link pertama yang aktif lalu lihat apakah URL pindah.
    `block_ads=True` wajib: tanpa itu klik bisa mendarat di redirect iklan
    (mis. sgkk8.info) alih-alih host tujuan.
    """
    try:
        from scrapling import StealthyFetcher
    except Exception as e:
        log.debug("Scrapling tidak tersedia untuk bypass ouo: %r", e)
        return None

    start_host = (urlsplit(url).hostname or "").lower()
    result = {"url": None}
    deadline = time.time() + timeout_ms / 1000.0

    def _reached_target(u: str) -> bool:
        """Sukses HANYA kalau sudah keluar dari semua host shortener.

        Jangan pakai `h != start_host`: ouo.io nge-redirect otomatis ke
        ouo.press (sekeluarga) SEBELUM halaman termuat, jadi begitu halaman
        kebuka page.url sudah `ouo.press` padahal tombolnya belum diklik sama
        sekali. Akibatnya URL shortener dikira hasil akhir -> diteruskan ke
        pengecekan pixeldrain di main.py -> "Gagal mem-bypass link Pixeldrain".
        """
        h = (urlsplit(u or "").hostname or "").lower()
        if not h:
            return False
        if h == start_host:
            return False
        return _is_final_target(u)

    def _auto(page):
        page.on("popup", lambda p: p.close())

        def _click_first_available(locator, limit=4):
            n = locator.count()
            for i in range(min(n, limit)):
                el = locator.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    if not el.is_enabled():
                        continue
                    if "disabled" in (el.get_attribute("class") or ""):
                        continue
                except Exception:
                    continue
                try:
                    el.click(force=True)
                    return True
                except Exception:
                    continue
            return False

        def _click_main():
            loc = page.locator("#btn-main")
            if loc.count() == 0:
                return False
            # Tombol ouo nunggu Turnstile selesai -> polling sampai aktif.
            for _ in range(25):
                try:
                    el = loc.first
                    if el.is_enabled() and "disabled" not in (el.get_attribute("class") or ""):
                        break
                except Exception:
                    break
                page.wait_for_timeout(300)
            try:
                loc.first.click(force=True)
            except Exception:
                pass
            return True

        try:
            page.wait_for_timeout(2000)
            for step in range(5):
                if time.time() > deadline:
                    break
                u = page.url
                if _reached_target(u):
                    result["url"] = u
                    return

                clicked = _click_main()
                if not clicked:
                    # Bukan halaman ouo -> coba tombol/link generik.
                    clicked = _click_first_available(
                        page.locator(
                            "#continue, .btn-main, .btn-continue, "
                            "a.btn, button"
                        )
                    )
                if not clicked:
                    break

                # Polling: tunggu redirect selesai sebelum klik lagi, supaya
                # tidak ikut mengeklik elemen di halaman tujuan.
                for _ in range(20):
                    if time.time() > deadline:
                        break
                    page.wait_for_timeout(300)
                    if _reached_target(page.url):
                        result["url"] = page.url
                        return
                page.wait_for_timeout(1500)

            u = page.url
            if _reached_target(u):
                result["url"] = u
        except Exception as e:
            log.debug("Aksi stealth shortener gagal %s : %r", url, e)

    try:
        StealthyFetcher.fetch(
            url, headless=True, timeout=timeout_ms,
            block_ads=True, page_action=_auto,
        )
    except Exception as e:
        log.debug("Stealth fetcher gagal untuk %s : %r", url, e)
        return None
    return result["url"]


def bypass_ouo(url: str) -> str | None:
    """Bypass ouo.io/ouo.press -> url asli (mis. pixeldrain/krakenfiles).

    ouo.io sejak 2026 penuh Cloudflare Turnstile, jadi jalur form murah
    hampir selalu gagal -> fallback browser stealth Scrapling yang benar-benar
    mengklik tombol sampai redirect. Fungsi ini blocking (~20s) kalau lewat
    jalur browser; panggil dari thread (`asyncio.to_thread`).
    """
    host = (urlsplit(url).hostname or "").lower()
    if not (host.endswith("ouo.io") or host.endswith("ouo.press")):
        log.debug("bypass_ouo dipanggil untuk host non-ouo (%s) -> tolak", url)
        return None

    target = _bypass_ouo_form(url)
    if not target:
        log.debug("Jalur form ouo gagal, fallback ke Scrapling | %s", url)
        target = _bypass_ouo_stealth(url)
    if target:
        log.info("Bypass ouo.io sukses | %s -> %s", url, target)
    else:
        log.debug("Gagal bypass ouo.io | %s", url)
    return target


def _is_pixeldrain(url: str) -> bool:
    h = (urlsplit(url).hostname or "").lower()
    return h == "pixeldrain.com" or h.endswith(".pixeldrain.com")


# Host shortener Nekopoi yang harus "dilewati" sebelum sampai file asli.
_SHORTENER_HOSTS = ("ouo.io", "ouo.press", "ouo.cx", "ouo.com", "linkpoi.me")


def _is_final_target(url: str) -> bool:
    """URL sudah keluar dari host shortener (bukan ouo/linkpoi lagi)."""
    h = (urlsplit(url).hostname or "").lower()
    if not h:
        return False
    return not any(h == s or h.endswith("." + s) for s in _SHORTENER_HOSTS)


def _follow_http_redirects(url: str, hops: int = 5) -> str | None:
    """Ikuti redirect satu-per-satu (tanpa auto) + meta-refresh + JS location.

    Untuk shortener non-ouo (mis. `linkpoi.me`) yang cuma melempar Location /
    meta refresh. Return url tujuan kalau berbeda, else None.
    """
    headers = {"User-Agent": UA, "Referer": url}
    cur = url
    for _ in range(hops):
        try:
            r = _http().get(cur, headers=headers, timeout=_HTTP_TIMEOUT, allow_redirects=False)
        except Exception as e:
            log.debug("redirect follow gagal | %s : %r", cur, e)
            return None
        loc = r.headers.get("Location") or r.headers.get("location")
        if loc:
            nxt = urljoin(cur, loc)
            if nxt != cur:
                cur = nxt
                if _is_final_target(cur):
                    return cur
                continue
        if r.status_code in (200, 301, 302, 303, 307, 308):
            text = r.text or ""
            for pat in (
                r'http-equiv\s*=\s*["\']?refresh["\']?[^>]*?url\s*=\s*["\']?([^"\'>\s]+)',
                r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
                r'href\s*=\s*["\'](https?://[^"\']+)["\']',
            ):
                m = re.search(pat, text, re.I)
                if m:
                    nxt = urljoin(cur, m.group(1))
                    if nxt != cur and _is_final_target(nxt):
                        return nxt
        break
    return cur if cur != url else None


def resolve_shortener(url: str) -> str | None:
    """Resolusi shortener Nekopoi -> URL final (pixeldrain / tujuan lain).

    - `ouo.io` / `ouo.press` -> `bypass_ouo` (form lama, lalu browser stealth).
    - shortener lain (`linkpoi.me`, dsb.) -> ikuti redirect HTTP / meta refresh;
      kalau masih mentok, pakai browser stealth generik.
    Blocking (bisa ~20-40s) -> panggil dari thread.
    """
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("ouo.io") or host.endswith("ouo.press"):
        return bypass_ouo(url)

    got = _follow_http_redirects(url)
    if got and _is_final_target(got):
        log.info("Shortener terresolusi via redirect | %s -> %s", url, got)
        return got

    log.debug("Redirect HTTP shortener mentok, fallback ke browser | %s", url)
    target = _bypass_ouo_stealth(url)
    if target and _is_final_target(target):
        log.info("Shortener terresolusi via browser | %s -> %s", url, target)
        return target
    log.debug("Gagal resolusi shortener | %s", url)
    return None


# Batas waktu jalur PROBE (buka embed -> packer -> master.m3u8 -> daftar varian).
# Probe hanya butuh halaman kecil, jadi timeout ketat: host yang origin-nya mati
# (streampoi balas HTTP 522 setelah Cloudflare nunggu ~40 detik) harus gagal
# cepat supaya fallback Pixeldrain tidak bikin user nunggu 2 menit.
PROBE_TIMEOUT = float(os.getenv("NEKOPOI_PROBE_TIMEOUT", "8"))
PROBE_ATTEMPTS = int(os.getenv("NEKOPOI_PROBE_ATTEMPTS", "1"))


def probe_stream(emb_url: str, referer: str) -> str:
    """Buka embed -> unpack packer -> URL master.m3u8.

    Sengaja timeout ketat + percobaan sedikit (lihat `PROBE_TIMEOUT`): embed yang
    hidup balas <1 detik, sedangkan embed yang mati butuh ~40 detik per percobaan
    cuma buat dapet error — terlalu mahal kalau probe ini di path user-facing.
    """
    text = _get(
        emb_url,
        referer=referer,
        attempts=PROBE_ATTEMPTS,
        timeout=PROBE_TIMEOUT,
    )
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


def _dood_variant_from_html(embed_url: str, html: str, title_hint: str = "") -> dict | None:
    """HTML embed DoodStream -> dict varian direct MP4 (atau None).

    Memakai path `/pass_md5/...` yang ada di HTML. Dipakai oleh jalur HTTP
    murni maupun jalur browser (setelah Turnstile selesai, halaman memuat
    pass_md5 dan fungsi ini bisa dipakai ulang dengan `page.content()`).
    """
    origin = embed_origin(embed_url).rstrip("/")
    m_pass = re.search(r"/pass_md5/[^\"'\s<>,)]+", html)
    if not m_pass:
        return None

    pass_url = origin + m_pass.group(0)
    try:
        rp = _http().get(
            pass_url, headers={"User-Agent": UA, "Referer": embed_url},
            timeout=PROBE_TIMEOUT,
        )
    except Exception as e:
        log.debug("DoodStream pass_md5 GET gagal | %s : %r", pass_url, e)
        return None
    if rp.status_code != 200:
        return None
    base_video = (rp.text or "").strip()
    if not base_video.startswith("http"):
        log.debug("DoodStream base URL tidak valid | %r", base_video[:60])
        return None

    m_tok = re.search(r"token=([A-Za-z0-9]+)", html)
    token = m_tok.group(1) if m_tok else "x"
    direct_url = base_video + "?token=" + token

    title_m = re.search(r"<title>([^<]+)</title>", html, re.I)
    t_raw = title_m.group(1) if title_m else title_hint
    h = 0
    for cand in (1080, 720, 480, 360):
        if str(cand) in t_raw:
            h = cand
            break
    if not h:
        h = 480

    sz = 0
    try:
        rv = _http().get(
            direct_url,
            headers={"User-Agent": UA, "Referer": embed_url},
            stream=True,
            timeout=PROBE_TIMEOUT,
        )
        sz = int(rv.headers.get("Content-Length") or 0)
        rv.close()
    except Exception:
        sz = 0

    return {
        "height": h,
        "bandwidth": h * 1000,
        "url": direct_url,
        "format_id": str(h),
        "type": "doodstream_direct",
        "referer": embed_url,
        "size": sz,
        "title": t_raw,
    }


def _probe_doodstream_browser(embed_url: str, title_hint: str = "") -> dict | None:
    """Fallback browser: DoodStream kadang menuntut Cloudflare Turnstile.

    Kalau Turnstile aktif, HTML awal cuma wrapper captcha (~5KB, tanpa
    `pass_md5`); `pass_md5` baru muncul setelah captcha tervalidasi
    (`turnstile.render(... callback -> /dood?op=validate&gc_response=... -> reload)`).
    Browser stealth menyelesaikan Turnstile lalu klik tombol play, baru
    `page.content()` memuat pass_md5. Blocking -> panggil dari thread.
    """
    try:
        from scrapling import StealthyFetcher
    except Exception as e:
        log.debug("Scrapling tidak tersedia untuk DoodStream: %r", e)
        return None

    holder = {"html": ""}

    def _auto(page):
        try:
            page.wait_for_timeout(2500)
            # Klik tombol play (memicu Turnstile) lalu tunggu halaman reload.
            for _ in range(12):
                try:
                    btn = page.locator("button.vjs-big-play-button, .captcha_l, .vjs-big-play-button")
                    if btn.count() > 0:
                        btn.first.click(force=True)
                except Exception:
                    pass
                page.wait_for_timeout(1200)
                c = page.content()
                if "/pass_md5/" in c:
                    holder["html"] = c
                    return
            holder["html"] = page.content()
        except Exception as e:
            log.debug("DoodStream browser aksi gagal | %s : %r", embed_url, e)

    try:
        StealthyFetcher.fetch(
            embed_url, headless=True, timeout=60000,
            block_ads=True, page_action=_auto,
        )
    except Exception as e:
        log.debug("DoodStream browser fetch gagal | %s : %r", embed_url, e)
        return None

    html = holder["html"]
    if not html or "/pass_md5/" not in html:
        return None
    return _dood_variant_from_html(embed_url, html, title_hint)


def probe_doodstream(embed_url: str, referer: str = "") -> dict | None:
    """Ekstrak direct MP4 dari embed DoodStream / Playmogo.

    Dua jalur:
    - **HTTP murni (cepat, ~2 request):** GET embed -> baca `<title>` + cari
      `/pass_md5/<hash>/<token>` -> GET pass_md5 (Referer = embed) -> dapat URL
      video dasar -> URL final = `base + "?token=" + token` (tanpa `?token=`
      host CDN redirect ke dood.video yang menolak koneksi).
    - **Browser stealth (fallback):** dipakai HANYA kalau HTML murni tidak
      memuat `pass_md5` karena DoodStream menuntut Cloudflare Turnstile
      (halaman ~5KB tanpa pass_md5). Lihat `_probe_doodstream_browser`.

    Return `{height, bandwidth, url, format_id, type="doodstream_direct",
    referer, title, size}` atau None.
    """
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer

    html = ""
    try:
        r = _http().get(embed_url, headers=headers, timeout=PROBE_TIMEOUT)
        if r.status_code == 200:
            html = r.text or ""
    except Exception as e:
        log.debug("DoodStream embed GET gagal | %s : %r", embed_url, e)

    # Urutan PENTING: cari pass_md5 DULU. String "video you are looking for is
    # not found" selalu ada di template JS player doodstream (jadi fallback
    # message), jadi kalau dicek duluan semua embed dianggap "file dihapus" —
    # padahal file-nya ada (false positive yang pernah terjadi di post 3D).
    if html and "/pass_md5/" in html:
        info = _dood_variant_from_html(embed_url, html)
        if info:
            return info

    # HTML murni tidak punya pass_md5 -> kemungkinan Turnstile. Coba browser.
    if html and ("turnstile" in html.lower() or "challenges.cloudflare.com" in html):
        log.debug("DoodStream butuh Turnstile, fallback browser | %s", embed_url)
    else:
        low = html.lower()
        if "video you are looking for is not found" in low or "file was deleted" in low:
            log.debug("DoodStream file sudah dihapus | %s", embed_url)
            return None
        log.debug("DoodStream pass_md5 tidak ada di HTML murni | %s", embed_url)

    return _probe_doodstream_browser(embed_url)


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


def _is_aes_encrypted(text: str) -> bool:
    """True kalau playlist punya kunci AES-128 (ffmpeg bisa dekripsi sendiri)."""
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("#EXT-X-KEY") and "AES" in ln.upper() and "METHOD=NONE" not in ln.upper():
            return True
    return False


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
    # Jalur probe -> timeout ketat (lihat komentar PROBE_TIMEOUT).
    text = _get(master_url, referer=referer, attempts=PROBE_ATTEMPTS, timeout=PROBE_TIMEOUT)
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
    if _is_aes_encrypted(text):
        return ["__AES_HLS__", variant_url]
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
            r = _http().get(url, headers=headers, timeout=SEG_TIMEOUT)
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
    pct = (done_seg * 100.0 / total_seg) if total_seg > 0 else 0.0
    return render_progress_text(
        sanitize_filename(title, 80),
        downloaded=done_bytes,
        total=total_bytes,
        speed_bps=speed_bps,
        eta_seconds=eta_seconds,
        extra=extra,
        pct=pct,
    )


def _render_progress_log(kind: str, title: str, downloaded: int, total: int,
                         speed_bps: float, avg_bps: float, eta_seconds: float | None) -> None:
    """Terminal progress log with consistent format across all downloaders."""
    try:
        downloaded = max(int(downloaded or 0), 0)
    except (TypeError, ValueError):
        downloaded = 0
    try:
        total = max(int(total or 0), 0)
    except (TypeError, ValueError):
        total = 0
    pct = (downloaded * 100.0 / total) if total > 0 else 0.0
    size_part = (
        f"{format_size(downloaded)}/{format_size(total)}"
        if total > 0
        else f"{format_size(downloaded)} downloaded"
    )
    eta_part = (
        f" eta={format_eta(eta_seconds)}"
        if (eta_seconds is not None and eta_seconds >= 0 and total > 0)
        else ""
    )
    log.info(
        "%s progress | title=%s %.1f%% %s speed=%s avg_speed=%s%s",
        kind,
        sanitize_filename(title, 40),
        pct,
        size_part,
        format_speed(speed_bps),
        format_speed(avg_bps),
        eta_part,
    )


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
    stats = TransferStats()
    extra = f"{label} · " if label else ""
    clean_extra = extra.rstrip(" · ")
    status_title = sanitize_filename(title, 80)

    # Interval edit adaptif (helper bersama, lihat `_adaptive_edit_interval`):
    # server Nekopoi lambat & file besar -> jangan spam Telegram (429).
    _interval_for = lambda: _adaptive_edit_interval(stats)  # noqa: E731

    if status_msg_id:
        await safe_edit_status(
            bot, chat_id, status_msg_id,
            _progress_text(title, 0, total, 0, 0, 0.0, None, clean_extra),
        )
        last_edit = time.monotonic()

    finished_count = 0
    while finished_count < total:
        await asyncio.sleep(PROGRESS_POLL)
        finished_count = sum(1 for t in tasks if t.done())

        now = time.monotonic()
        if state["total_est"]:
            stats.total = state["total_est"]
        stats.sample(state["bytes"], now=now)
        if stats.should_log(PROGRESS_LOG_INTERVAL, now=now):
            stats.log("Nekopoi download", label=status_title)

        if status_msg_id and now >= flood_until and (now - last_edit >= _interval_for() or last_edit < 0):
            wait = await safe_edit_status(
                bot, chat_id, status_msg_id,
                _progress_text(title, state["done"], total, stats.downloaded,
                               stats.total, stats.avg_bps, stats.eta_seconds, clean_extra),
            )
            last_edit = now
            if wait:
                flood_until = now + wait

    errors = [t.exception() for t in tasks if t.done() and not t.cancelled() and t.exception()]
    if errors:
        raise RuntimeError(str(errors[0]))

    # Edit terakhir: paksa bar 100% tampil sebelum lanjut ke ffmpeg/upload.
    if status_msg_id:
        await safe_edit_status(
            bot, chat_id, status_msg_id,
            _progress_text(title, total, total, stats.downloaded,
                           stats.total or stats.downloaded, stats.avg_bps, 0.0, clean_extra),
        )

    stats.log_done("Nekopoi download", label=status_title, size=state["bytes"])

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
        if len(seg_urls) == 2 and seg_urls[0] == "__AES_HLS__":
            _dbg("AES-128 detected, delegating to ffmpeg")
            from handlers.dl.remux import download_hls_aes_ffmpeg
            await download_hls_aes_ffmpeg(variant_url, referer, out_path, user_agent=UA, audio_only=False)
            if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
                raise RuntimeError("Gagal merangkai video Nekopoi via AES ffmpeg (file kosong)")
            return os.path.getsize(out_path)
        _dbg("segments | %s", len(seg_urls))
        concat_path, _ = await _download_segments(
            seg_urls, seg_dir, referer, bot, chat_id, status_msg_id, title, label
        )
        # Tidak ada edit "Muxing..." di sini: ffmpeg -c copy cuma beberapa
        # detik, status langsung berlanjut ke "Uploading" oleh worker.
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
        if len(seg_urls) == 2 and seg_urls[0] == "__AES_HLS__":
            _dbg("AES-128 audio detected, delegating to ffmpeg")
            from handlers.dl.remux import download_hls_aes_ffmpeg
            await download_hls_aes_ffmpeg(variant_url, referer, out_path, user_agent=UA, audio_only=True)
            if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
                raise RuntimeError("Gagal membuat MP3 dari Nekopoi via AES ffmpeg")
            return os.path.getsize(out_path)
            
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
        r = _http().get(url, headers={"User-Agent": UA}, timeout=_HTTP_TIMEOUT)
        if r.status_code == 200 and r.content:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception as e:
        log.debug("Nekopoi thumb download failed | url=%s err=%r", url, e)
    return None
