from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)

ENABLED = os.getenv("ASUPAN_DTK_SEARCH", "1").strip().lower() not in ("0", "false", "no")
IMPERSONATE = os.getenv("ASUPAN_DTK_IMPERSONATE", "chrome131")
TIMEOUT = float(os.getenv("ASUPAN_DTK_TIMEOUT", "25"))
MAX_ITEMS = max(1, min(35, int(os.getenv("ASUPAN_DTK_COUNT", "20"))))
BOOT_PATH = "/@tiktok"
WWW = "https://www.tiktok.com"
SEARCH_REFERER = WWW + "/search"

BREAKER_THRESHOLD = int(os.getenv("ASUPAN_DTK_BREAKER", "5"))
BREAKER_COOLDOWN = float(os.getenv("ASUPAN_DTK_BREAKER_COOLDOWN", "120"))
BOOT_MAX_TRIES = int(os.getenv("ASUPAN_DTK_BOOT_TRIES", "3"))
RISK_STATUS_CODES = frozenset({10000, 10101})
LOGIN_WALL_STATUS = 2483

def _quiet_dtk_logging() -> None:
    try:
        import structlog

        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    except Exception:
        pass


_quiet_dtk_logging()

WAF_MAX_NONCE = 2_000_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ACCEPT_LANGUAGE = os.getenv(
    "ASUPAN_DTK_ACCEPT_LANGUAGE", "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"
)
PROFILE_REGION = os.getenv("ASUPAN_DTK_REGION", "US")
PRIORITY_REGION = os.getenv("ASUPAN_DTK_PRIORITY_REGION", "ID")
PROFILE_TIMEZONE = os.getenv("ASUPAN_DTK_TIMEZONE", "Asia/Jakarta")
PROFILE_LANGUAGE = os.getenv("ASUPAN_DTK_LANGUAGE", "id")
PROFILE_BROWSER_LANGUAGE = os.getenv("ASUPAN_DTK_BROWSER_LANGUAGE", "id-ID")

PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": ACCEPT_LANGUAGE,
    "Referer": WWW + "/",
}

API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": ACCEPT_LANGUAGE,
    "Referer": SEARCH_REFERER,
}

_WCI_RE = re.compile(r'(?is)<[^>]+\bid="wci"[^>]*class="([^"]*)"')
_CS_RE = re.compile(r'(?is)<[^>]+\bid="cs"[^>]*class="([^"]*)"')
_RCI_RE = re.compile(r'(?is)<[^>]+\bid="rci"[^>]*class="([^"]*)"')
_RS_RE = re.compile(r'(?is)<[^>]+\bid="rs"[^>]*class="([^"]*)"')
_RS_ID_RE = re.compile(r'(?is)<[^>]+\bid="rs_id"[^>]*class="([^"]*)"')


class DtkSearchError(RuntimeError):
    pass


class SlardarBlocked(DtkSearchError):
    pass


class _State:
    def __init__(self) -> None:
        self.session: AsyncSession | None = None
        self.lock = asyncio.Lock()
        self.cookies: dict[str, str] = {}
        self.booted = False
        self.failures = 0
        self.disabled_until = 0.0
        self.signer = None
        self.fingerprint = None
        self.profile = None


_state = _State()


def configured() -> bool:
    return ENABLED


def available() -> bool:
    return ENABLED and time.monotonic() >= _state.disabled_until


def _trip_breaker() -> None:
    _state.failures += 1
    if _state.failures >= BREAKER_THRESHOLD:
        _state.disabled_until = time.monotonic() + BREAKER_COOLDOWN
        _state.failures = 0
        log.warning(
            "[ASUPAN] dtk search breaker open | cooldown=%.0fs", BREAKER_COOLDOWN
        )


def _reset_breaker() -> None:
    _state.failures = 0
    _state.disabled_until = 0.0


def _b64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))


def solve_waf_challenge(html: str) -> dict[str, str]:
    wci = _WCI_RE.search(html or "")
    cs = _CS_RE.search(html or "")
    if not wci or not cs:
        return {}
    name = wci.group(1).strip()
    try:
        cfg = json.loads(_b64(cs.group(1).strip()).decode("utf-8"))
        prefix = _b64(cfg["v"]["a"])
        expect = _b64(cfg["v"]["c"]).hex()
    except Exception as exc:
        log.debug("[ASUPAN] slardar challenge unreadable | err=%r", exc)
        return {}

    for nonce in range(WAF_MAX_NONCE):
        if hashlib.sha256(prefix + str(nonce).encode("ascii")).hexdigest() == expect:
            cfg["d"] = base64.b64encode(str(nonce).encode("ascii")).decode("ascii")
            blob = json.dumps(cfg, separators=(",", ":")).encode("utf-8")
            out = {name: base64.b64encode(blob).decode("ascii")}
            rci = _RCI_RE.search(html or "")
            rs = _RS_RE.search(html or "")
            if rci and rs and rs.group(1).strip():
                out[rci.group(1).strip()] = rs.group(1).strip()
            rs_id = _RS_ID_RE.search(html or "")
            if rs_id and rs_id.group(1).strip():
                out["waforigin_id"] = rs_id.group(1).strip()
            return out
    return {}


def _bind_signers() -> None:
    if _state.signer is not None:
        return
    from dataclasses import replace
    from dtk.core.types import Platform
    from dtk.platforms.tiktok.params import DEFAULT_PROFILE
    from dtk.signing.base import StaticFingerprint
    from dtk.signing.native.signer import NativeSigner

    _state.signer = NativeSigner(Platform.TIKTOK)
    _state.fingerprint = StaticFingerprint(user_agent=USER_AGENT)
    _state.profile = replace(
        DEFAULT_PROFILE,
        region=PROFILE_REGION,
        timezone=PROFILE_TIMEZONE,
        language=PROFILE_LANGUAGE,
        browser_language=PROFILE_BROWSER_LANGUAGE,
    )


async def _session() -> AsyncSession:
    if _state.session is None:
        _state.session = AsyncSession(impersonate=IMPERSONATE, timeout=TIMEOUT)
    return _state.session


def _harvest(resp) -> None:
    try:
        for key, value in (resp.cookies or {}).items():
            if value:
                _state.cookies[str(key)] = str(value)
    except Exception:
        pass


async def _boot() -> None:
    if _state.booted:
        return
    session = await _session()
    target = WWW + BOOT_PATH
    for _ in range(BOOT_MAX_TRIES):
        try:
            resp = await session.get(
                target, headers=PAGE_HEADERS, cookies=_state.cookies, allow_redirects=True
            )
        except Exception as exc:
            raise DtkSearchError(f"boot request failed: {type(exc).__name__}: {exc}") from exc

        _harvest(resp)
        text = resp.text or ""

        if 'id="wci"' in text:
            solved = solve_waf_challenge(text)
            if solved:
                _state.cookies.update(solved)
                continue
            raise SlardarBlocked("slardar challenge could not be solved")

        if resp.status_code in (401, 403, 429):
            raise SlardarBlocked(f"boot HTTP {resp.status_code} for {BOOT_PATH}")
        if resp.status_code != 200:
            raise DtkSearchError(f"boot HTTP {resp.status_code} for {BOOT_PATH}")
        if "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in text:
            raise SlardarBlocked(f"{BOOT_PATH} came back without page data")

        _state.booted = True
        return

    raise SlardarBlocked("boot exhausted retries after slardar challenges")


async def _fetch(keyword: str, count: int) -> dict:
    from dtk.platforms.tiktok import endpoints, params
    from dtk.signing.base import RequestSpec

    _bind_signers()
    await _boot()

    session = await _session()
    spec_params = params.search_posts_params(
        keyword=keyword, count=count, profile=_state.profile
    )
    spec_params["priority_region"] = PRIORITY_REGION
    spec = RequestSpec(
        method="GET", url=endpoints.TikTokAPIEndpoints.SEARCH_ITEM, params=spec_params
    )
    signed = await _state.signer.sign(spec, identity_fingerprint=_state.fingerprint)
    url = f"{endpoints.TikTokAPIEndpoints.SEARCH_ITEM}?{signed.query}"

    resp = await session.get(url, headers=API_HEADERS, cookies=_state.cookies)
    _harvest(resp)

    if resp.status_code in (401, 403, 429):
        _state.booted = False
        raise SlardarBlocked(f"search HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise DtkSearchError(f"search HTTP {resp.status_code}")

    text = resp.text or ""
    if not text.strip():
        _state.booted = False
        raise SlardarBlocked("search answered with an empty body")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        _state.booted = False
        raise SlardarBlocked("search returned a non-JSON body") from exc
    if not isinstance(payload, dict):
        raise DtkSearchError("search returned unexpected JSON")

    code = payload.get("status_code")
    if code is None:
        code = payload.get("statusCode")
    if code in RISK_STATUS_CODES:
        _state.booted = False
        raise SlardarBlocked(f"search risk control status_code={code}")
    if code == LOGIN_WALL_STATUS:
        _state.booted = False
        raise SlardarBlocked(f"search asked for a login status_code={code}")
    return payload


def _map_items(payload: dict) -> list[dict]:
    out = []
    for item in payload.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        vid = item.get("id")
        if not vid:
            continue
        author = item.get("author") or {}
        out.append(
            {
                "id": str(vid),
                "link": item.get("shareUrl")
                or f"{WWW}/@{author.get('uniqueId') or '_'}/video/{vid}",
                "unique_id": author.get("uniqueId") or "_",
            }
        )
    return out


async def search_videos(keyword: str, count: int = MAX_ITEMS) -> list[dict]:
    if not available():
        return []
    query = (keyword or "").strip()
    if not query:
        return []

    try:
        await asyncio.wait_for(_state.lock.acquire(), timeout=TIMEOUT * 2)
    except asyncio.TimeoutError as exc:
        raise DtkSearchError("signer lock timeout") from exc

    try:
        payload = await _fetch(query, max(1, min(35, count)))
    except Exception:
        _trip_breaker()
        await _reset_session()
        raise
    else:
        _reset_breaker()
    finally:
        _state.lock.release()

    return _map_items(payload)


async def _reset_session() -> None:
    session = _state.session
    _state.session = None
    _state.booted = False
    _state.cookies.clear()
    if session is not None:
        try:
            await session.close()
        except Exception:
            pass


async def close() -> None:
    async with _state.lock:
        await _reset_session()


async def warmup() -> bool:
    if not configured():
        return False
    try:
        async with _state.lock:
            await _boot()
    except Exception as exc:
        log.warning("[ASUPAN] dtk warmup failed | err=%r", exc)
        return False
    return True
