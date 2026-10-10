import re
import time
import logging
from urllib.parse import urlsplit, urlunsplit, urljoin
from curl_cffi import requests as curl_requests

from utils import scrapling_browser as _sb

from .constants import (
    UA,
    _HTTP_TIMEOUT,
    _PREFERRED_HOSTS,
    _DECOY_HOSTS,
    _MIRROR_GROUPS,
    _MIRROR_EXTRA_HOSTS,
    DEBUG_RETROTUBE,
    mirror_domains_for,
)
from .packer import _PACKER_RE, _unpack_eval, _deobfuscate_voe_json

log = logging.getLogger(__name__)


def _dbg(msg, *args):
    if DEBUG_RETROTUBE:
        log.warning("RTDBG | " + msg, *args)


def _host(url: str) -> str:
    try:
        return (url or "").split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    except Exception:
        return ""


def _collect_embed_candidates(html_text: str) -> list:
    """Collect all embed URL candidates; lulustream hosts are prioritized."""
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


def _scrape_post(url: str, _depth: int = 0) -> tuple:
    """Extract (title, thumb_url, [embed_candidates]) from the post page.
    Automatically jumps to the first video post if this is a category/listing page."""
    html_text = ""
    last_err = None
    for attempt in range(2):
        try:
            r = curl_requests.get(
                url,
                headers={"User-Agent": UA},
                impersonate="chrome",
                timeout=_HTTP_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 200:
                html_text = r.text
                break
            _dbg("scrape post non-200 (attempt %s) | %s %s", attempt + 1, url, r.status_code)
        except Exception as e:
            last_err = e
            _dbg("scrape post fetch failed (attempt %s) | %s %r", attempt + 1, url, e)
            if attempt == 0:
                time.sleep(0.8)

    if not html_text:
        raise RuntimeError(f"Failed to fetch post page ({last_err or 'non-200'})")

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

    # Auto-resolve listing/category page:
    # If there are no embeds and this is the first attempt, check for an <article> list.
    if not candidates and _depth == 0:
        articles = re.findall(r'<article\b.*?</article>', html_text, re.S | re.I)
        if articles:
            for art in articles:
                m_href = re.search(r'href="([^"]+)"', art, re.I)
                if m_href:
                    first_post_url = m_href.group(1).strip()
                    # Ensure same host to prevent ad traps.
                    if _host(first_post_url) == _host(url):
                        _dbg("Category page detected. Jumping to first post: %s", first_post_url)
                        return _scrape_post(first_post_url, _depth=1)

    _dbg("post scraped | title=%r candidates=%s", title, candidates)
    return title, thumb, candidates


def _mirror_urls(raw_url: str) -> list:
    """Return identical-path post URLs on mirror domains, excluding the original host.

    Wildcard domains (e.g. 'lendirqu.*') cannot be used as target hosts, so only
    explicit hosts listed in _MIRROR_EXTRA_HOSTS + the origin domain are used.
    """
    try:
        parts = urlsplit(raw_url)
    except Exception:
        return []
    host = (parts.netloc or "").lower().split(":", 1)[0]
    if not host:
        return []

    group = mirror_domains_for(host)
    if not group:
        return []

    hosts = []
    for g in group:
        if g.endswith(".*"):
            # Wildcard: use hosts already verified active for this prefix.
            hosts.extend(_MIRROR_EXTRA_HOSTS.get(g, ()))
        else:
            hosts.append(g)

    out = []
    seen = {host}
    if host.startswith("www."):
        seen.add(host[4:])
    for g in hosts:
        if g in seen:
            continue
        seen.add(g)
        out.append(urlunsplit((parts.scheme, g, parts.path, parts.query, parts.fragment)))
    return out


_VOE_RESOLVE_CACHE: dict = {}
_VOE_CACHE_TTL = 300.0
_VOE_CACHE_MAX = 500


def _prune_voe_cache():
    now = time.time()
    # 1) drop every entry past its TTL
    for k in [k for k, v in list(_VOE_RESOLVE_CACHE.items()) if now - v[1] > _VOE_CACHE_TTL]:
        _VOE_RESOLVE_CACHE.pop(k, None)
    # 2) if still over the cap (all fresh), drop the oldest so the map stays bounded.
    if len(_VOE_RESOLVE_CACHE) > _VOE_CACHE_MAX:
        overflow = len(_VOE_RESOLVE_CACHE) - _VOE_CACHE_MAX
        oldest = sorted(_VOE_RESOLVE_CACHE.items(), key=lambda kv: kv[1][1])[: max(1, overflow)]
        for k, _ in oldest:
            _VOE_RESOLVE_CACHE.pop(k, None)


# ── Cookie clearance & browser session → centralized in utils/scrapling_browser ──
# (one global StealthySession shared by all downloaders; cookie reuse for the
#  fast curl_cffi path). Thin wrappers below keep this extractor's old API.

def _drop_host_cookies(host: str):
    _sb.clear_cookies(host)


def _http_get(url: str, referer: str = ""):
    """Fast GET via curl_cffi, reusing clearance cookies when available."""
    kwargs = {}
    ck = _sb.cookies_for(_host(url))
    if ck:
        kwargs["cookies"] = ck
        _dbg("cookie reuse | %s", _host(url))
    return curl_requests.get(
        url,
        headers={"User-Agent": UA, "Referer": referer or url},
        impersonate="chrome",
        timeout=_HTTP_TIMEOUT,
        allow_redirects=True,
        **kwargs,
    )


def _is_challenge_page(text: str) -> bool:
    """Detect a JS challenge page (DDoS-Guard/Cloudflare) that requires a browser.

    Only verified challenge signatures trigger the browser fallback — standard
    404/server errors must NOT waste ~20s launching a headless browser.
    """
    if not text:
        return False
    low = text.lower()
    return (
        "ddos-guard" in low
        or "checking your browser" in low
        or "challenges.cloudflare.com" in low
        or "just a moment" in low
    )


def _browser_data_usable(data: dict) -> bool:
    """True if browser capture contains real media (not a 403/challenge page).

    A captured `m3u8` from the player request is always usable. Otherwise the
    HTML must hold the obfuscated `<script type="application/json">` block (or a
    packer blob) that `_deobfuscate_voe_json` can decode. A plain DDoS-Guard /
    Cloudflare challenge page must NOT be treated as usable, otherwise it gets
    cached and poisons every subsequent resolve for this embed until TTL expiry.
    """
    if not data:
        return False
    if data.get("m3u8"):
        return True
    html = data.get("html") or ""
    if not html or _is_challenge_page(html) or "chrome-error" in html:
        return False
    return "application/json" in html or bool(_PACKER_RE.search(html))


_VOE_BROWSER_ATTEMPTS = 1


def _fetch_voe_browser(embed_url: str, referer: str = "", timeout_ms: int = 45000) -> dict:
    """Resolve a VOE page via the shared StealthySession (utils/scrapling_browser).

    Returns {"html": str, "m3u8": str|None}. Only returns usable media; never a
    bare challenge/403 page.

    The DDoS-Guard JS challenge on voe.sx is flaky: roughly one attempt in
    several ends on `chrome-error://chromewebdata/` (browser navigation aborted)
    or stays on the challenge page. Retrying the whole fetch recovers those runs
    - the second attempt almost always clears the challenge, so we do not raise
    from a single bad spin of the browser.
    """
    last = {"html": "", "m3u8": None}

    for attempt in range(_VOE_BROWSER_ATTEMPTS):
        holder = {"html": "", "m3u8": None}

        def _on_request(req):
            u = getattr(req, "url", "") or ""
            if ".m3u8" in u and not holder["m3u8"]:
                holder["m3u8"] = u

        def _setup(page):
            try:
                if getattr(page, "_rt_m3u8_hook", False):
                    return
                page.on("request", _on_request)
                page._rt_m3u8_hook = True
            except Exception as e:
                _dbg("failed to attach m3u8 listener | %r", e)

        def _auto(page):
            # Poll for the player JSON. The first (cold) resolve can take ~22s and
            # a transient network hiccup can push it higher (Playwright retries the
            # navigation itself), so the window must be generous. Once the session
            # has cleared DDoS-Guard once, later resolves land in 2-4s.
            deadline = time.time() + min(65.0, max(25.0, timeout_ms / 1000.0 * 1.5))
            while time.time() < deadline:
                if holder["m3u8"]:
                    break
                try:
                    c = page.content()
                except Exception:
                    page.wait_for_timeout(350)
                    continue
                if 'type="application/json"' in c and len(c) > 2000:
                    holder["html"] = c
                    break
                page.wait_for_timeout(350)

        html = _sb.fetch_html(
            embed_url,
            page_action=_auto,
            page_setup=_setup,
            referer=referer,
            timeout_ms=timeout_ms,
        )
        # Only fall back to the returned HTML when it is real media, not a
        # challenge/error page. Returning a 403 body here poisons the resolve
        # cache upstream.
        if not holder["m3u8"] and not (holder["html"] and "application/json" in holder["html"]):
            if html and not _is_challenge_page(html) and "chrome-error" not in html:
                holder["html"] = html

        last = holder
        if _browser_data_usable(holder):
            if attempt:
                _dbg("VOE browser resolve succeeded on attempt %s | %s", attempt + 1, embed_url)
            return holder
        _dbg("VOE browser resolve attempt %s produced no media | %s", attempt + 1, embed_url)

    return last


def warmup_rt_browser() -> bool:
    return _sb.warmup_browser()


def close_rt_browser():
    _sb.close_browser()


def _resolve_embed(embed_url: str, referer: str, prefer_gdrive: bool = True):
    """Resolve an embed URL to (kind, media_url, thumb) with kind in {'hls', 'mp4'}, or (None, None, None)."""
    
    # VOE rotates its player domain via a literal JavaScript redirect.
    # Do not pin an obsolete clone before fetching the canonical embed.
    is_voe = _host(embed_url) in ('voe.sx', 'www.voe.sx')

    text = None
    for attempt in range(2):
        try:
            r = _http_get(embed_url, referer or embed_url)
            if r.status_code == 200:
                text = r.text

                if _is_challenge_page(text):
                    # clearance cookie expired/rejected -> drop it so it is not reused
                    _drop_host_cookies(_host(embed_url))
                    break  # text = challenge -> triggers the browser fallback

                # voe.sx first returns a tiny JS page that redirects to a current
                # player mirror (the host rotates; e.g. teresapoliticallearn.com).
                if is_voe:
                    redirect = re.search(
                        r"window\.location\.href\s*=\s*['\"](https?://[^'\"]+/e/[^'\"?#]+)",
                        text,
                        re.I,
                    )
                    if redirect:
                        player_url = redirect.group(1)
                        player = _http_get(player_url, referer or embed_url)
                        if player.status_code == 200:
                            text = player.text
                            embed_url = player_url
                        else:
                            _dbg("VOE mirror non-200 | %s %s", player_url, player.status_code)
                            # non-200 but body is a challenge -> still hand off to the browser
                            if _is_challenge_page(getattr(player, "text", "") or ""):
                                _drop_host_cookies(_host(player_url))
                                text = player.text
                break

            # non-200: if the body is a challenge page, force the browser path
            body = getattr(r, "text", "") or ""
            if _is_challenge_page(body):
                _drop_host_cookies(_host(embed_url))
                text = body
                break
            _dbg("resolve non-200 | %s %s", embed_url, r.status_code)
        except Exception as e:
            _dbg("resolve fetch failed (attempt %s) | %s %r", attempt + 1, embed_url, e)
            time.sleep(1.0)

    # Fallback: DDoS-Guard/VOE challenge -> stealth browser.
    # Used ONLY when plain HTTP fails or yields no media, so the fast path
    # (2 requests) stays the default.
    #
    # PERFORMANCE GUARD: never open a browser for arbitrary failures. Only when:
    #   a) a VOE embed (`voe.sx`) whose HTML lacks the player JSON block, or
    #   b) the response is a challenge page (DDoS-Guard/Cloudflare "Just a moment").
    # A plain 404/dead page gives up immediately -> no wasted ~20s.
    voe_needs = is_voe and not (text and (_PACKER_RE.search(text) or "application/json" in text))
    needs_browser = bool(voe_needs or _is_challenge_page(text or ""))
    if needs_browser:
        cache_key = embed_url  # referer does not change the HTML -> fewer misses
        cached = _VOE_RESOLVE_CACHE.get(cache_key)
        browser_data = None
        if cached and (time.time() - cached[1]) < _VOE_CACHE_TTL:
            browser_data = cached[0]
            _dbg("VOE cache hit | %s", embed_url)
        else:
            _dbg("plain HTTP failed, resolving via StealthySession | %s", embed_url)
            browser_data = _fetch_voe_browser(embed_url, referer=referer)
            # NEVER cache a failure (403/challenge HTML without JSON) — caching it
            # makes every later resolve for this embed fail until TTL expiry
            # (poison cache).
            if browser_data and _browser_data_usable(browser_data):
                _prune_voe_cache()
                _VOE_RESOLVE_CACHE[cache_key] = (browser_data, time.time())
            else:
                _dbg("browser resolve produced no media, not cached | %s", embed_url)
                browser_data = None

        if browser_data:
            # m3u8 captured from the player request -> use directly, skip deobfuscate
            if browser_data.get("m3u8"):
                _dbg("m3u8 via request capture | %s", browser_data["m3u8"])
                return "hls", browser_data["m3u8"], None
            if browser_data.get("html"):
                text = browser_data["html"]

    if not text:
        return None, None, None

    m = _PACKER_RE.search(text)
    if m:
        try:
            text = _unpack_eval(m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|"))
        except Exception as e:
            _dbg("unpack failed | %s %r", embed_url, e)

    # VOE / its clones (miaw.lol): obfuscated JSON config -> m3u8 (source) / mp4.
    voe_hls, voe_mp4 = _deobfuscate_voe_json(text)
    if voe_hls:
        return "hls", voe_hls, None
    if voe_mp4:
        return "mp4", voe_mp4, None

    # mumu.watch: MASTER_URL stored plaintext in JS with escaped slashes (\/).
    if "mumu.watch" in embed_url or "m-cdn.video" in text:
        mm = re.search(r'MASTER_URL\s*=\s*"([^"]+)"', text)
        if mm:
            return "hls", mm.group(1).replace("\\/", "/"), None
        mm2 = re.search(r'https?:\\/\\/[^"\']+?master\.m3u8', text)
        if mm2:
            return "hls", mm2.group(0).replace("\\/", "/"), None

    # HTML5 <video>/<source> (e.g. lordfile.site): direct mp4, URL may contain spaces
    for m in re.finditer(r'<(?:source|video)\b[^>]*\bsrc=["\']([^"\']+)["\']', text, re.I):
        u = m.group(1).strip()
        if re.search(r"\.(?:mp4|m3u8|webm)(?:\?|#|$)", u, re.I):
            u = u.replace(" ", "%20")
            return ("hls" if ".m3u8" in u.lower() else "mp4"), u, None

    # db.fbplay.vip: Google Drive ID proxied to HLS (with 1x1 PNG segments from tiktokcdn).
    # Bypass directly to original Google Drive for fast and complete downloads.
    if prefer_gdrive:
        m_gid = re.search(r'db\.fbplay\.vip/embed/video/([a-zA-Z0-9_-]{20,})', embed_url, re.I)
        if m_gid:
            return "gdrive", m_gid.group(1), None

    # nontonvideo.xyz / JWPlayer relative path: /stream/...
    # (can be escaped quotes: \'file\':\'/stream/...\')
    stream_m = re.search(r'''['"]?file['"]?\s*:\s*['"](/stream/[^'"]+)['"]''', text)
    if not stream_m:
        stream_m = re.search(r'''\\['"]file\\['"]\s*:\s*\\['"](/stream/[^\\'"]+)\\['"]''', text)
    if stream_m:
        stream_path = stream_m.group(1)
        full_stream_url = urljoin(embed_url, stream_path)
        return "mp4", full_stream_url, None

    # JWPlayer m3u8
    m3u8 = re.findall(r'file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', text)
    if not m3u8:
        m3u8 = re.findall(r'https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*', text)
    if m3u8:
        thumbs = re.findall(r'image\s*:\s*["\'](https?://[^"\']+)["\']', text)
        return "hls", m3u8[0], (thumbs[0] if thumbs else None)

    # JWPlayer mp4
    mp4 = re.findall(r'file\s*:\s*["\'](https?://[^"\']+\.mp4[^"\']*)["\']', text)
    if not mp4:
        mp4 = re.findall(r'https?://[^"\'\s\\]+\.mp4[^"\'\s\\]*', text)
    for u in mp4:
        if any(_host(u) == d or _host(u).endswith("." + d) for d in _DECOY_HOSTS):
            continue
        return "mp4", u, None

    _dbg("no media | %s", embed_url)
    return None, None, None
