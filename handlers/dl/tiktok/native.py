import asyncio
import copy
import logging
import time
from collections import OrderedDict

from .constants import (
    USE_NATIVE,
    NATIVE_TIMEOUT,
    NATIVE_IMPERSONATE,
    NATIVE_CACHE_TTL,
    NATIVE_CACHE_MAX,
    USER_AGENT,
)

log = logging.getLogger(__name__)

_CACHE_TTL = NATIVE_CACHE_TTL
_CACHE_MAX = NATIVE_CACHE_MAX

_session = None
_signer = None
_fingerprint = None
_init_lock = asyncio.Lock()
_locks: dict[str, asyncio.Lock] = {}
_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()

_STATS = {"hits": 0, "misses": 0, "errors": 0, "not_found": 0}

NOT_FOUND_CODES = frozenset({10204, 10216, 10222, 2483})


class ItemNotFound(RuntimeError):
    def __init__(self, video_id: str, code):
        self.video_id = video_id
        self.code = code
        super().__init__(f"TikTok item unavailable (code={code}) id={video_id}")


def enabled() -> bool:
    return USE_NATIVE


def configured() -> bool:
    return USE_NATIVE


def stats() -> dict:
    return {**_STATS, "cached": len(_cache)}


async def _client():
    global _session
    if _session is not None:
        return _session
    async with _init_lock:
        if _session is None:
            from curl_cffi.requests import AsyncSession
            _session = AsyncSession(impersonate=NATIVE_IMPERSONATE, timeout=NATIVE_TIMEOUT)
    return _session


def _bind_signer():
    global _signer, _fingerprint
    if _signer is not None:
        return _signer, _fingerprint
    from dtk.core.types import Platform
    from dtk.signing.base import StaticFingerprint
    from dtk.signing.native.signer import NativeSigner

    _signer = NativeSigner(Platform.TIKTOK)
    _fingerprint = StaticFingerprint(user_agent=USER_AGENT)
    return _signer, _fingerprint


def _purge_expired() -> None:
    now = time.time()
    stale = [k for k, (ts, _) in _cache.items() if now - ts > _CACHE_TTL]
    for key in stale:
        _cache.pop(key, None)


def cache_get(video_id: str) -> dict | None:
    entry = _cache.get(video_id)
    if not entry:
        _STATS["misses"] += 1
        return None
    ts, payload = entry
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(video_id, None)
        _STATS["misses"] += 1
        return None
    _cache.move_to_end(video_id)
    _STATS["hits"] += 1
    return copy.deepcopy(payload)


def cache_put(video_id: str, payload: dict) -> None:
    _purge_expired()
    if video_id not in _cache and len(_cache) >= _CACHE_MAX:
        overflow = len(_cache) - _CACHE_MAX + 1
        for stale in list(_cache)[:overflow]:
            _cache.pop(stale, None)
    _cache[video_id] = (time.time(), copy.deepcopy(payload))
    _cache.move_to_end(video_id)


def cache_clear() -> None:
    _cache.clear()
    _locks.clear()


async def fetch_item(video_id: str) -> dict:
    from dtk.platforms.tiktok import endpoints, params
    from dtk.signing.base import RequestSpec

    vid = str(video_id or "").strip()
    if not vid:
        raise RuntimeError("TikTok video id empty")
    signer, fingerprint = _bind_signer()
    session = await _client()

    spec = RequestSpec(
        method="GET",
        url=endpoints.TikTokAPIEndpoints.POST_DETAIL,
        params=params.content_detail_params(item_id=vid),
    )
    signed = await signer.sign(spec, identity_fingerprint=fingerprint)
    url = f"{endpoints.TikTokAPIEndpoints.POST_DETAIL}?{signed.query}"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.tiktok.com/",
        "Accept": "application/json, text/plain, */*",
    }

    last_err = None
    for attempt in range(2):
        try:
            resp = await session.get(url, headers=headers)
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                await asyncio.sleep(0.2)
                continue
            raise RuntimeError(f"TikTok native detail request failed: {last_err}") from exc

        if resp.status_code in (401, 403, 429):
            last_err = f"HTTP {resp.status_code}"
            if attempt == 0:
                await asyncio.sleep(0.4)
                continue
            raise RuntimeError(f"TikTok native detail blocked: {last_err}")

        if resp.status_code != 200:
            raise RuntimeError(f"TikTok native detail HTTP {resp.status_code}")

        body = (resp.text or "").strip()
        if not body:
            last_err = "empty body"
            if attempt == 0:
                await asyncio.sleep(0.2)
                continue
            raise RuntimeError("TikTok native detail returned empty body")

        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"TikTok native detail returned non-JSON payload: {exc}") from exc

        code = data.get("statusCode", data.get("status_code"))
        item = (data.get("itemInfo") or {}).get("itemStruct") or {}
        if not isinstance(item, dict) or not item.get("id"):
            if code in NOT_FOUND_CODES:
                _STATS["not_found"] += 1
                raise ItemNotFound(vid, code)
            raise RuntimeError(f"TikTok native detail has no itemStruct (code={code})")
        return item

    raise RuntimeError(f"TikTok native detail failed: {last_err}")


async def fetch_detail(video_id: str, parse) -> dict:
    vid = str(video_id or "").strip()
    cached = cache_get(vid)
    if cached is not None:
        return cached

    lock = _locks.setdefault(vid, asyncio.Lock())
    async with lock:
        cached = cache_get(vid)
        if cached is not None:
            return cached

        started = time.monotonic()
        try:
            item = await fetch_item(vid)
            media = parse(item)
        except Exception:
            _STATS["errors"] += 1
            raise
        finally:
            _locks.pop(vid, None)

        media["aweme_id"] = vid
        media["source"] = "native"
        cache_put(vid, media)
        log.info(
            "TikTok native detail resolved | id=%s kind=%s elapsed=%.3fs",
            vid, media.get("kind"), time.monotonic() - started,
        )
        return media


async def close() -> None:
    global _session, _signer, _fingerprint
    session = _session
    _session = None
    _signer = None
    _fingerprint = None
    cache_clear()
    if session is not None:
        try:
            await session.close()
        except Exception as exc:
            log.debug("TikTok native session close failed | err=%r", exc)
