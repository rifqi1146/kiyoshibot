"""Shared Scrapling browser session manager.

PROBLEM:
    Beberapa downloader (RetroTube/VOE, Nekopoi/ouo+DoodStream, Asupan/tikwm)
    memakai Scrapling untuk melewati Cloudflare/DDoS-Guard. Pola lama memanggil
    `StealthyFetcher.fetch(...)` / `StealthyFetcher()` per-request, yang berarti
    **launch browser baru setiap kali** (~5-15s + RAM 300-500MB per instance).
    Di bawah beban (beberapa resolve paralel) ini boros RAM dan lambat.

SOLUTION:
    Satu `StealthySession` global yang hidup sepanjang proses, dijalankan di
    SATU thread kerja (Playwright sync API tidak thread-safe). Semua resolve
    berbagi browser/tab pool yang sama -> tanwha launch tiap request.

USAGE:
    from utils.scrapling_browser import fetch_html

    # blocking, panggil dari asyncio.to_thread
    html = fetch_html(url, page_action=my_action, referer=referer)

    # opsional saat bot start / shutdown
    warmup_browser()
    close_browser()

Catatan API (scrapling 0.4.15):
    - `StealthySession(**cfg).start()` / `.fetch(url, **kw)` / `.close()`.
    - `fetch()` kwarg yang sah: timeout, wait, network_idle, load_dom,
      google_search, page_setup, page_action, extra_headers, wait_selector,
      wait_selector_state, disable_resources, blocked_domains, proxy,
      solve_cloudflare, selector_config.
    - `block_ads`/`capture_xhr` TIDAK ada di versi ini -> jangan dipakai.
    - `disable_resources=True` men-drop request tipe `media` (m3u8) -> JANGAN
      dipakai bila kita perlu menangkap stream.
"""

from __future__ import annotations

import logging
import threading
import time
import concurrent.futures

log = logging.getLogger(__name__)

# ── Session global (sync StealthySession) ───────────────────────────────────
_BROWSER_EXEC = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="scrapling-browser"
)
_SESSION = None
_SESSION_LOCK = threading.Lock()

# Cookie clearance per-host (dipakai ulang oleh curl_cffi di jalur cepat).
_COOKIES: dict = {}          # host -> ({"name": "value"}, ts)
_COOKIE_TTL = 1800.0
_COOKIE_MAX = 500            # batas jumlah host yang di-cache

# Konfigurasi default; bisa diperkecil oleh caller bila perlu.
_DEFAULT_CFG = dict(
    headless=True,
    timeout=45000,
    wait=0,
    network_idle=False,
    retries=1,
    google_search=False,
    solve_cloudflare=True,
)


def _get_session():
    """Buat (sekali) StealthySession global. Harus dipanggil dari _BROWSER_EXEC."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            return _SESSION
        try:
            from scrapling.fetchers import StealthySession
        except Exception as e:
            log.warning("Scrapling StealthySession tidak tersedia | err=%r", e)
            return None
        try:
            _SESSION = StealthySession(**_DEFAULT_CFG)
        except Exception as e:
            log.warning("StealthySession tolak konfig penuh, pakai minimal | err=%r", e)
            try:
                _SESSION = StealthySession(headless=True, timeout=45000, solve_cloudflare=True)
            except Exception as e2:
                log.warning("StealthySession gagal dibuat | err=%r", e2)
                _SESSION = None
                return None
        try:
            _SESSION.start()
            try:
                log.info("Scrapling browser session started | pool=%s", _SESSION.get_pool_stats())
            except Exception:
                log.info("Scrapling browser session started")
        except Exception as e:
            log.warning("StealthySession gagal start | err=%r", e)
            _SESSION = None
    return _SESSION


def _prune_cookies(now: float):
    """Buang cookie cache yang sudah expired; kalau masih lebih dari batas,
    buang entri tertua supaya map tidak tumbuh tanpa batas."""
    expired = [h for h, (_, ts) in _COOKIES.items() if now - ts > _COOKIE_TTL]
    for h in expired:
        _COOKIES.pop(h, None)
    if len(_COOKIES) > _COOKIE_MAX:
        # dict menjaga urutan insert -> buang yang paling awal (paling tua)
        overflow = len(_COOKIES) - _COOKIE_MAX
        for h in list(_COOKIES)[: max(1, overflow)]:
            _COOKIES.pop(h, None)


def store_cookies(ck_list):
    """Simpan cookie (mis. cf_clearance) agar jalur curl_cffi bisa reuse."""
    if not ck_list:
        return
    now = time.time()
    per_host = {}
    for c in ck_list:
        if not isinstance(c, dict) or "name" not in c:
            continue
        h = (c.get("domain") or "").lstrip(".").lower()
        if not h:
            continue
        per_host.setdefault(h, {})[str(c["name"])] = c.get("value", "")
    for h, d in per_host.items():
        _COOKIES[h] = (d, now)
    _prune_cookies(now)


def cookies_for(host: str):
    """Ambil cookie clearance untuk host (termasuk parent domain). None bila tidak ada/expired."""
    if not host:
        return None
    ent = _COOKIES.get(host)
    if not ent:
        parts = host.split(".")
        for i in range(1, len(parts) - 1):
            ent = _COOKIES.get(".".join(parts[i:]))
            if ent:
                break
    if not ent:
        return None
    data, ts = ent
    if time.time() - ts > _COOKIE_TTL:
        return None
    return data


def clear_cookies(host: str):
    if not host:
        return
    for k in list(_COOKIES):
        if host == k or host.endswith("." + k):
            _COOKIES.pop(k, None)


# ── Job runner (semua eksekusi browser lewat satu thread) ───────────────────
def _fetch_job(url: str, kwargs: dict, page_action, page_setup,
               extra_headers: dict, wait_selector, wait_selector_state,
               timeout_ms: int) -> str:
    """Satu fetch. Berjalan HANYA di thread _BROWSER_EXEC."""
    session = _get_session()
    if session is None:
        return ""
    try:
        fetch_kwargs = dict(
            timeout=timeout_ms,
            wait=kwargs.get("wait", 0),
            network_idle=kwargs.get("network_idle", False),
            load_dom=kwargs.get("load_dom", True),
            google_search=False,
            page_setup=page_setup,
            page_action=page_action,
            solve_cloudflare=kwargs.get("solve_cloudflare", True),
        )
        if extra_headers:
            fetch_kwargs["extra_headers"] = extra_headers
        # Jangan kirim None: Scrapling menolak `null` untuk argumen bertipe str.
        if wait_selector:
            fetch_kwargs["wait_selector"] = wait_selector
        if wait_selector_state:
            fetch_kwargs["wait_selector_state"] = wait_selector_state
        resp = session.fetch(url, **fetch_kwargs)
        try:
            text = resp.html_content or ""
            text = str(text)
        except Exception:
            try:
                text = resp.body.decode("utf-8", "ignore") if resp.body else ""
            except Exception:
                text = ""
        # Simpan cookie agar jalur cepat bisa reuse.
        try:
            cks = None
            ctx = getattr(resp, "context", None)
            if ctx is not None:
                cks = ctx.cookies()
            if cks:
                store_cookies(cks)
        except Exception:
            pass
        return text
    except Exception as e:
        log.debug("Scrapling fetch gagal | url=%s err=%r", url, e)
        return ""


def fetch_html(
    url: str,
    *,
    page_action=None,
    page_setup=None,
    referer: str = "",
    timeout_ms: int = 45000,
    wait_selector: str | None = None,
    wait_selector_state: str | None = None,
    **kwargs,
) -> str:
    """Fetch HTML via StealthySession global. Blocking -> panggil dari thread.

    Mengembalikan HTML string (kosong bila gagal / Scrapling tidak tersedia).
    """
    extra_headers = {"Referer": referer} if referer else {}
    try:
        fut = _BROWSER_EXEC.submit(
            _fetch_job, url, kwargs, page_action, page_setup,
            extra_headers, wait_selector, wait_selector_state, timeout_ms,
        )
        # Margin: job bisa mengantre di belakang job lain pada thread tunggal.
        return fut.result(timeout=(timeout_ms / 1000.0) * 3 + 30.0)
    except concurrent.futures.TimeoutError:
        log.debug("Scrapling fetch timeout | url=%s", url)
    except Exception as e:
        log.debug("Scrapling fetch job gagal | url=%s err=%r", url, e)
    return ""


def warmup_browser() -> bool:
    """Panggil sekali saat bot start supaya launch pertama tidak membebani user."""
    try:
        _BROWSER_EXEC.submit(_get_session).result(timeout=90)
        return _SESSION is not None
    except Exception as e:
        log.debug("Scrapling warmup gagal | err=%r", e)
        return False


def close_browser():
    """Panggil saat shutdown agar proses browser mati bersih."""
    def _close():
        global _SESSION
        if _SESSION is not None:
            try:
                _SESSION.close()
            except Exception as e:
                log.debug("Scrapling close gagal | err=%r", e)
            _SESSION = None

    try:
        _BROWSER_EXEC.submit(_close).result(timeout=30)
    except Exception:
        pass
