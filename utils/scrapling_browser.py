"""Shared Scrapling browser session manager.

PROBLEM:
    Several downloaders (RetroTube/VOE, Nekopoi/ouo+DoodStream, Asupan/tikwm)
    use Scrapling to bypass Cloudflare/DDoS-Guard. The old pattern called
    `StealthyFetcher.fetch(...)` / `StealthyFetcher()` per request, which meant
    **launching a new browser every time** (~5-15s + 300-500MB RAM per instance).
    Under load (several parallel resolves) this is wasteful and slow.

SOLUTION:
    One global `StealthySession` that lives for the whole process, running on a
    SINGLE worker thread (the Playwright sync API is not thread-safe). All
    resolves share the same browser/tab pool -> no launch per request.

USAGE:
    from utils.scrapling_browser import fetch_html

    # blocking, call from asyncio.to_thread
    html = fetch_html(url, page_action=my_action, referer=referer)

    # optional at bot start / shutdown
    warmup_browser()
    close_browser()

API notes (scrapling 0.4.15):
    - `StealthySession(**cfg).start()` / `.fetch(url, **kw)` / `.close()`.
    - valid `fetch()` kwargs: timeout, wait, network_idle, load_dom,
      google_search, page_setup, page_action, extra_headers, wait_selector,
      wait_selector_state, disable_resources, blocked_domains, proxy,
      solve_cloudflare, selector_config.
    - `block_ads`/`capture_xhr` do NOT exist in this version -> do not use them.
    - `disable_resources=True` drops `media` requests (m3u8) -> do NOT use it
      when we need to capture the stream.
"""

from __future__ import annotations

import logging
import threading
import time
import concurrent.futures

log = logging.getLogger(__name__)

# ── Global session (sync StealthySession) ───────────────────────────────────
_BROWSER_EXEC = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="scrapling-browser"
)
_SESSION = None
_SESSION_LOCK = threading.Lock()

# Per-host clearance cookies (reused by curl_cffi on the fast path).
_COOKIES: dict = {}          # host -> ({"name": "value"}, ts)
_COOKIE_TTL = 1800.0
_COOKIE_MAX = 500            # maximum number of hosts to cache

# Default configuration; can be overridden per call by callers when needed.
#
# solve_cloudflare=False: many targets in this repo (VOE/DDoS-Guard, tikwm) are
# NOT Cloudflare Turnstile. Scrapling's CF solver only adds delay + waits for
# elements that do not exist (`ERROR: No Cloudflare challenge found.`) and
# disrupts navigation timing. Challenge cookies (cf_clearance / __ddg*) are
# still stored from the response and reused by curl_cffi on the fast path.
# Set solve_cloudflare=True per call only when the target actually uses CF Turnstile.
_DEFAULT_CFG = dict(
    headless=True,
    timeout=45000,
    wait=0,
    network_idle=False,
    retries=1,
    google_search=False,
    solve_cloudflare=False,
)


def _get_session():
    """Create (once) the global StealthySession. Must be called from _BROWSER_EXEC."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            return _SESSION
        try:
            from scrapling.fetchers import StealthySession
        except Exception as e:
            log.warning("Scrapling StealthySession unavailable | err=%r", e)
            return None
        try:
            _SESSION = StealthySession(**_DEFAULT_CFG)
        except Exception as e:
            log.warning("StealthySession rejected full config, falling back to minimal | err=%r", e)
            try:
                _SESSION = StealthySession(headless=True, timeout=45000, solve_cloudflare=False)
            except Exception as e2:
                log.warning("Failed to create StealthySession | err=%r", e2)
                _SESSION = None
                return None
        try:
            _SESSION.start()
            try:
                log.info("Scrapling browser session started | pool=%s", _SESSION.get_pool_stats())
            except Exception:
                log.info("Scrapling browser session started")
        except Exception as e:
            log.warning("Failed to start StealthySession | err=%r", e)
            _SESSION = None
    return _SESSION


def _reset_session():
    """Drop the shared session so the next fetch spins up a fresh browser.

    A crashed browser (e.g. node/playwright EPIPE after a redirect) leaves a
    dead session behind; reusing it makes every subsequent fetch fail. Call
    this from the single worker thread only.
    """
    global _SESSION
    if _SESSION is None:
        return
    try:
        _SESSION.close()
    except Exception:
        pass
    _SESSION = None


def _prune_cookies(now: float):
    """Evict expired cookie cache entries; if still over the cap, drop the oldest."""
    expired = [h for h, (_, ts) in _COOKIES.items() if now - ts > _COOKIE_TTL]
    for h in expired:
        _COOKIES.pop(h, None)
    if len(_COOKIES) > _COOKIE_MAX:
        overflow = len(_COOKIES) - _COOKIE_MAX
        for h in list(_COOKIES)[: max(1, overflow)]:
            _COOKIES.pop(h, None)


def store_cookies(ck_list):
    """Store clearance cookies (e.g. cf_clearance, __ddg*) for curl_cffi reuse."""
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
    """Return clearance cookies for a host (including parent domains). None if missing/expired."""
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


# ── Job runner (all browser execution goes through a single thread) ─────────
def _fetch_job(url: str, kwargs: dict, page_action, page_setup,
               extra_headers: dict, wait_selector, wait_selector_state,
               timeout_ms: int) -> str:
    """One fetch. Runs ONLY on the _BROWSER_EXEC thread."""
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
            solve_cloudflare=kwargs.get("solve_cloudflare", False),
        )
        if extra_headers:
            fetch_kwargs["extra_headers"] = extra_headers
        # Never send None: Scrapling rejects `null` for str-typed arguments.
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
        # Store clearance cookies (DDoS-Guard `__ddg*`, Cloudflare `cf_clearance`)
        # so the fast curl_cffi path can reuse them. API note: a Scrapling Response
        # has NO `.context` — cookies live on the `.cookies` attribute (tuple of dicts).
        try:
            cks = getattr(resp, "cookies", None)
            if cks:
                store_cookies(cks)
        except Exception:
            pass
        return text
    except Exception as e:
        # A crashed/dead browser must not be reused: drop the session so the next
        # fetch starts a fresh one. Without this, one EPIPE poisons every later
        # fetch (the real cause of the intermittent 50s timeouts).
        log.warning("Scrapling fetch failed, recycling session | url=%s err=%r", url, str(e)[:200])
        _reset_session()
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
    """Fetch HTML via the global StealthySession. Blocking -> call from a thread.

    Returns the HTML string (empty on failure / Scrapling unavailable).
    """
    extra_headers = {"Referer": referer} if referer else {}
    try:
        fut = _BROWSER_EXEC.submit(
            _fetch_job, url, kwargs, page_action, page_setup,
            extra_headers, wait_selector, wait_selector_state, timeout_ms,
        )
        # Margin: the job may queue behind another job on the single thread.
        return fut.result(timeout=(timeout_ms / 1000.0) * 3 + 30.0)
    except concurrent.futures.TimeoutError:
        log.debug("Scrapling fetch timeout | url=%s", url)
    except Exception as e:
        log.debug("Scrapling fetch job failed | url=%s err=%r", url, e)
    return ""


def warmup_browser() -> bool:
    """Call once at bot start so the first launch does not burden a user request."""
    try:
        _BROWSER_EXEC.submit(_get_session).result(timeout=90)
        return _SESSION is not None
    except Exception as e:
        log.debug("Scrapling warmup failed | err=%r", e)
        return False


def close_browser():
    """Call at shutdown so the browser process exits cleanly."""
    def _close():
        global _SESSION
        if _SESSION is not None:
            try:
                _SESSION.close()
            except Exception as e:
                log.debug("Scrapling close failed | err=%r", e)
            _SESSION = None

    try:
        _BROWSER_EXEC.submit(_close).result(timeout=30)
    except Exception:
        pass
