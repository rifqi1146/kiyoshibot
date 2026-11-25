import random
import asyncio
import logging
from contextlib import asynccontextmanager
import json
import time
import os
import aiohttp
from curl_cffi import requests as curl_requests
from utils.http import get_http_session
from utils import scrapling_browser as _sb
from . import tiktoksource
from .constants import DEFAULT_ASUPAN_KEYWORDS, ASUPAN_MAX_VIDEO_BYTES
log = logging.getLogger(__name__)
logging.getLogger("scrapling").setLevel(logging.ERROR)

TIKWM_HOME = "https://www.tikwm.com/"
TIKWM_SEARCH_API = "https://www.tikwm.com/api/feed/search"
TIKWM_RESOLVE_API = "https://www.tikwm.com/api/"
CF_COOKIE_FILE = os.path.join("data", "tikwm_cf.json")

CF_COOKIE_TTL = 25 * 60

_RATE_LIMITED = object()


class _RateLimited(Exception):
    pass

_VIDEO_POOLS: dict[str, list[dict]] = {}
_DEFAULT_SCRAPER_POOL: list[dict] = []
_DEAD_KEYWORDS: dict[str, float] = {}
_RECENT_KEYWORDS: list[str] = []
_DEFAULT_REFILL_TASK: asyncio.Task | None = None
_DEFAULT_KEYWORD: str | None = None
_DEFAULT_KEYWORD_SCRAPER: str | None = None
_POOL_LOCKS: dict[str, asyncio.Lock] = {}
_DEFAULT_META_LOCK = asyncio.Lock()
_VIDEO_POOLS_MAX = int(os.getenv("ASUPAN_POOL_MAX", "64"))
_POOL_LOCKS_MAX = int(os.getenv("ASUPAN_POOL_LOCKS_MAX", "64"))
_CF_SESSION: dict | None = None

_API_LOCK = asyncio.Lock()
_LAST_API_TS = 0.0
API_MIN_INTERVAL = 1.1


@asynccontextmanager
async def _api_gate():
    global _LAST_API_TS
    async with _API_LOCK:
        wait = API_MIN_INTERVAL - (time.monotonic() - _LAST_API_TS)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            yield
        finally:
            _LAST_API_TS = time.monotonic()

def _load_cf_session() -> dict | None:
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
        log.debug("Cloudflare session saved | cookies=%s", len(jar))
    except Exception as e:
        log.debug("Failed to save Cloudflare session: %r", e)


def _cf_session_fresh(data: dict | None) -> bool:
    if not data or not data.get("cookies") or not data.get("ua"):
        return False
    try:
        return (time.time() - float(data.get("ts", 0))) < CF_COOKIE_TTL
    except Exception:
        return False


def _map_videos(data: dict) -> list[dict]:
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
        log.info("curl_cffi search HTTP %s (cookie likely expired)", resp.status_code)
        return None

    try:
        data = resp.json()
    except Exception as e:
        log.warning("curl_cffi search returned non-JSON (likely a CF challenge): %r", e)
        return None

    if data.get("code") == -1:
        raise _RateLimited(data.get("msg") or "Free Api Limit")

    return _map_videos(data)


def _fetch_api_in_browser(query: str) -> list[dict]:
    result_container = []

    def page_action(page):
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)

        try:
            cookies = page.context.cookies()
            ua = page.evaluate("navigator.userAgent")
            if cookies and ua:
                _save_cf_session(cookies, ua)
        except Exception as e:
            log.debug("Failed to capture Cloudflare session: %r", e)

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

                // Drop photo/slideshow entries and return plain MP4 videos only
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

    _sb.fetch_html(
        TIKWM_HOME,
        timeout_ms=40000,
        page_action=page_action,
        solve_cloudflare=True,
    )

    return result_container


async def _search_keyword(query: str) -> list[dict]:
    for _ in range(3):
        cf = _load_cf_session()
        if cf is None:
            break
        if not _cf_session_fresh(cf):
            log.debug("Cloudflare session past TTL, retrying over curl anyway")

        async with _api_gate():
            cf = _load_cf_session() or cf
            try:
                items = await asyncio.to_thread(_search_via_curl, query, cf)
            except _RateLimited:
                items = _RATE_LIMITED

        if items is _RATE_LIMITED:
            log.warning("Search keyword=%r hit the rate limit, retrying", query)
            await asyncio.sleep(API_MIN_INTERVAL)
            continue
        if items is not None:
            return items
        break
    else:
        log.warning("Search keyword=%r still rate-limited, skipping", query)
        return []

    async with _api_gate():
        cf = _load_cf_session()
        if cf is not None:
            try:
                items = await asyncio.to_thread(_search_via_curl, query, cf)
            except _RateLimited:
                items = None
            if items is not None:
                return items
        return await asyncio.to_thread(_fetch_api_in_browser, query)


async def _prime_and_get_url(video_item: dict) -> str | None:
    target_url = f"https://www.tiktok.com/@{video_item['unique_id']}/video/{video_item['id']}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }

    session = await get_http_session()
    for attempt in range(2):
        rate_limited = False
        try:
            async with _api_gate():
                async with session.post(
                    TIKWM_RESOLVE_API,
                    headers=headers,
                    data={"url": target_url, "hd": "1"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        log.debug("Resolve HTTP %s | id=%s", resp.status, video_item.get("id"))
                        continue
                    data = await resp.json()
            if data.get("code") == 0 and data.get("data"):
                vid_data = data["data"]
                if vid_data.get("images"):
                    return None
                url = vid_data.get("play") or vid_data.get("hdplay") or vid_data.get("wmplay")
                if url and not url.startswith("/"):
                    return url
                log.debug("Resolve returned a relative/odd URL | id=%s url=%s", video_item.get("id"), str(url)[:60])
            else:
                code = data.get("code")
                log.debug("Resolve failed | id=%s code=%s msg=%s", video_item.get("id"), code, data.get("msg"))
                if code == -1:
                    rate_limited = True
        except Exception as e:
            log.debug("Resolve error | id=%s err=%r", video_item.get("id"), e)
        if rate_limited and attempt == 0:
            await asyncio.sleep(1.5)
    return None
def _lock_busy(lock: asyncio.Lock) -> bool:
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
    pool = _VIDEO_POOLS.get(norm)
    if not pool:
        return None
    item = pool.pop(random.randrange(len(pool)))
    if not pool:
        _VIDEO_POOLS.pop(norm, None)
    return item


def _prune_pools() -> None:
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
                log.info("Filling asupan pool | query=%s", q)
                new_videos = await _search_keyword(q)
                if not new_videos:
                    return None
                _VIDEO_POOLS[norm] = new_videos
                log.info("Asupan pool keyword=%r filled: %s videos", q, len(new_videos))
                return _pop_pool(norm)
        finally:
            if norm not in _VIDEO_POOLS and _POOL_LOCKS.get(norm) is lock and not _lock_busy(lock):
                _POOL_LOCKS.pop(norm, None)

    async with _DEFAULT_META_LOCK:
        if _DEFAULT_KEYWORD:
            item = _pop_pool(_DEFAULT_KEYWORD.lower())
            if item:
                return item

        for _ in range(3):
            kw = random.choice(DEFAULT_ASUPAN_KEYWORDS)
            log.info("Filling asupan pool | query=%s", kw)
            new_videos = await _search_keyword(kw)
            if new_videos:
                _DEFAULT_KEYWORD = kw
                _VIDEO_POOLS[kw.lower()] = new_videos
                log.info("Asupan pool keyword=%r filled: %s videos", kw, len(new_videos))
                return _pop_pool(kw.lower())

        return None


def _pick_default_keyword() -> str:
    now = time.monotonic()
    for k in [k for k, ts in _DEAD_KEYWORDS.items() if now - ts > 600]:
        _DEAD_KEYWORDS.pop(k, None)
    recent = set(_RECENT_KEYWORDS)
    candidates = [k for k in DEFAULT_ASUPAN_KEYWORDS if k not in _DEAD_KEYWORDS and k not in recent]
    if not candidates:
        _RECENT_KEYWORDS.clear()
        candidates = [k for k in DEFAULT_ASUPAN_KEYWORDS if k not in _DEAD_KEYWORDS]
    if not candidates:
        _DEAD_KEYWORDS.clear()
        candidates = DEFAULT_ASUPAN_KEYWORDS
    return random.choice(candidates)


async def _fill_default_scraper_pool_locked() -> None:
    global _DEFAULT_KEYWORD_SCRAPER
    for _ in range(5):
        kw = _pick_default_keyword()
        log.info("Filling asupan pool (scraper) | query=%s", kw)
        new_videos = await tiktoksource.search_videos(kw)
        if not new_videos:
            _DEAD_KEYWORDS[kw] = time.monotonic()
            continue
        random.shuffle(new_videos)
        for item in new_videos:
            item["_kw"] = kw
        _DEFAULT_KEYWORD_SCRAPER = kw
        _RECENT_KEYWORDS.append(kw)
        if len(_RECENT_KEYWORDS) > 30:
            _RECENT_KEYWORDS.pop(0)
        _DEFAULT_SCRAPER_POOL.extend(new_videos)
        log.info("Asupan pool (scraper) keyword=%r filled: %s videos (pool=%s)", kw, len(new_videos), len(_DEFAULT_SCRAPER_POOL))
        break


async def _refill_default_scraper_bg() -> None:
    try:
        async with _DEFAULT_META_LOCK:
            if len(_DEFAULT_SCRAPER_POOL) <= 4:
                await _fill_default_scraper_pool_locked()
    except Exception as e:
        log.debug("Background default pool refill failed | err=%r", e)


def _schedule_default_scraper_refill() -> None:
    global _DEFAULT_REFILL_TASK
    if _DEFAULT_REFILL_TASK is not None and not _DEFAULT_REFILL_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _DEFAULT_REFILL_TASK = loop.create_task(_refill_default_scraper_bg())
    except RuntimeError:
        pass


def _pop_default_scraper_pool() -> tuple[dict | None, str | None]:
    if not _DEFAULT_SCRAPER_POOL:
        return None, None
    item = _DEFAULT_SCRAPER_POOL.pop(0)
    if len(_DEFAULT_SCRAPER_POOL) <= 4:
        _schedule_default_scraper_refill()
    return item, item.get("_kw") or _DEFAULT_KEYWORD_SCRAPER

async def warm_default_pool() -> None:
    if not tiktoksource.configured():
        return
    async with _DEFAULT_META_LOCK:
        if not _DEFAULT_SCRAPER_POOL:
            await _fill_default_scraper_pool_locked()
async def _acquire_item_scraper(keyword: str | None) -> tuple[dict | None, str | None]:
    if keyword and keyword.strip():
        q = keyword.strip()
        norm = "scraper:" + q.lower()
        _prune_pools()
        lock = _POOL_LOCKS.setdefault(norm, asyncio.Lock())
        try:
            async with lock:
                item = _pop_pool(norm)
                if item:
                    return item, q
                log.info("Filling asupan pool (scraper) | query=%s", q)
                new_videos = await tiktoksource.search_videos(q)
                if not new_videos:
                    return None, q
                _VIDEO_POOLS[norm] = new_videos
                log.info("Asupan pool (scraper) keyword=%r filled: %s videos", q, len(new_videos))
                return _pop_pool(norm), q
        finally:
            if norm not in _VIDEO_POOLS and _POOL_LOCKS.get(norm) is lock and not _lock_busy(lock):
                _POOL_LOCKS.pop(norm, None)

    item, kw_used = _pop_default_scraper_pool()
    if item:
        return item, kw_used

    async with _DEFAULT_META_LOCK:
        item, kw_used = _pop_default_scraper_pool()
        if item:
            return item, kw_used
        await _fill_default_scraper_pool_locked()
        return _pop_default_scraper_pool()


async def _download_url_bytes(url: str, max_bytes: int = ASUPAN_MAX_VIDEO_BYTES) -> bytes | None:
    if not url:
        return None
    try:
        session = await get_http_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            ct = (resp.headers.get("Content-Type") or "").lower()
            if any(bad in ct for bad in ("text/html", "application/json", "text/plain")):
                return None
            cl = int(resp.headers.get("Content-Length") or 0)
            if cl > max_bytes:
                return None
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    return None
            data = bytes(buf)
            if len(data) < 10_000:
                return None
            if b"ftyp" not in data[:32] and not ct.startswith("video/") and "octet-stream" not in ct:
                return None
            return data
    except Exception as e:
        log.debug("Asupan URL download failed | err=%r", e)
        return None


async def fetch_asupan_media(keyword: str | None = None) -> dict:
    if tiktoksource.configured():
        last_vid = None
        last_kw = None
        try:
            for _ in range(4):
                vid, kw_used = await _acquire_item_scraper(keyword)
                if not vid:
                    break
                last_vid = vid
                last_kw = kw_used
                data = await tiktoksource.download_video(vid, max_bytes=ASUPAN_MAX_VIDEO_BYTES)
                if data:
                    log.debug("[ASUPAN] fetched source=scraper | keyword=%r id=%s bytes=%d", kw_used, vid.get("id"), len(data))
                    return {
                        "bytes": data,
                        "width": vid.get("width") or 0,
                        "height": vid.get("height") or 0,
                        "duration": vid.get("duration") or 0,
                        "source": "scraper",
                        "keyword": kw_used,
                        "id": vid.get("id"),
                    }
            if last_vid:
                url = await _prime_and_get_url(last_vid)
                if url:
                    data = await _download_url_bytes(url, ASUPAN_MAX_VIDEO_BYTES)
                    if data:
                        log.debug("[ASUPAN] fetched source=scraper | keyword=%r id=%s bytes=%d", last_kw, last_vid.get("id"), len(data))
                        return {
                            "bytes": data,
                            "width": last_vid.get("width") or 0,
                            "height": last_vid.get("height") or 0,
                            "duration": last_vid.get("duration") or 0,
                            "source": "scraper",
                            "keyword": last_kw,
                            "id": last_vid.get("id"),
                        }
                    log.debug("[ASUPAN] fetched source=scraper | keyword=%r id=%s", last_kw, last_vid.get("id"))
                    return {"url": url, "source": "scraper", "keyword": last_kw, "id": last_vid.get("id")}
            log.info("[ASUPAN] source=scraper empty -> falling back to tikwm")
        except Exception as e:
            log.warning("[ASUPAN] source=scraper error (%r) -> falling back", e)

    for _ in range(3):
        video_item = await _acquire_item(keyword)
        if not video_item:
            break
        final_url = await _prime_and_get_url(video_item)
        if final_url:
            data = await _download_url_bytes(final_url, ASUPAN_MAX_VIDEO_BYTES)
            if data:
                log.debug("[ASUPAN] fetched source=tikwm | id=%s bytes=%d", video_item.get("id"), len(data))
                return {
                    "bytes": data,
                    "width": 0,
                    "height": 0,
                    "duration": 0,
                    "source": "tikwm",
                    "keyword": keyword,
                    "id": video_item.get("id"),
                }
            log.debug("[ASUPAN] fetched source=tikwm | id=%s", video_item.get("id"))
            return {"url": final_url, "source": "tikwm", "keyword": keyword, "id": video_item.get("id")}
        log.warning("Failed to resolve video id=%s, trying another video", video_item.get("id"))

    raise RuntimeError("Failed to get stream URL from asupan video")


async def fetch_asupan_tikwm(keyword: str | None = None) -> str:
    if tiktoksource.configured():
        try:
            for _ in range(4):
                vid, kw_used = await _acquire_item_scraper(keyword)
                if not vid:
                    break
                url = await _prime_and_get_url(vid)
                if url:
                    log.info("[ASUPAN] source=scraper | keyword=%r id=%s", kw_used, vid.get("id"))
                    return url
            log.info("[ASUPAN] source=scraper empty -> falling back to tikwm")
        except Exception as e:
            log.warning("[ASUPAN] source=scraper error (%r) -> falling back", e)

    for _ in range(3):
        video_item = await _acquire_item(keyword)
        if not video_item:
            break

        final_url = await _prime_and_get_url(video_item)
        if final_url:
            log.info("[ASUPAN] source=tikwm | id=%s", video_item.get("id"))
            return final_url
        log.warning("Failed to resolve video id=%s, trying another video", video_item.get("id"))

    raise RuntimeError("Failed to get stream URL from asupan video")
