
from __future__ import annotations

import logging
import threading
import time
import concurrent.futures

log = logging.getLogger(__name__)

_BROWSER_EXEC = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="scrapling-browser"
)
_SESSION = None
_SESSION_LOCK = threading.Lock()

_COOKIES: dict = {}
_COOKIE_TTL = 1800.0
_COOKIE_MAX = 500

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
    global _SESSION
    if _SESSION is None:
        return
    try:
        _SESSION.close()
    except Exception:
        pass
    _SESSION = None


def _prune_cookies(now: float):
    expired = [h for h, (_, ts) in _COOKIES.items() if now - ts > _COOKIE_TTL]
    for h in expired:
        _COOKIES.pop(h, None)
    if len(_COOKIES) > _COOKIE_MAX:
        overflow = len(_COOKIES) - _COOKIE_MAX
        for h in list(_COOKIES)[: max(1, overflow)]:
            _COOKIES.pop(h, None)


def store_cookies(ck_list):
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


def _fetch_job(url: str, kwargs: dict, page_action, page_setup,
               extra_headers: dict, wait_selector, wait_selector_state,
               timeout_ms: int) -> str:
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
        try:
            cks = getattr(resp, "cookies", None)
            if cks:
                store_cookies(cks)
        except Exception:
            pass
        return text
    except Exception as e:
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
    extra_headers = {"Referer": referer} if referer else {}
    try:
        fut = _BROWSER_EXEC.submit(
            _fetch_job, url, kwargs, page_action, page_setup,
            extra_headers, wait_selector, wait_selector_state, timeout_ms,
        )
        return fut.result(timeout=(timeout_ms / 1000.0) * 3 + 30.0)
    except concurrent.futures.TimeoutError:
        log.debug("Scrapling fetch timeout | url=%s", url)
    except Exception as e:
        log.debug("Scrapling fetch job failed | url=%s err=%r", url, e)
    return ""


def warmup_browser() -> bool:
    try:
        _BROWSER_EXEC.submit(_get_session).result(timeout=90)
        return _SESSION is not None
    except Exception as e:
        log.debug("Scrapling warmup failed | err=%r", e)
        return False


def close_browser():
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
