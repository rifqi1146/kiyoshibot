import random
import asyncio
import logging
from contextlib import asynccontextmanager
import json
import time
import os
from scrapling.fetchers import StealthyFetcher
from curl_cffi import requests as curl_requests
from utils.http import get_http_session
from .constants import DEFAULT_ASUPAN_KEYWORDS

log = logging.getLogger(__name__)
logging.getLogger("scrapling").setLevel(logging.ERROR)

TIKWM_HOME = "https://www.tikwm.com/"
TIKWM_SEARCH_API = "https://www.tikwm.com/api/feed/search"
TIKWM_RESOLVE_API = "https://www.tikwm.com/api/"
CF_COOKIE_FILE = os.path.join("data", "tikwm_cf.json")

# cf_clearance umurnya terbatas. TTL ini cuma HINT (untuk log); cookie yang
# lewat TTL tetap dicoba dulu via curl karena sering masih valid, dan browser
# solve (~25s) baru dipakai kalau curl benar-benar ditolak Cloudflare.
CF_COOKIE_TTL = 25 * 60

# tikwm free API membalas code=-1 saat kena limit 1 req/detik. Itu BUKAN masalah
# cookie Cloudflare, jadi jangan sampai memicu browser solve.
_RATE_LIMITED = object()


class _RateLimited(Exception):
    """tikwm membalas code=-1 (limit 1 req/detik); sesi Cloudflare masih bagus."""

# Pool video DIPISAH per-keyword. Ini mencegah video hasil search keyword A
# bocor/terpakai untuk keyword B (dulu pool-nya global sehingga campur).
_VIDEO_POOLS: dict[str, list[dict]] = {}
# Keyword default yang sedang aktif. Dipakai ulang sampai pool-nya habis supaya
# 1x search melayani banyak video (tidak search 20 video tiap kali ganti asupan).
_DEFAULT_KEYWORD: str | None = None
# Satu lock per keyword: keyword berbeda boleh search bergantian tanpa saling
# menunggu selesai (jarak request tetap dijaga _api_gate), tapi keyword yang
# sama tidak akan memicu search berkali-kali (thundering herd).
_POOL_LOCKS: dict[str, asyncio.Lock] = {}
# Lock khusus jalur default (tanpa keyword): semua user default memakai SATU
# keyword aktif yang sama sampai pool-nya habis.
_DEFAULT_META_LOCK = asyncio.Lock()
# Cap pool keyword + lock: pool per keyword memuat ~20 dict video, jadi keyword
# sembarang/typo user harus punya batas. Dibiarkan tanpa cap = menumpuk selama
# umur proses. Lihat `_prune_pools`.
_VIDEO_POOLS_MAX = int(os.getenv("ASUPAN_POOL_MAX", "64"))
_POOL_LOCKS_MAX = int(os.getenv("ASUPAN_POOL_LOCKS_MAX", "64"))
# Cache sesi Cloudflare (cookie cf_clearance + UA browser) untuk fast-path search.
_CF_SESSION: dict | None = None

# tikwm free API dibatasi "1 request/second". Semua request /api/ diserialkan
# dengan jarak minimal 1.1s supaya tidak kena code=-1 (yang bikin fallback download).
_API_LOCK = asyncio.Lock()
_LAST_API_TS = 0.0
API_MIN_INTERVAL = 1.1


@asynccontextmanager
async def _api_gate():
    """Satu request tikwm /api/ dalam satu waktu, jarak minimal API_MIN_INTERVAL.

    Lock DITAHAN sampai request selesai supaya request tidak tumpang-tindih:
    tikwm free API menolak request yang berbarengan dengan code=-1 (hasil kosong).
    """
    global _LAST_API_TS
    async with _API_LOCK:
        wait = API_MIN_INTERVAL - (time.monotonic() - _LAST_API_TS)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_API_TS = time.monotonic()
        yield


def _load_cf_session() -> dict | None:
    """Ambil sesi Cloudflare dari memori/disk."""
    global _CF_SESSION
    if _CF_SESSION:
        return _CF_SESSION
    try:
        with open(CF_COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("cookies") and data.get("ua"):
            _CF_SESSION = data
            return _CF_SESSION
    except Exception:
        pass
    return None


def _save_cf_session(cookies: list[dict], ua: str) -> None:
    """Simpan cookie cf_clearance + UA hasil solve browser untuk dipakai curl_cffi."""
    global _CF_SESSION
    jar = {
        c["name"]: c["value"]
        for c in (cookies or [])
        if c.get("name") and c.get("value") is not None
    }
    data = {"cookies": jar, "ua": ua, "ts": time.time()}
    _CF_SESSION = data
    try:
        os.makedirs(os.path.dirname(CF_COOKIE_FILE) or ".", exist_ok=True)
        tmp_file = f"{CF_COOKIE_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_file, CF_COOKIE_FILE)
        log.debug("Sesi Cloudflare tersimpan | cookies=%s", len(jar))
    except Exception as e:
        log.debug("Gagal menyimpan sesi Cloudflare: %r", e)


def _cf_session_fresh(data: dict | None) -> bool:
    if not data or not data.get("cookies") or not data.get("ua"):
        return False
    try:
        return (time.time() - float(data.get("ts", 0))) < CF_COOKIE_TTL
    except Exception:
        return False


def _map_videos(data: dict) -> list[dict]:
    """Normalisasi respons /api/feed/search jadi daftar video (buang foto/slideshow)."""
    if not data or data.get("code") != 0 or not data.get("data"):
        return []
    videos = data["data"].get("videos") or []
    items = []
    for v in videos:
        if v.get("images"):
            continue
        items.append({
            "id": v.get("video_id") or v.get("id"),
            "unique_id": (v.get("author") or {}).get("unique_id") or "_",
            "play": v.get("play") or v.get("wmplay") or "",
        })
    return items


def _search_via_curl(query: str, cf: dict) -> list[dict] | None:
    """Fast-path: keyword search pakai cf_clearance + impersonasi TLS Chrome.

    Return list video kalau sukses, None kalau cookie ditolak/expired/error
    (supaya pemanggil refresh lewat browser), atau raise _RateLimited kalau
    tikwm cuma membalas limit 1 req/detik (cookie CF tetap valid).
    """
    headers = {
        "User-Agent": cf["ua"],
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": TIKWM_HOME,
        "Origin": "https://www.tikwm.com",
    }
    try:
        resp = curl_requests.post(
            TIKWM_SEARCH_API,
            data={"keywords": query, "count": "20", "cursor": "0", "web": "1", "hd": "1"},
            headers=headers,
            cookies=cf["cookies"],
            impersonate="chrome",
            timeout=25,
        )
    except Exception as e:
        log.warning("Search via curl_cffi error: %r", e)
        return None

    if resp.status_code != 200:
        log.info("Search via curl_cffi HTTP %s (cookie mungkin expired)", resp.status_code)
        return None

    try:
        data = resp.json()
    except Exception as e:
        # Bukan JSON -> hampir pasti halaman challenge Cloudflare.
        log.warning("Search via curl_cffi bukan JSON (kemungkinan challenge CF): %r", e)
        return None

    if data.get("code") == -1:
        raise _RateLimited(data.get("msg") or "Free Api Limit")

    return _map_videos(data)


def _fetch_api_in_browser(query: str) -> list[dict]:
    """Solve Cloudflare di browser, sekaligus ambil 20 video & refresh cookie cf_clearance."""
    result_container = []

    def page_action(page):
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)

        # Simpan cf_clearance + UA supaya request berikutnya bisa pakai curl_cffi (jauh lebih cepat).
        try:
            cookies = page.context.cookies()
            ua = page.evaluate("navigator.userAgent")
            if cookies and ua:
                _save_cf_session(cookies, ua)
        except Exception as e:
            log.debug("Gagal capture sesi Cloudflare: %r", e)

        js_script = f"""
        (async () => {{
            try {{
                const resp = await fetch("/api/feed/search", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest"
                    }},
                    body: new URLSearchParams({{
                        keywords: {json.dumps(query)},
                        count: "20",
                        cursor: "0",
                        web: "1",
                        hd: "1"
                    }})
                }});
                const data = await resp.json();

                if (!data || data.code !== 0 || !data.data || !data.data.videos) {{
                    return [];
                }}

                // Saring foto/slideshow dan kembalikan array video MP4 murni
                return data.data.videos.filter(v => !v.images || v.images.length === 0).map(v => ({{
                    id: v.video_id || v.id,
                    unique_id: (v.author && v.author.unique_id) ? v.author.unique_id : "_",
                    play: v.play || v.wmplay || ""
                }}));
            }} catch (err) {{
                return [];
            }}
        }})()
        """

        try:
            data = page.evaluate(js_script)
            if isinstance(data, list):
                result_container.extend(data)
        except Exception as e:
            log.warning("Failed to evaluate JS priming: %s", e)

    fetcher = StealthyFetcher()
    fetcher.fetch(
        TIKWM_HOME,
        headless=True,
        solve_cloudflare=True,
        timeout=40000,
        page_action=page_action
    )

    return result_container


async def _search_keyword(query: str) -> list[dict]:
    """Cari video via curl_cffi (cepat); browser solve hanya kalau terpaksa.

    Optimasi bypass Cloudflare:
    - cookie yang lewat TTL tetap dicoba dulu (TTL cuma estimasi) -> sering
      menghemat satu browser solve ~25s;
    - sesi dibaca ulang di dalam _api_gate supaya cookie yang baru di-refresh
      request lain langsung terpakai (tidak browser solve berkali-kali);
    - code=-1 (rate limit) diulang tanpa membuang waktu solve browser;
    - error jaringan sesaat pun diulang dulu sebelum jatuh ke browser.
    """
    for _ in range(3):
        cf = _load_cf_session()
        if cf is None:
            break
        if not _cf_session_fresh(cf):
            log.debug("Sesi Cloudflare lewat TTL, tetap dicoba via curl")

        async with _api_gate():
            # Baca ulang: request lain mungkin baru saja me-refresh cookie.
            cf = _load_cf_session() or cf
            try:
                items = await asyncio.to_thread(_search_via_curl, query, cf)
            except _RateLimited:
                items = _RATE_LIMITED

        if items is _RATE_LIMITED:
            log.warning("Search keyword=%r kena rate limit, ulangi", query)
            await asyncio.sleep(API_MIN_INTERVAL)
            continue
        if items is not None:
            return items
        break  # cookie ditolak/expired -> refresh lewat browser
    else:
        log.warning("Search keyword=%r tetap rate-limited, lewati", query)
        return []

    # Belum ada cookie / cookie ditolak -> solve Cloudflare lewat browser
    # (sekaligus menyimpan cookie + mengembalikan hasil search).
    async with _api_gate():
        cf = _load_cf_session()
        if cf is not None:
            # Request lain rupanya baru refresh cookie; cukup pakai curl.
            try:
                items = await asyncio.to_thread(_search_via_curl, query, cf)
            except _RateLimited:
                items = None
            if items is not None:
                return items
        return await asyncio.to_thread(_fetch_api_in_browser, query)


async def _prime_and_get_url(video_item: dict) -> str | None:
    """Resolve URL MP4 CDN langsung via /api/ (tanpa browser).

    Hanya mengembalikan URL absolut (tiktokcdn). URL relatif tikwm-hosted TIDAK
    dipakai karena Telegram tidak bisa mengambilnya -> kalau gagal, return None
    supaya pemanggil mencoba video lain.
    """
    target_url = f"https://www.tiktok.com/@{video_item['unique_id']}/video/{video_item['id']}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }

    session = await get_http_session()
    for _ in range(3):
        try:
            async with _api_gate():
                async with session.post(TIKWM_RESOLVE_API, headers=headers, data={"url": target_url, "hd": "1"}, timeout=20) as resp:
                    if resp.status != 200:
                        log.debug("Resolve HTTP %s | id=%s", resp.status, video_item.get("id"))
                        continue
                    data = await resp.json()
            if data.get("code") == 0 and data.get("data"):
                vid_data = data["data"]
                url = vid_data.get("play") or vid_data.get("hdplay") or vid_data.get("wmplay")
                if url and not url.startswith("/"):
                    return url
                log.debug("Resolve relatif/aneh | id=%s url=%s", video_item.get("id"), str(url)[:60])
            else:
                log.debug("Resolve gagal | id=%s code=%s msg=%s",
                          video_item.get("id"), data.get("code"), data.get("msg"))
        except Exception as e:
            log.debug("Resolve error | id=%s err=%r", video_item.get("id"), e)
    return None


def _lock_busy(lock: asyncio.Lock) -> bool:
    """True kalau lock sedang dipegang ATAU sedang ditunggu coroutine lain.

    `Lock.locked()` hanya melihat yang sedang memegang; coroutine yang antre
    tidak terlihat. `_waiters` dipakai untuk melihat antrean — `None` berarti
    tidak pernah ada yang menantre (idle, aman dibuang), sedangkan deque
    berisi antrian nyata. Kalau atributnya hilang sama sekali (API berubah),
    dianggap sibuk supaya lock aktif tidak pernah dibuang paksa.
    """
    if lock.locked():
        return True
    try:
        waiters = lock._waiters
    except AttributeError:
        return True
    if waiters is None:
        return False
    try:
        return len(waiters) > 0
    except TypeError:
        return True


def _pop_pool(norm: str) -> dict | None:
    """Ambil 1 video acak dari pool keyword. Hapus pool kalau sudah kosong."""
    pool = _VIDEO_POOLS.get(norm)
    if not pool:
        return None
    item = pool.pop(random.randrange(len(pool)))
    if not pool:
        _VIDEO_POOLS.pop(norm, None)
    return item


def _prune_pools() -> None:
    """Batas ukuran pool keyword & lock supaya tak tumbuh tanpa batas.

    Pool tiap keyword menyimpan ~20 dict video. Kalau user memasukkan keyword
    yang berbeda-beda (termasuk typo), kedua struktur menumpuk selamanya.
    Kedua dict mempertahankan urutan insert, jadi buang dari kepala = yang
    paling lama tidak dipakai. Entry yang masih di-PIN oleh lock aktif tidak
    disentuh.
    """
    if len(_VIDEO_POOLS) > _VIDEO_POOLS_MAX:
        for k in list(_VIDEO_POOLS)[: len(_VIDEO_POOLS) - _VIDEO_POOLS_MAX]:
            lk = _POOL_LOCKS.get(k)
            if lk is not None and _lock_busy(lk):
                continue
            _VIDEO_POOLS.pop(k, None)
            if k in _POOL_LOCKS and not _lock_busy(_POOL_LOCKS[k]):
                _POOL_LOCKS.pop(k, None)
    if len(_POOL_LOCKS) > _POOL_LOCKS_MAX:
        for k in [k for k, lk in _POOL_LOCKS.items() if not _lock_busy(lk)]:
            if len(_POOL_LOCKS) <= _POOL_LOCKS_MAX:
                break
            if k in _VIDEO_POOLS:
                continue
            _POOL_LOCKS.pop(k, None)


async def _acquire_item(keyword: str | None) -> dict | None:
    """Ambil 1 video dari pool; isi pool lewat search kalau kosong.

    - Keyword eksplisit: dikunci per keyword, jadi keyword yang sama tidak
      memicu search berkali-kali (thundering herd), sedangkan keyword berbeda
      bisa berjalan berbarengan (jarak request tetap dijaga _api_gate).
    - Tanpa keyword: dikunci satu lock global supaya semua user default memakai
      SATU keyword aktif yang sama sampai pool-nya habis (tidak search per user).
    """
    global _DEFAULT_KEYWORD

    if keyword and keyword.strip():
        q = keyword.strip()
        norm = q.lower()
        _prune_pools()
        lock = _POOL_LOCKS.setdefault(norm, asyncio.Lock())
        try:
            async with lock:
                item = _pop_pool(norm)
                if item:
                    return item
                log.info("Mengisi pool asupan | query=%s", q)
                new_videos = await _search_keyword(q)
                if not new_videos:
                    return None
                _VIDEO_POOLS[norm] = new_videos
                log.info("Pool asupan keyword=%r terisi: %s video", q, len(new_videos))
                return _pop_pool(norm)
        finally:
            # Dijalankan SETELAH `async with` melepas kunci, dan hanya membuang
            # saat benar-benar tidak ada pemakai lain (holder + antrean kosong).
            # Kalau pool sudah habis, simpanan lock tidak lagi berguna — buang
            # supaya keyword sembarang tidak menumpuk.
            if norm not in _VIDEO_POOLS and _POOL_LOCKS.get(norm) is lock and not _lock_busy(lock):
                _POOL_LOCKS.pop(norm, None)

    async with _DEFAULT_META_LOCK:
        if _DEFAULT_KEYWORD:
            item = _pop_pool(_DEFAULT_KEYWORD.lower())
            if item:
                return item

        for _ in range(3):
            kw = random.choice(DEFAULT_ASUPAN_KEYWORDS)
            log.info("Mengisi pool asupan | query=%s", kw)
            new_videos = await _search_keyword(kw)
            if new_videos:
                _DEFAULT_KEYWORD = kw
                _VIDEO_POOLS[kw.lower()] = new_videos
                log.info("Pool asupan keyword=%r terisi: %s video", kw, len(new_videos))
                return _pop_pool(kw.lower())

        return None


async def fetch_asupan_tikwm(keyword: str | None = None) -> str:
    """Ambil 1 video asupan dan kembalikan URL CDN langsung (bukan file).

    Video diambil HANYA dari pool milik keyword yang diminta, jadi keyword user
    yang berbeda tidak akan saling tabrakan. Keyword None memakai satu keyword
    default aktif sampai pool-nya habis.
    """
    for _ in range(5):
        video_item = await _acquire_item(keyword)
        if not video_item:
            break

        final_url = await _prime_and_get_url(video_item)
        if final_url:
            return final_url
        log.warning("Failed to resolve video id=%s, trying another video", video_item.get("id"))

    raise RuntimeError("Failed to get stream URL from asupan video")
