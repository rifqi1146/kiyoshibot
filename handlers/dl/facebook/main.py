import os
import re
import json
import uuid
import asyncio
import aiohttp
import aiofiles
import logging
from html import unescape as _html_unescape
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, unquote, urljoin, urlsplit, urlunsplit
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import check_media_size_limit, FileSizeLimitExceeded
from handlers.dl.progress import edit_status, TransferStats
try:
    from handlers.dl.constants import BASE_DIR
except Exception:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from handlers.dl.proc import spawn_process, DEVNULL, PIPE, resolve_aria2

log = logging.getLogger(__name__)

COOKIES_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "..", "data", "cookies.txt"))
DEBUG_FACEBOOK = os.getenv("FACEBOOK_DEBUG", "0").strip().lower() in ("1", "true", "on", "yes")
FACEBOOK_ARIA2_THRESHOLD = int(os.getenv("FACEBOOK_ARIA2_THRESHOLD", str(32 * 1024 * 1024)))
FACEBOOK_DOWNLOAD_ENGINE = os.getenv("FACEBOOK_DOWNLOAD_ENGINE", "auto").strip().lower()
FACEBOOK_ARIA2_POLL = float(os.getenv("FACEBOOK_ARIA2_POLL", "0.2"))
FACEBOOK_CHUNK_SIZE = int(os.getenv("FACEBOOK_CHUNK_SIZE", str(512 * 1024)))
FACEBOOK_PROGRESS_MIN_BYTES = int(os.getenv("FACEBOOK_PROGRESS_MIN_BYTES", str(8 * 1024 * 1024)))
FACEBOOK_PHOTO_CONCURRENCY = max(1, int(os.getenv("FACEBOOK_PHOTO_CONCURRENCY", "6")))
FACEBOOK_THUMB_WIDTH = int(os.getenv("FACEBOOK_THUMB_WIDTH", "320"))
_EARLY_EXIT_MIN_BYTES = 128 * 1024
_COOKIE_HEADER_CACHE = None
_COOKIE_CACHE_MTIME = None

WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_DESC_RE = re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', re.I)
VIDEO_SIGNAL_RE = re.compile(r'"(?:progressive_url|browser_native_hd_url|browser_native_sd_url)"')

SHARE_RE = re.compile(r"https?://(?:(?:www|m)\.)?facebook\.com/share/(?:r|v|p)/([a-zA-Z0-9]+)", re.I)
CONTENT_RE = re.compile(r"https?://(?:(?:www|m|mbasic)\.)?facebook\.com/" r"(?:watch/?\?(?:[^&]*&)*v=|share/(?:[rvp]/)?|(?:reel|videos?|posts?|permalink)/|groups/[^/]+/(?:posts|permalink)/|[^/]+/(?:videos|posts|reels?|permalink)/)" r"([a-zA-Z0-9]+)", re.I)
HD_URL_PATTERN = re.compile(r'"progressive_url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*,\s*"failure_reason"\s*:\s*[^,]+\s*,\s*"metadata"\s*:\s*\{\s*"quality"\s*:\s*"HD"\s*\}', re.S)
SD_URL_PATTERN = re.compile(r'"progressive_url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*,\s*"failure_reason"\s*:\s*[^,]+\s*,\s*"metadata"\s*:\s*\{\s*"quality"\s*:\s*"SD"\s*\}', re.S)
BROWSER_NATIVE_HD_URL_PATTERN = re.compile(r'"browser_native_hd_url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', re.S)
BROWSER_NATIVE_SD_URL_PATTERN = re.compile(r'"browser_native_sd_url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', re.S)
OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I)
TWITTER_TITLE_RE = re.compile(r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']', re.I)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

FACEBOOK_COOKIE_DOMAINS = (
    "facebook.com",
    ".facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
)

FACEBOOK_COOKIE_NAMES = {
    "c_user",
    "xs",
    "fr",
    "datr",
    "sb",
    "wd",
    "dpr",
    "locale",
    "presence",
    "vpd",
    "m_pixel_ratio",
    "fbl_st",
    "wl_cbv",
}

def is_facebook_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return False
    host = (urlparse(text).hostname or "").lower()
    if "facebook.com" in host or host in {"fb.watch", "fb.com", "www.fb.com"}:
        return True
    return "facebook.com/" in text.lower() or "fb.watch/" in text.lower()

def _dbg(msg: str, *args):
    if DEBUG_FACEBOOK:
        log.warning("FBDBG | " + msg, *args)

def _clip(text: str, limit: int = 400) -> str:
    text = str(text or "").replace("\n", "\\n").replace("\r", "\\r")
    return text if len(text) <= limit else text[:limit] + "...<cut>"

def _write_debug_file(prefix: str, content: bytes | str) -> str:
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        path = os.path.join(TMP_DIR, f"{prefix}_{uuid.uuid4().hex}.txt")
        if isinstance(content, (bytes, bytearray)):
            with open(path, "wb") as f:
                f.write(content)
        else:
            with open(path, "w", encoding="utf-8", errors="ignore") as f:
                f.write(content)
        _dbg("debug file written | path=%s", path)
        return path
    except Exception as e:
        _dbg("failed writing debug file | err=%r", e)
        return ""

def _cookie_domain_ok(domain: str) -> bool:
    d = (domain or "").strip().lower()
    return any(d == x or d.endswith(x) for x in FACEBOOK_COOKIE_DOMAINS)

def _load_cookie_header(path: str) -> str:
    global _COOKIE_HEADER_CACHE, _COOKIE_CACHE_MTIME
    if not path or not os.path.exists(path):
        _dbg("cookie file not found | path=%s", path)
        _COOKIE_HEADER_CACHE = ""
        _COOKIE_CACHE_MTIME = None
        return _COOKIE_HEADER_CACHE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if _COOKIE_HEADER_CACHE is not None and _COOKIE_CACHE_MTIME == mtime:
        return _COOKIE_HEADER_CACHE
    jar = {}

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif line.startswith("#"):
                    continue

                parts = line.split("\t")

                if len(parts) >= 7:
                    domain = (parts[0] or "").strip().lower()
                    name = (parts[5] or "").strip()
                    value = (parts[6] or "").strip()

                    if not name or not value:
                        continue
                    if not _cookie_domain_ok(domain):
                        continue
                    if name not in FACEBOOK_COOKIE_NAMES:
                        continue

                    jar[name] = value
                    continue

                if "=" in line and "\t" not in line and not line.lower().startswith(("http://", "https://")):
                    name, value = line.split("=", 1)
                    name = name.strip()
                    value = value.strip()

                    if name in FACEBOOK_COOKIE_NAMES and value:
                        jar[name] = value

        ordered = [
            "c_user",
            "xs",
            "fr",
            "datr",
            "sb",
            "wd",
            "dpr",
            "locale",
            "presence",
            "vpd",
            "m_pixel_ratio",
            "fbl_st",
            "wl_cbv",
        ]

        header = "; ".join(f"{k}={jar[k]}" for k in ordered if jar.get(k))

        _dbg(
            "facebook cookie loaded | path=%s pairs=%s header_len=%s has_login=%s",
            path,
            len(jar),
            len(header),
            bool(jar.get("c_user") and jar.get("xs")),
        )

        _COOKIE_HEADER_CACHE = header
        _COOKIE_CACHE_MTIME = mtime
        return _COOKIE_HEADER_CACHE

    except Exception as e:
        _dbg("failed to load cookie file | path=%s err=%r", path, e)
        _COOKIE_HEADER_CACHE = ""
        _COOKIE_CACHE_MTIME = None
        return _COOKIE_HEADER_CACHE

def _build_headers(referer: str | None = None, use_cookies: bool = True) -> dict:
    headers = dict(WEB_HEADERS)
    if referer:
        headers["Referer"] = referer
    if use_cookies:
        cookie_header = _load_cookie_header(COOKIES_PATH)
        if cookie_header:
            headers["Cookie"] = cookie_header
    return headers

def _resolve_canonical_fb_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    if "/login" in text:
        p = urlparse(text)
        qs = parse_qs(p.query)
        next_val = (qs.get("next") or [""])[0]
        if next_val:
            return _resolve_canonical_fb_url(unquote(next_val))
    p = urlparse(text)
    qs = parse_qs(p.query)
    fbid = (qs.get("story_fbid") or qs.get("fbid") or qs.get("post_id") or [""])[0]
    author = (qs.get("id") or [""])[0]
    if fbid and author:
        return f"https://www.facebook.com/{author}/posts/{fbid}"
    if fbid:
        return f"https://www.facebook.com/photo/?fbid={fbid}"
    vid = (qs.get("v") or [""])[0]
    if vid:
        return f"https://www.facebook.com/watch/?v={vid}"
    clean = re.sub(r"[?&](?:rdid|share_url|mibextid|ref|__cft__|__tn__)=[^&]*", "", text)
    clean = clean.replace("m.facebook.com", "www.facebook.com").replace("mbasic.facebook.com", "www.facebook.com")
    clean = re.sub(r"/+$", "", clean)
    return clean

def _extract_content_id(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    m = CONTENT_RE.search(text)
    if m:
        value = (m.group(1) or "").strip()
        _dbg("content id from regex | url=%s id=%s", text, value)
        return value
    parsed = urlparse(text)
    qs = parse_qs(parsed.query)
    for qk in ("v", "fbid", "story_fbid", "post_id"):
        val = (qs.get(qk) or [""])[0].strip()
        if val:
            _dbg("content id from query %s | url=%s id=%s", qk, text, val)
            return val
    _dbg("content id not found | url=%s", text)
    return ""

def _normalize_content_url(content_url: str, content_id: str) -> str:
    original = (content_url or "").strip()
    content_url = _resolve_canonical_fb_url(original)
    _dbg("normalize content url | before=%s after=%s content_id=%s", original, content_url, content_id)
    return content_url

async def _follow_share_redirect(url: str) -> str:
    session = await get_http_session()
    headers = _build_headers(use_cookies=False)
    _dbg("follow share redirect start | url=%s cookie=%s", url, bool(headers.get("Cookie")))
    current = url
    for _hop in range(4):
        try:
            async with session.get(current, headers=headers, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False) as resp:
                status = resp.status
                location = resp.headers.get("Location") or ""
                await resp.read()
                if not location:
                    canonical = _resolve_canonical_fb_url(str(resp.url))
                    _dbg("share redirect settled | status=%s final=%s", status, str(resp.url))
                    return canonical
        except Exception as e:
            _dbg("share redirect hop failed | url=%s err=%r", current, e)
            return _resolve_canonical_fb_url(current)
        current = urljoin(current, location)
        low = current.lower()
        if not SHARE_RE.search(current) and "fb.watch" not in low and "/l.php" not in low and "/login" not in low:
            canonical = _resolve_canonical_fb_url(current)
            _dbg("share redirect resolved on hop | status=%s canonical=%s", status, canonical)
            return canonical
        _dbg("share redirect hop | status=%s next=%s", status, _clip(current, 200))
    return _resolve_canonical_fb_url(current)

def _unescape_unicode(text: str) -> str:
    if not text:
        return ""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if i + 5 < n and text[i] == "\\" and text[i + 1] == "u":
            hex_part = text[i + 2:i + 6]
            try:
                out.append(chr(int(hex_part, 16)))
                i += 6
                continue
            except Exception:
                pass
        out.append(text[i])
        i += 1
    from handlers.dl.utils import fix_surrogates
    return fix_surrogates("".join(out))

def _unescape_facebook_url(text: str) -> str:
    return _unescape_unicode((text or "").replace(r"\/", "/"))

def _is_bad_fb_title(text: str) -> bool:
    text = re.sub(r"\s+", " ", (text or "").strip()).lower()
    return text in {"", "facebook", "reel", "watch", "facebook reel", "facebook watch"}

def _clean_fb_title(text: str, fallback: str = "Facebook Video") -> str:
    text = (text or "").strip()
    text = re.sub(r"\s*\|\s*facebook\s*$", "", text, flags=re.I).strip()
    text = re.sub(r"\s*-\s*facebook\s*$", "", text, flags=re.I).strip()
    text = text.replace(r"\n", "\n").replace(r"\t", "\t")
    text = re.sub(r"\s+", " ", text).strip()
    if _is_bad_fb_title(text):
        return fallback
    return text or fallback

def _extract_title_from_html(body: bytes, fallback: str = "Facebook Video") -> str:
    text = body.decode("utf-8", errors="ignore")

    patterns = [
        r'"message"\s*:\s*\{"text"\s*:\s*"([^"]+)"',
        r'"title"\s*:\s*\{"text"\s*:\s*"([^"]+)"',
        r'"creation_story".{0,4000}?"message".{0,1000}?"text"\s*:\s*"([^"]+)"',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        r"<title[^>]*>(.*?)</title>",
    ]

    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if not m:
            continue
        val = m.group(1)
        val = _unescape_facebook_url(val)
        val = _clean_fb_title(val, "")
        if val and not _is_bad_fb_title(val):
            return val

    return fallback


def _unescape_json_str(s: str) -> str:
    if not s:
        return ""
    out = s.replace(r"\/", "/")
    out = out.replace(r"\"", '"').replace("\\\\", "\\")
    out = out.replace(r"\n", "\n").replace(r"\t", "\t")
    return _html_unescape(out)

def _normalize_image_url(url: str) -> str:
    text = (url or "").strip()
    m = re.search(r"([^\?]+/(\d+)_\d+_\d+)(?:_\d+)?\.([a-zA-Z0-9]+)", text)
    if m:
        return f"{m.group(1)}_{m.group(3)}"
    return text.split("?")[0]

def _extract_og_image(text: str) -> str:
    m = OG_IMAGE_RE.search(text or "")
    if not m:
        return ""
    return unquote(_html_unescape(m.group(1)))

def _extract_post_caption(text: str) -> str:
    caption = ""
    m = OG_DESC_RE.search(text or "")
    if m:
        caption = _unescape_json_str(m.group(1))
    if not caption:
        m = OG_TITLE_RE.search(text or "")
        if m:
            caption = _unescape_json_str(m.group(1))
    caption = (caption or "").strip()
    caption = re.sub(r"\s*\|\s*Facebook\s*$", "", caption, flags=re.I).strip()
    caption = re.sub(r"\s*-\s*Facebook\s*$", "", caption, flags=re.I).strip()
    return caption

def _extract_attachment_photos(text: str) -> list[dict]:
    text = text or ""

    for pm in re.finditer(r'<script type="application/json"[^>]*>(.*?)</script>', text, re.S):
        payload = pm.group(1)
        if '"all_subattachments"' not in payload:
            continue
        try:
            data = json.loads(payload)
        except Exception:
            continue

        found_sub = None

        def _walk(o):
            nonlocal found_sub
            if found_sub is not None:
                return
            if isinstance(o, dict):
                sub = o.get("all_subattachments")
                if isinstance(sub, dict) and sub.get("count") and isinstance(sub.get("nodes"), list) and sub.get("nodes"):
                    found_sub = sub
                    return
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)

        _walk(data)
        if not (found_sub and found_sub.get("nodes")):
            continue

        photos: list[dict] = []
        seen_ids: set = set()
        for node in found_sub["nodes"]:
            media = node.get("media") or {}
            if media.get("__typename") != "Photo":
                continue
            photo_id = str(media.get("id") or "")
            if photo_id:
                if photo_id in seen_ids:
                    continue
                seen_ids.add(photo_id)
            best_uri, best_w, best_h = "", 0, 0
            for candidate in (media.get("viewer_image") or {}, media.get("image") or {}):
                uri = candidate.get("uri") or ""
                try:
                    w = int(candidate.get("width") or 0)
                    h = int(candidate.get("height") or 0)
                except (TypeError, ValueError):
                    w = h = 0
                if uri and (w * h > best_w * best_h or not best_uri):
                    best_uri, best_w, best_h = uri, w, h
            if best_uri:
                photos.append({
                    "url": _unescape_json_str(best_uri),
                    "width": best_w,
                    "height": best_h,
                    "photo_id": photo_id,
                    "caption": media.get("accessibility_caption") or "",
                })
        if photos:
            return photos

    found: dict = {}
    for m in re.finditer(
        r'"photo_image"\s*:\s*\{\s*"uri"\s*:\s*"((?:[^"\\]|\\.)+)"[^}]*"height"\s*:\s*(\d+)\s*,\s*"width"\s*:\s*(\d+)\s*\}\s*,\s*"accessibility_caption"\s*:\s*"([^"]*)"',
        text,
    ):
        uri = _unescape_json_str(m.group(1))
        h, w = int(m.group(2)), int(m.group(3))
        key = _normalize_image_url(uri)
        if key not in found or h * w > found[key]["height"] * found[key]["width"]:
            found[key] = {"url": uri, "width": w, "height": h, "photo_id": "", "caption": m.group(4)}

    if not found:
        for m in re.finditer(
            r'"photo_image"\s*:\s*\{\s*"uri"\s*:\s*"((?:[^"\\]|\\.)+)"\s*,\s*"height"\s*:\s*(\d+)\s*,\s*"width"\s*:\s*(\d+)',
            text,
        ):
            uri = _unescape_json_str(m.group(1))
            key = _normalize_image_url(uri)
            found.setdefault(key, {"url": uri, "width": int(m.group(3)), "height": int(m.group(2)), "photo_id": "", "caption": ""})

    return sorted(found.values(), key=lambda d: d["height"] * d["width"], reverse=True)

def _extract_mixed_media(text: str) -> dict:
    text = text or ""
    if '"all_subattachments"' not in text:
        return {"videos": [], "photos": []}
    videos = []
    photos = []
    
    for pm in re.finditer(r'<script type="application/json"[^>]*>(.*?)</script>', text, re.S):
        payload = pm.group(1)
        if '"all_subattachments"' not in payload:
            continue
        try:
            data = json.loads(payload)
        except Exception:
            continue
        
        found_sub = None
        def _walk(o):
            nonlocal found_sub
            if found_sub is not None:
                return
            if isinstance(o, dict):
                sub = o.get("all_subattachments")
                if isinstance(sub, dict) and sub.get("count") and isinstance(sub.get("nodes"), list) and sub.get("nodes"):
                    found_sub = sub
                    return
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)
        
        _walk(data)
        if not (found_sub and found_sub.get("nodes")):
            continue
        
        seen_ids = set()
        for node in found_sub["nodes"]:
            media = node.get("media") or {}
            media_type = media.get("__typename")
            media_id = str(media.get("id") or "")
            
            if not media_id or media_id in seen_ids:
                continue
            seen_ids.add(media_id)
            
            if media_type == "Video":
                videos.append({"id": media_id, "type": "video"})
            elif media_type == "Photo":
                best_uri, best_w, best_h = "", 0, 0
                for candidate in (media.get("viewer_image") or {}, media.get("image") or {}):
                    uri = candidate.get("uri") or ""
                    try:
                        w = int(candidate.get("width") or 0)
                        h = int(candidate.get("height") or 0)
                    except (TypeError, ValueError):
                        w = h = 0
                    if uri and (w * h > best_w * best_h or not best_uri):
                        best_uri, best_w, best_h = uri, w, h
                if best_uri:
                    photos.append({
                        "url": _unescape_json_str(best_uri),
                        "width": best_w,
                        "height": best_h,
                        "photo_id": media_id,
                        "type": "photo"
                    })
        
        if videos or photos:
            return {"videos": videos, "photos": photos}
    
    return {"videos": [], "photos": []}

def _upgrade_image_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return text
    parts = urlsplit(text)
    if not parts.query:
        return text
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "ctp"]
    if len(pairs) == len(parse_qsl(parts.query, keep_blank_values=True)):
        return text
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))



def _strip_cover_crop(url: str) -> str:
    text = (url or "").strip()
    if not text or "ctp=" not in text:
        return text
    parts = urlsplit(text)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k != "ctp"]
    if len(kept) == len(pairs):
        return text
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _ctp_area(url: str) -> int:
    m = re.search(r"[?&]ctp=[ps](\d+)x(\d+)", url or "")
    return int(m.group(1)) * int(m.group(2)) if m else 0


def _collect_photo_urls(text: str, attachment_photos: list[dict] | None = None) -> list[str]:
    raw = [p["url"] for p in (attachment_photos if attachment_photos is not None else _extract_attachment_photos(text)) if p.get("url")]
    og_image = _extract_og_image(text)
    if og_image:
        og_key = _normalize_image_url(og_image)
        matched_idx = next((i for i, u in enumerate(raw) if _normalize_image_url(u) == og_key), None)
        if matched_idx is None:
            raw.insert(0, og_image)
        elif len(raw) == 1:
            og_area = _ctp_area(og_image)
            cur_area = _ctp_area(raw[0])
            if (og_area == 0 and cur_area > 0) or (og_area > cur_area):
                raw[0] = og_image
    return raw

def _sniff_image_ext(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"

def _fix_image_ext(path: str) -> str:
    if not path or not os.path.exists(path):
        return path
    ext = _sniff_image_ext(path)
    if path.lower().endswith(ext):
        return path
    new_path = os.path.splitext(path)[0] + ext
    try:
        os.replace(path, new_path)
        return new_path
    except OSError:
        return path

def _extract_video_duration_from_url(url: str) -> int:
    import base64
    if not url:
        return -1
    m = re.search(r'[?&]efg=([^&]+)', url)
    if not m:
        return -1
    try:
        efg_b64 = unquote(m.group(1))
        efg_json = base64.b64decode(efg_b64).decode('utf-8', errors='ignore')
        efg_data = json.loads(efg_json)
        duration = int(efg_data.get('duration_s', -1))
        _dbg("video url duration parsed | duration_s=%s", duration)
        return duration
    except Exception as e:
        _dbg("video url duration parse failed | err=%r", e)
        return -1

def _find_video_section(body: bytes, video_id: str) -> bytes | None:
    if not video_id:
        _dbg("find section skipped | no video_id")
        return None
    anchor = f"dash_mpd_debug.mpd?v={video_id}".encode()
    start = body.find(anchor)
    if start == -1:
        m = re.search(rb"dash_mpd_debug\.mpd\?v=(\d+)", body)
        if m:
            video_id = m.group(1).decode()
            anchor = f"dash_mpd_debug.mpd?v={video_id}".encode()
            start = body.find(anchor)
    if start == -1:
        _dbg("anchor not found | video_id=%s anchor=%s", video_id, anchor.decode(errors="ignore"))
        return None
    remaining = body[start:]
    end_marker = f'"id":"{video_id}"'.encode()
    end_idx = remaining.find(end_marker)
    if end_idx > 0:
        section = remaining[:end_idx + len(end_marker)]
        _dbg("section found with end marker | video_id=%s start=%s len=%s", video_id, start, len(section))
        return section
    max_len = min(20000, len(remaining))
    section = remaining[:max_len]
    _dbg("section fallback window | video_id=%s start=%s len=%s", video_id, start, len(section))
    return section

def _parse_video_from_body(body: bytes, video_id: str) -> dict:
    data = {"hd_url": "", "sd_url": "", "title": _extract_title_from_html(body, "Facebook Video")}
    _dbg("parse body start | video_id=%s body_len=%s", video_id, len(body))
    section = _find_video_section(body, video_id)
    if section is None:
        section = body
        _dbg("section nil, fallback to full body | video_id=%s", video_id)
    section_text = section.decode("utf-8", errors="ignore")
    hd_match = HD_URL_PATTERN.search(section_text)
    if hd_match:
        data["hd_url"] = _unescape_facebook_url(hd_match.group(1))
        _dbg("hd progressive match found | url=%s", _clip(data["hd_url"], 200))
    else:
        _dbg("hd progressive match not found")
    sd_match = SD_URL_PATTERN.search(section_text)
    if sd_match:
        data["sd_url"] = _unescape_facebook_url(sd_match.group(1))
        _dbg("sd progressive match found | url=%s", _clip(data["sd_url"], 200))
    else:
        _dbg("sd progressive match not found")
    if not data["hd_url"]:
        hd_native_match = BROWSER_NATIVE_HD_URL_PATTERN.search(section_text)
        if hd_native_match:
            data["hd_url"] = _unescape_facebook_url(hd_native_match.group(1))
            _dbg("hd native match found | url=%s", _clip(data["hd_url"], 200))
        else:
            _dbg("hd native match not found")
    if not data["sd_url"]:
        sd_native_match = BROWSER_NATIVE_SD_URL_PATTERN.search(section_text)
        if sd_native_match:
            data["sd_url"] = _unescape_facebook_url(sd_native_match.group(1))
            _dbg("sd native match found | url=%s", _clip(data["sd_url"], 200))
        else:
            _dbg("sd native match not found")
    _dbg("parse body done | video_id=%s hd=%s sd=%s title=%s", video_id, bool(data["hd_url"]), bool(data["sd_url"]), _clip(data["title"], 160))
    if not data["hd_url"] and not data["sd_url"]:
        preview = _clip(section_text, 1500)
        _dbg("no video urls found | section_preview=%s", preview)
        _write_debug_file("facebook_body", body)
        _write_debug_file("facebook_section", section)
        raise RuntimeError("no video URLs found in page")
    return data

async def _safe_edit_status(bot, chat_id, status_msg_id, text: str):
    await edit_status(bot, chat_id, status_msg_id, text, label="Facebook")

async def _probe_total_bytes(session, url: str, headers: dict | None = None) -> int:
    total = 0
    try:
        async with session.head(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
    except Exception:
        total = 0
    if total > 0:
        return total
    try:
        h = dict(headers or {})
        h["Range"] = "bytes=0-0"
        async with session.get(url, headers=h, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
            m = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 0

async def _aria2c_download_with_progress(session, media_url: str, out_path: str, bot, chat_id, status_msg_id, title_text: str, headers: dict | None = None, known_total: int = 0):
    aria2 = resolve_aria2()
    if not aria2:
        raise RuntimeError("aria2c not found in PATH")
    total = known_total or await _probe_total_bytes(session, media_url, headers=headers)
    if total:
        check_media_size_limit(total, "Facebook video")
    out_dir, out_name = os.path.dirname(out_path) or ".", os.path.basename(out_path)
    cmd = [aria2, "--dir", out_dir, "--out", out_name, "--file-allocation=none", "--allow-overwrite=true", "--auto-file-renaming=false", "--continue=true", "--max-connection-per-server=16", "--split=16", "--min-split-size=1M", "--summary-interval=0", "--download-result=hide", "--console-log-level=warn"]
    for k, v in (headers or {}).items():
        if v:
            cmd.extend(["--header", f"{k}: {v}"])
    cmd.append(media_url)
    _dbg("aria2c start | out=%s url=%s", out_path, _clip(media_url, 200))
    proc = await spawn_process(*cmd, stdout=DEVNULL, stderr=PIPE)
    stats = TransferStats(total)
    progress_bot = None if (total and total < FACEBOOK_PROGRESS_MIN_BYTES) else bot
    while proc.returncode is None:
        await asyncio.sleep(FACEBOOK_ARIA2_POLL)
        if not os.path.exists(out_path):
            continue
        try:
            downloaded = os.path.getsize(out_path)
        except Exception:
            continue
        if downloaded <= 0:
            continue
        if downloaded > MAX_TG_SIZE:
            try:
                proc.kill()
            except Exception:
                pass
            raise FileSizeLimitExceeded("Facebook video exceeds 2GB limit. Download canceled.")
        await stats.update(downloaded, progress_bot, chat_id, status_msg_id, title_text, kind="Facebook download", label=os.path.basename(out_path), log_interval=2.5, edit_interval=5.0)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore").strip() if stderr else ""
        _dbg("aria2c failed | code=%s err=%s", proc.returncode, _clip(err, 500))
        raise RuntimeError(err or f"aria2c exited with code {proc.returncode}")
    stats.log_done("Facebook download", label=os.path.basename(out_path), size=os.path.getsize(out_path))
    _dbg("aria2c success | out=%s", out_path)

async def _aiohttp_download_with_progress(session, media_url: str, out_path: str, bot, chat_id, status_msg_id, title_text: str, headers: dict | None = None):
    _dbg("aiohttp download start | out=%s url=%s", out_path, _clip(media_url, 200))
    async with session.get(media_url, headers=headers, timeout=aiohttp.ClientTimeout(total=600), allow_redirects=True) as r:
        _dbg("aiohttp download response | status=%s final=%s", r.status, str(r.url))
        if r.status >= 400:
            raise RuntimeError(f"Download failed: HTTP {r.status}")
        total = int(r.headers.get("Content-Length", 0) or 0)
        if total:
            check_media_size_limit(total, "Facebook video")
        downloaded = 0
        stats = TransferStats(total)
        progress_bot = None if (total and total < FACEBOOK_PROGRESS_MIN_BYTES) else bot
        async with aiofiles.open(out_path, "wb") as f:
            async for chunk in r.content.iter_chunked(FACEBOOK_CHUNK_SIZE):
                if not chunk:
                    continue
                await f.write(chunk)
                downloaded += len(chunk)
                if downloaded > MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("Facebook video exceeds 2GB limit. Download canceled.")
                await stats.update(downloaded, progress_bot, chat_id, status_msg_id, title_text, kind="Facebook download", label=os.path.basename(out_path), log_interval=2.5, edit_interval=5.0)
    stats.log_done("Facebook download", label=os.path.basename(out_path), size=downloaded)
    _dbg("aiohttp download success | out=%s", out_path)

async def _download_with_best_engine(session, media_url: str, out_path: str, bot, chat_id, status_msg_id, title_text: str, headers: dict | None = None):
    engine = FACEBOOK_DOWNLOAD_ENGINE
    total = 0 if engine in ("aria2c", "aiohttp") else await _probe_total_bytes(session, media_url, headers=headers)
    prefer_http = engine == "aiohttp" or (engine == "auto" and total and total <= FACEBOOK_ARIA2_THRESHOLD)
    if prefer_http:
        try:
            _dbg("engine aiohttp | size=%s threshold=%s mode=%s", total, FACEBOOK_ARIA2_THRESHOLD, engine)
            await _aiohttp_download_with_progress(session, media_url, out_path, bot, chat_id, status_msg_id, title_text, headers=headers)
            return
        except FileSizeLimitExceeded:
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except Exception:
                    pass
            raise
        except Exception as e:
            log.warning("Facebook aiohttp failed, fallback aria2c | err=%r", e)
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except Exception:
                    pass
    try:
        await _aria2c_download_with_progress(session, media_url, out_path, bot, chat_id, status_msg_id, title_text, headers=headers, known_total=total)
    except FileSizeLimitExceeded:
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise
    except Exception as e:
        log.warning("Facebook aria2c failed, fallback aiohttp | err=%r", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        await _aiohttp_download_with_progress(session, media_url, out_path, bot, chat_id, status_msg_id, title_text, headers=headers)
def _video_urls_complete(buf: bytearray, content_id: str) -> bool:
    if len(buf) < _EARLY_EXIT_MIN_BYTES:
        return False
    hd = buf.rfind(b'"browser_native_hd_url"')
    sd = buf.rfind(b'"browser_native_sd_url"')
    if hd < 0 or sd < 0:
        return False
    if len(buf) - max(hd, sd) < 4096:
        return False
    if content_id and content_id.encode() not in buf:
        return False
    if b'"all_subattachments"' in buf:
        return False
    return True


async def _fetch_page_html(content_url: str, content_id: str, use_cookies: bool = False, early_exit_video: bool = False) -> tuple[int, str, bytes]:
    content_url = _normalize_content_url(content_url, content_id)
    session = await get_http_session()
    headers = _build_headers(content_url, use_cookies=use_cookies)
    _dbg("fetch page start | url=%s content_id=%s cookie=%s early=%s", content_url, content_id, bool(headers.get("Cookie")), early_exit_video)
    async with session.get(content_url, headers=headers, timeout=aiohttp.ClientTimeout(total=25), allow_redirects=True) as resp:
        final_url = str(resp.url)
        status = resp.status
        if not early_exit_video:
            body = await resp.read()
            _dbg("fetch page response | status=%s final=%s body_len=%s", status, final_url, len(body))
            return status, final_url, body
        cid = content_id or _extract_content_id(final_url)
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(64 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if not _video_urls_complete(buf, cid):
                continue
            try:
                probe = _parse_video_from_body(bytes(buf), cid)
            except Exception:
                continue
            if probe.get("hd_url") or probe.get("sd_url"):
                _dbg("fetch page early exit | status=%s final=%s body_len=%s", status, final_url, len(buf))
                return status, final_url, bytes(buf)
        _dbg("fetch page response | status=%s final=%s body_len=%s", status, final_url, len(buf))
        return status, final_url, bytes(buf)

def _shrink_thumb_sync(path: str, max_side: int, max_bytes: int) -> str | None:
    try:
        from PIL import Image
    except Exception:
        return path if os.path.exists(path) and os.path.getsize(path) <= max_bytes else None
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = os.path.splitext(path)[0] + "_s.jpg"
            im.save(out, format="JPEG", quality=85, optimize=True)
        if os.path.getsize(out) <= max_bytes:
            try:
                os.remove(path)
            except OSError:
                pass
            return out
        try:
            os.remove(out)
        except OSError:
            pass
    except Exception as e:
        _dbg("facebook thumbnail resize failed | path=%s err=%r", path, e)
    return path if os.path.exists(path) and os.path.getsize(path) <= max_bytes else None


async def _fetch_fb_thumb(url: str) -> str | None:
    if not url:
        return None
    os.makedirs(TMP_DIR, exist_ok=True)
    out_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_thumb.jpg")
    try:
        session = await get_http_session()
        async with session.get(url, headers={"User-Agent": WEB_HEADERS["User-Agent"]}, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True) as resp:
            if resp.status >= 400:
                return None
            raw = await resp.read()
        if not raw or len(raw) < 512:
            return None
        async with aiofiles.open(out_path, "wb") as fh:
            await fh.write(raw)
        shrunk = await asyncio.to_thread(_shrink_thumb_sync, out_path, FACEBOOK_THUMB_WIDTH, 200 * 1024)
        if not shrunk and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
        return shrunk
    except Exception as e:
        _dbg("facebook thumb fetch failed | err=%r", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
        return None


async def _download_fb_photo(session, media_url: str, out_path: str, bot, chat_id, status_msg_id, title_text: str) -> str:
    _dbg("photo download start | out=%s url=%s", out_path, _clip(media_url, 200))
    headers = dict(WEB_HEADERS)
    upgraded = _strip_cover_crop(media_url)
    candidates = [upgraded, media_url] if upgraded and upgraded != media_url else [media_url]
    last_error = None
    for attempt, cand in enumerate(candidates):
        try:
            async with session.get(cand, headers=headers, timeout=aiohttp.ClientTimeout(total=600), allow_redirects=True) as r:
                _dbg("photo download response | status=%s final=%s", r.status, str(r.url))
                if r.status >= 400:
                    raise RuntimeError(f"Facebook photo download failed: HTTP {r.status}")
                total = int(r.headers.get("Content-Length", 0) or 0)
                if total:
                    check_media_size_limit(total, "Facebook photo")
                downloaded = 0
                stats = TransferStats(total)
                progress_bot = None if (total and total < FACEBOOK_PROGRESS_MIN_BYTES) else bot
                async with aiofiles.open(out_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(FACEBOOK_CHUNK_SIZE):
                        if not chunk:
                            continue
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > MAX_TG_SIZE:
                            raise FileSizeLimitExceeded("Facebook photo exceeds 2GB limit. Download canceled.")
                        await stats.update(downloaded, progress_bot, chat_id, status_msg_id, title_text, kind="Facebook download", label=os.path.basename(out_path), log_interval=2.5, edit_interval=5.0)
            if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
                raise RuntimeError("Facebook photo download output empty")
            stats.log_done("Facebook download", label=os.path.basename(out_path), size=downloaded)
            final_path = _fix_image_ext(out_path)
            _dbg("photo download success | out=%s", final_path)
            return final_path
        except FileSizeLimitExceeded:
            raise
        except Exception as e:
            last_error = e
            _dbg("photo download attempt failed | attempt=%s err=%r", attempt, e)
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except Exception:
                    pass
            if attempt + 1 >= len(candidates):
                break
    raise last_error or RuntimeError("Facebook photo download failed")


async def _download_fb_photos_parallel(session, urls: list[str], bot, chat_id, status_msg_id) -> list[str]:
    total = len(urls)
    if status_msg_id and total > 1:
        await _safe_edit_status(bot, chat_id, status_msg_id, "<b>Downloading Facebook photos...</b>")
    sem = asyncio.Semaphore(FACEBOOK_PHOTO_CONCURRENCY)

    async def _one(idx: int, url: str) -> str:
        async with sem:
            title_text = f"Downloading Facebook photo {idx}/{total}..." if total > 1 else "Downloading Facebook photo..."
            out_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.jpg")
            return await _download_fb_photo(session, url, out_path, bot, chat_id, status_msg_id, title_text)

    results = await asyncio.gather(*(_one(idx, url) for idx, url in enumerate(urls, 1)), return_exceptions=True)
    err = next((r for r in results if isinstance(r, Exception)), None)
    downloaded = [r for r in results if isinstance(r, str) and r]
    if err is not None:
        for p in downloaded:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        raise err
    return downloaded

async def _get_video_data(content_url: str, content_id: str) -> dict:
    content_url = _normalize_content_url(content_url, content_id)
    session = await get_http_session()
    headers = _build_headers(content_url)
    _dbg("fetch video data start | url=%s content_id=%s cookie=%s", content_url, content_id, bool(headers.get("Cookie")))
    async with session.get(content_url, headers=headers, timeout=aiohttp.ClientTimeout(total=25), allow_redirects=True) as resp:
        final_url = str(resp.url)
        status = resp.status
        body = await resp.read()
        _dbg("fetch video data response | status=%s final=%s body_len=%s", status, final_url, len(body))
        if status != 200:
            _write_debug_file("facebook_http_error", body)
            raise RuntimeError(f"failed to get page: HTTP {status}")
    parsed = _parse_video_from_body(body, content_id)
    _dbg("after _parse_video_from_body | parsed=%r", parsed)
    return parsed

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
GRAPHQL_VIDEO_DOC_ID = "28300151029658894"
GRAPHQL_VIDEO_OP = "CometVideoHomeNewPermalinkHeroUnitQuery"
GRAPHQL_VIDEO_PROVIDED = {
    "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": False,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": False,
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": False,
    "__relay_internal__pv__TestPilotShouldIncludeDemoAdUseCaserelayprovider": False,
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "ORIGINAL",
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": True,
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": True,
}
LSD_RE = re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"')
JAZOEST_RE = re.compile(r'"jazoest","value":"(\d+)"')

def _graphql_variables(video_id: str) -> dict:
    variables = {
        "caller": "TAHOE",
        "entityNumber": 5,
        "feedbackSource": 41,
        "feedLocation": "TAHOE",
        "focusCommentID": None,
        "isCrawler": False,
        "isLoggedOut": True,
        "privacySelectorRenderLocation": "COMET_STREAM",
        "renderLocation": "video_home",
        "scale": 1,
        "useDefaultActor": False,
        "videoID": video_id,
        "videoIDStr": str(video_id),
    }
    variables.update(GRAPHQL_VIDEO_PROVIDED)
    return variables

def _iter_ndjson(text: str):
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue

def _graphql_extract_video(text: str) -> dict:
    out = {"hd_url": "", "sd_url": "", "title": ""}
    for node in _iter_ndjson(text):
        video = ((node.get("data") or {}).get("video")) or {}
        story = video.get("story") or {}
        for attachment in story.get("attachments") or []:
            media = attachment.get("media") or {}
            delivery = media.get("videoDeliveryLegacyFields") or {}
            hd = delivery.get("browser_native_hd_url") or ""
            sd = delivery.get("browser_native_sd_url") or ""
            if hd and not out["hd_url"]:
                out["hd_url"] = hd
            if sd and not out["sd_url"]:
                out["sd_url"] = sd
            title = media.get("title") or {}
            if isinstance(title, dict) and title.get("text") and not out["title"]:
                out["title"] = title["text"]
        if not out["title"]:
            node_title = video.get("name") or (story.get("title") or {}).get("text")
            if node_title:
                out["title"] = node_title
        if out["hd_url"] or out["sd_url"]:
            break
    return out

async def _graphql_video_data(video_id: str, page_text: str = "") -> dict:
    if not video_id:
        return {"hd_url": "", "sd_url": "", "title": ""}
    session = await get_http_session()
    watch_url = f"https://www.facebook.com/watch/?v={video_id}"
    headers = _build_headers(watch_url, use_cookies=True)
    lsd_match = LSD_RE.search(page_text) if page_text else None
    jz_match = JAZOEST_RE.search(page_text) if page_text else None
    if not lsd_match:
        try:
            async with session.get(watch_url, headers=headers, timeout=aiohttp.ClientTimeout(total=25), allow_redirects=True) as resp:
                page = await resp.text()
        except Exception as e:
            _dbg("graphql bootstrap failed | video_id=%s err=%r", video_id, e)
            return {"hd_url": "", "sd_url": "", "title": ""}
        lsd_match = LSD_RE.search(page)
        jz_match = JAZOEST_RE.search(page)
    if not lsd_match:
        _dbg("graphql bootstrap missing lsd | video_id=%s", video_id)
        return {"hd_url": "", "sd_url": "", "title": ""}
    lsd = lsd_match.group(1)
    payload = {
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": GRAPHQL_VIDEO_OP,
        "variables": json.dumps(_graphql_variables(video_id)),
        "doc_id": GRAPHQL_VIDEO_DOC_ID,
        "server_timestamps": "true",
        "lsd": lsd,
    }
    if jz_match:
        payload["jazoest"] = jz_match.group(1)
    req_headers = dict(headers)
    req_headers.update({
        "X-FB-Friendly-Name": GRAPHQL_VIDEO_OP,
        "X-FB-LSD": lsd,
        "Origin": "https://www.facebook.com",
        "Referer": watch_url,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        async with session.post(GRAPHQL_URL, data=payload, headers=req_headers, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as resp:
            text = await resp.text()
            status = resp.status
    except Exception as e:
        _dbg("graphql video request failed | video_id=%s err=%r", video_id, e)
        return {"hd_url": "", "sd_url": "", "title": ""}
    parsed = _graphql_extract_video(text)
    _dbg("graphql video result | video_id=%s status=%s hd=%s sd=%s", video_id, status, bool(parsed["hd_url"]), bool(parsed["sd_url"]))
    return parsed

def _alternate_video_urls(content_url: str, content_id: str) -> list[str]:
    urls: list[str] = []
    if not content_id:
        return urls
    if "/reel/" in content_url:
        urls.append(f"https://www.facebook.com/watch/?v={content_id}")
    if "/watch" in content_url:
        urls.append(f"https://www.facebook.com/reel/{content_id}")
    return urls

async def facebook_scrape_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    if not metadata_ready:
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Scraping Facebook post...</b>")
    _dbg("facebook init | BASE_DIR=%s COOKIES_PATH=%s exists=%s",BASE_DIR,COOKIES_PATH,os.path.exists(COOKIES_PATH))
    _dbg("facebook scrape start | raw_url=%s fmt_key=%s",raw_url,fmt_key)
    content_url=(raw_url or "").strip()
    is_share=bool(SHARE_RE.search(content_url))
    _dbg("share url detected=%s | url=%s",is_share,content_url)
    if is_share:
        content_url=await _follow_share_redirect(content_url)
    content_url=_resolve_canonical_fb_url(content_url)
    content_id=_extract_content_id(content_url)
    if not content_id:
        _dbg("no content_id extracted from url, proceeding to fetch page directly | url=%s", content_url)
    _dbg("before _fetch_page_html (cookieless) | content_url=%s content_id=%s",content_url,content_id)
    video_only_page=bool(re.search(r"/(?:reel|watch|videos?)/", content_url)) or "?v=" in content_url
    status,final_url,body=await _fetch_page_html(content_url,content_id,use_cookies=False,early_exit_video=video_only_page)
    if status!=200 or not body or "/login" in str(final_url):
        _dbg("cookieless fetch failed | status=%s body_len=%s login=%s, retry with cookies",status,len(body or b""),"/login" in str(final_url))
        status,final_url,body=await _fetch_page_html(content_url,content_id,use_cookies=True,early_exit_video=video_only_page)
    if status!=200 or not body:
        _write_debug_file("facebook_http_error", body or b"")
        raise RuntimeError(f"failed to get page: HTTP {status}")
    body_text=body.decode("utf-8",errors="ignore")
    if not VIDEO_SIGNAL_RE.search(body_text):
        for alt_url in _alternate_video_urls(content_url, content_id):
            _dbg("no video signal, trying alternate url | alt=%s", alt_url)
            alt_status, alt_final, alt_body = await _fetch_page_html(alt_url, content_id, use_cookies=True)
            if alt_status != 200 or not alt_body:
                continue
            alt_text = alt_body.decode("utf-8", errors="ignore")
            if VIDEO_SIGNAL_RE.search(alt_text):
                _dbg("video signal found on alternate url | alt=%s", alt_url)
                content_url, body, body_text = alt_url, alt_body, alt_text
                break
    if not VIDEO_SIGNAL_RE.search(body_text):
        attachments = _extract_attachment_photos(body_text)
        if attachments and not video_only_page and '"__typename":"Video"' not in body_text:
            _dbg("photo attachments found, skipping graphql fallback | count=%s", len(attachments))
            return await _scrape_photo_post(body,body_text,bot,chat_id,status_msg_id,attachment_photos=attachments)
        graph = await _graphql_video_data(content_id, page_text=body_text)
        if graph.get("hd_url") or graph.get("sd_url"):
            video_data = {
                "hd_url": graph.get("hd_url") or "",
                "sd_url": graph.get("sd_url") or "",
                "title": graph.get("title") or "",
            }
            _dbg("video from graphql fallback | hd=%s sd=%s", bool(video_data["hd_url"]), bool(video_data["sd_url"]))
        else:
            return await _scrape_photo_post(body,body_text,bot,chat_id,status_msg_id,attachment_photos=attachments)
    else:
        _dbg("video signal found, parse video from fetched body")
        video_data=None
        try:
            video_data=_parse_video_from_body(body,content_id)
        except RuntimeError as e:
            _dbg("parse video from cookieless body failed | err=%r",e)
        if not video_data or not (video_data.get("hd_url") or video_data.get("sd_url")):
            _dbg("no video urls in cookieless body, retry with cookies")
            video_data=await _get_video_data(content_url,content_id)
        if not video_data or not (video_data.get("hd_url") or video_data.get("sd_url")):
            _dbg("no video urls after cookie retry, trying graphql")
            graph = await _graphql_video_data(content_id, page_text=body_text)
            if graph.get("hd_url") or graph.get("sd_url"):
                video_data = {
                    "hd_url": graph.get("hd_url") or "",
                    "sd_url": graph.get("sd_url") or "",
                    "title": graph.get("title") or (video_data or {}).get("title") or "",
                }
    _dbg("video data parsed | hd=%s sd=%s",bool(video_data.get("hd_url")),bool(video_data.get("sd_url")))
    video_url=video_data.get("hd_url") or video_data.get("sd_url")
    _dbg("video url selected | exists=%s",bool(video_url))
    if not video_url:
        _dbg("video url empty after parse | content_url=%s content_id=%s",content_url,content_id)
        raise RuntimeError("This Facebook video is private, deleted, or requires login. No playable source was found.")
    duration = _extract_video_duration_from_url(video_url)
    _dbg("video duration check | duration_s=%s", duration)
    if duration == 0:
        _dbg("video duration invalid or zero, treating as photo post | duration=%s", duration)
        mixed_media = _extract_mixed_media(body_text)
        has_photos = bool(mixed_media.get("photos"))
        og_image = _extract_og_image(body_text)
        all_photos = _collect_photo_urls(body_text)
        _dbg("video duration check fallback | photos=%s og_image=%s all_photos=%s", len(mixed_media.get("photos", [])), bool(og_image), len(all_photos))
        if has_photos or all_photos:
            return await _scrape_photo_post(body, body_text, bot, chat_id, status_msg_id)
        raise RuntimeError("video has invalid duration and no photo found in post")
    
    mixed_media = _extract_mixed_media(body_text)
    has_photos = bool(mixed_media.get("photos"))
    _dbg("mixed media check | videos=%s photos=%s", len(mixed_media.get("videos", [])), len(mixed_media.get("photos", [])))
    title=(video_data.get("title") or "Facebook Video").strip() or "Facebook Video"
    _dbg("fb title selected | title=%s",_clip(title,200))
    os.makedirs(TMP_DIR,exist_ok=True)
    out_path=f"{TMP_DIR}/{uuid.uuid4().hex}.mp4"
    session=await get_http_session()
    headers=_build_headers(_normalize_content_url(content_url,content_id))
    _dbg("download chosen url | url=%s out=%s",_clip(video_url,200),out_path)
    thumb_task=asyncio.create_task(_fetch_fb_thumb(_extract_og_image(body_text)))
    await _download_with_best_engine(session,video_url,out_path,bot,chat_id,status_msg_id,"Downloading Facebook video...",headers=headers)
    if not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
        thumb_task.cancel()
        raise RuntimeError("Facebook download output empty")
    _dbg("facebook scrape download video done | out=%s",out_path)
    try:
        thumb_path=await thumb_task
    except Exception as e:
        _dbg("facebook cover thumb failed | err=%r",e)
        thumb_path=None

    if has_photos:
        _dbg("mixed content: downloading photos | count=%s", len(mixed_media["photos"]))
        photo_urls=[p["url"] for p in mixed_media["photos"] if p.get("url")]
        try:
            photo_paths=await _download_fb_photos_parallel(session,photo_urls,bot,chat_id,status_msg_id)
        except Exception as e:
            _dbg("mixed content photo download failed | err=%r", e)
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except Exception:
                pass
            raise
        _dbg("mixed content download done | video=%s photos=%s", out_path, len(photo_paths))
        items = [{"path": out_path, "type": "video", "thumb": thumb_path}]
        items.extend([{"path": p, "type": "photo"} for p in photo_paths])
        return {"items": items, "title": title, "source": "Facebook"}

    result={"path":out_path,"title":title,"source":"Facebook"}
    if thumb_path:
        result["thumb"]=thumb_path
    return result

async def _scrape_photo_post(body:bytes,body_text:str,bot,chat_id,status_msg_id,attachment_photos:list[dict]|None=None):
    attachment_photos=attachment_photos if attachment_photos is not None else _extract_attachment_photos(body_text)
    photo_urls=_collect_photo_urls(body_text,attachment_photos)
    caption=_extract_post_caption(body_text)
    fallback_title=_extract_title_from_html(body,"Facebook Photo")
    if not caption:
        caption=fallback_title
    caption_is_real=bool(caption) and not _is_bad_fb_title(caption) and caption.strip().lower() not in {
        "facebook photo","facebook video","facebook","login","log in","facebook login","watch","reel"
    }
    _dbg("photo post parsed | photos=%s caption_len=%s caption_real=%s",len(photo_urls),len(caption or ""),caption_is_real)
    if not photo_urls:
        if caption_is_real and bot is not None and chat_id is not None:
            safe_caption=(caption or "").strip()
            if len(safe_caption)>3500:
                safe_caption=safe_caption[:3497].rstrip()+"..."
            try:
                from html import escape as _esc
                bot_name=""
                try:
                    me=await bot.get_me()
                    bot_name=getattr(me,"first_name",None) or ""
                except Exception:
                    bot_name=""
                suffix=f"\n\n🪄 <i>Powered by {_esc(bot_name or 'Bot')}</i>"
                await bot.send_message(chat_id=chat_id,text=f"<blockquote expandable>📝 {_esc(safe_caption)}</blockquote>{suffix}",parse_mode="HTML",disable_web_page_preview=True)
                if status_msg_id:
                    try:
                        await bot.delete_message(chat_id=chat_id,message_id=status_msg_id)
                    except Exception:
                        pass
                return {"handled":True}
            except Exception as e:
                log.warning("Facebook text post send failed | err=%r",e)
        raise RuntimeError("This Facebook post is private, deleted, or requires login. No media could be extracted.")
    os.makedirs(TMP_DIR,exist_ok=True)
    session=await get_http_session()
    downloaded=await _download_fb_photos_parallel(session,photo_urls,bot,chat_id,status_msg_id)
    _dbg("photo post download done | ok=%s/%s",len(downloaded),len(photo_urls))
    if len(downloaded)==1:
        return {"path":downloaded[0],"title":caption or "Facebook Photo","source":"Facebook"}
    return {"items":[{"path":p,"type":"photo"} for p in downloaded],"title":caption or "Facebook Photo","source":"Facebook"}

async def facebook_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    return await facebook_scrape_download(
        raw_url=raw_url,
        fmt_key=fmt_key,
        bot=bot,
        chat_id=chat_id,
        status_msg_id=status_msg_id,
        format_id=format_id,
        has_audio=has_audio,
        metadata_ready=metadata_ready,
    )