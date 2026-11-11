import os
import re
import ast
import html as html_lib
import logging
import asyncio
from urllib.parse import urlparse
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import FileSizeLimitExceeded
from handlers.dl.progress import TransferStats
from .constants import (
    UA,
    HTTP_TIMEOUT,
    SPANKBANG_REFERER,
    SPANKBANG_QUALITIES,
    SPANKBANG_PROGRESS_INTERVAL,
    SPANKBANG_BROWSER_TIMEOUT_MS,
)

log = logging.getLogger(__name__)

_ID_PATH_RE = re.compile(
    r"^/([a-z0-9]{4,10})(?:/video(?:/([^/?#]*))?|/embed(?:/([^/?#]*))?)?/?$",
    re.I,
)

_STREAM_DATA_RE = re.compile(r"var\s+stream_data\s*=\s*(\{.*?\})\s*;", re.S)
_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_OG_IMG_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_OG_DUR_RE = re.compile(r'<meta[^>]+property=["\']og:video:duration["\'][^>]+content=["\']([^"\']*)["\']', re.I)

_TITLE_SUFFIX_RE = re.compile(r"\s*[-|:]\s*(?:SpankBang|Watch\s+on\s+SpankBang).*$", re.I)

_BROWSER_UA: str = ""


def canonical_video_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    path = (parsed.path or "").strip()
    m = _ID_PATH_RE.match(path)
    if not m:
        return (url or "").strip()
    vid = m.group(1)
    slug = m.group(2) or m.group(3) or ""
    slug_part = f"/{slug}" if slug else ""
    return f"https://spankbang.com/{vid}/video{slug_part}"


def _clean_title(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", "", text)
    clean = html_lib.unescape(clean).strip()
    clean = _TITLE_SUFFIX_RE.sub("", clean).strip()
    return clean


def _parse_stream_data(html_text: str) -> dict | None:
    if not html_text:
        return None
    m = _STREAM_DATA_RE.search(html_text)
    if not m:
        return None
    raw = m.group(1)
    try:
        data = ast.literal_eval(raw)
        if isinstance(data, dict):
            return data
    except Exception as e:
        log.debug("SpankBang stream_data literal_eval failed | err=%r", e)
    return None


def _fetch_page_curl(url: str, cookies: dict | None, ua: str | None) -> tuple[int, str]:
    headers = {"User-Agent": ua or UA, "Referer": SPANKBANG_REFERER}
    sess = curl_requests.Session(impersonate="chrome")
    if cookies:
        for k, v in cookies.items():
            sess.cookies.set(k, v, domain=".spankbang.com")
    try:
        r = sess.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
        return r.status_code, r.text or ""
    except Exception as e:
        log.debug("SpankBang curl fetch failed | url=%s err=%r", url, e)
        return 0, ""


def _fetch_page_browser(url: str) -> str:
    global _BROWSER_UA
    import utils.scrapling_browser as sb

    ua_box = {}

    def _page_action(page):
        try:
            ua_box["ua"] = page.evaluate("() => navigator.userAgent")
        except Exception:
            pass

    html_text = sb.fetch_html(
        url,
        solve_cloudflare=True,
        page_action=_page_action,
        timeout_ms=SPANKBANG_BROWSER_TIMEOUT_MS,
    )
    if ua_box.get("ua"):
        _BROWSER_UA = str(ua_box["ua"]).strip()
    return html_text or ""


def _load_html(url: str) -> str:
    import utils.scrapling_browser as sb

    canon = canonical_video_url(url)
    ck = sb.cookies_for("spankbang.com")
    if ck and ck.get("cf_clearance"):
        ua = _BROWSER_UA or UA
        code, text = _fetch_page_curl(canon, ck, ua)
        if code == 200 and "stream_data" in text:
            return text
        log.info("SpankBang cached cf_clearance expired or failed | status=%s", code)

    log.info("SpankBang resolving page via stealth browser | url=%s", canon)
    html_text = _fetch_page_browser(canon)
    if "stream_data" in html_text:
        return html_text

    raise RuntimeError("Failed to resolve SpankBang video stream (Cloudflare challenge failed)")


def _probe_format_size(url: str) -> int:
    try:
        resp = curl_requests.get(
            url,
            headers={"User-Agent": UA, "Referer": SPANKBANG_REFERER, "Range": "bytes=0-0"},
            timeout=10,
        )
        cr = resp.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            resp.close()
            return int(m.group(1))
        if resp.status_code in (200, 206):
            cl = int(resp.headers.get("Content-Length") or 0)
            resp.close()
            return cl
        resp.close()
    except Exception as e:
        log.debug("SpankBang size probe failed | url=%s err=%r", url, e)
    return 0


def scrape_post(raw_url: str) -> dict:
    url = canonical_video_url(raw_url)
    html_text = _load_html(url)
    sd = _parse_stream_data(html_text)
    if not sd:
        raise RuntimeError("Failed to extract stream_data from SpankBang page.")

    title = ""
    m_h1 = _TITLE_RE.search(html_text)
    if m_h1:
        title = _clean_title(m_h1.group(1))
    if not title:
        m_og = _OG_TITLE_RE.search(html_text)
        if m_og:
            title = _clean_title(m_og.group(1))
    if not title:
        title = "SpankBang Video"

    duration = int(sd.get("length") or 0)
    if not duration:
        m_dur = _OG_DUR_RE.search(html_text)
        if m_dur:
            try:
                duration = int(m_dur.group(1).strip())
            except Exception:
                duration = 0

    thumbnail = sd.get("cover_image") or sd.get("thumbnail") or ""
    if not thumbnail:
        m_img = _OG_IMG_RE.search(html_text)
        if m_img:
            thumbnail = m_img.group(1).strip()

    variants = []
    res_list = []

    for fid, height in SPANKBANG_QUALITIES:
        urls = sd.get(fid)
        if not urls or not isinstance(urls, list):
            continue
        first_url = str(urls[0] or "").strip()
        if not first_url.startswith("http"):
            continue

        filesize = _probe_format_size(first_url)
        var = {
            "format_id": fid,
            "height": height,
            "label": fid,
            "url": first_url,
            "filesize": filesize,
            "total_size": filesize,
            "has_audio": True,
        }
        variants.append(var)
        res_list.append({
            "height": height,
            "format_id": fid,
            "label": fid,
            "filesize": filesize,
            "total_size": filesize,
            "has_audio": True,
        })

    if not variants:
        m_main = sd.get("main")
        if m_main and isinstance(m_main, list) and m_main[0]:
            first_url = str(m_main[0]).strip()
            filesize = _probe_format_size(first_url)
            var = {
                "format_id": "720p",
                "height": 720,
                "label": "720p",
                "url": first_url,
                "filesize": filesize,
                "total_size": filesize,
                "has_audio": True,
            }
            variants.append(var)
            res_list.append({
                "height": 720,
                "format_id": "720p",
                "label": "720p",
                "filesize": filesize,
                "total_size": filesize,
                "has_audio": True,
            })

    if not variants:
        raise RuntimeError("No downloadable video stream found on SpankBang page.")

    variants.sort(key=lambda x: int(x.get("height") or 0), reverse=True)
    res_list.sort(key=lambda x: int(x.get("height") or 0), reverse=True)

    return {
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "variants": variants,
        "res_list": res_list,
        "canonical_url": url,
    }


async def download_video(
    url: str,
    format_id: str,
    out_path: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    notify: bool = True,
    variants: list | None = None,
) -> dict:
    all_vars = variants or (await asyncio.to_thread(scrape_post, url)).get("variants") or []
    chosen = (format_id or "").strip().lower()
    target = next((v for v in all_vars if v["format_id"] == chosen), None)
    if not target and all_vars:
        target = all_vars[0]
    if not target:
        raise RuntimeError("Selected video resolution not found.")

    dl_url = target["url"]
    headers = {"User-Agent": UA, "Referer": SPANKBANG_REFERER}
    total_hint = int(target.get("filesize") or 0)
    if total_hint > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total_hint / 1024 ** 3:.2f} GB). Download canceled."
        )

    try:
        from handlers.dl.aria2 import download_aria2
        ok = await download_aria2(
            dl_url,
            out_path,
            headers=headers,
            total_size=total_hint,
            conns=16,
            kind="SpankBang download",
            label=f"SpankBang {target.get('label')}",
            title=title,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            notify=notify,
            edit_interval=SPANKBANG_PROGRESS_INTERVAL,
            timeout=HTTP_TIMEOUT * 12,
        )
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return {"path": out_path, "title": title}
    except FileSizeLimitExceeded:
        raise
    except Exception as e:
        log.warning("SpankBang aria2c failed, falling back to streaming | err=%r", e)

    def _open_stream():
        resp = curl_requests.get(
            dl_url,
            headers=headers,
            impersonate="chrome",
            stream=True,
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {resp.status_code} while downloading video.")
        return resp

    resp = await asyncio.to_thread(_open_stream)
    try:
        total = int(resp.headers.get("Content-Length") or 0)
    except Exception:
        total = total_hint

    if total > MAX_TG_SIZE:
        resp.close()
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total / 1024 ** 3:.2f} GB). Download canceled."
        )

    stats = TransferStats(total)

    def _write():
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=512 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                stats.sample(stats.downloaded + len(chunk))
                if stats.downloaded > MAX_TG_SIZE:
                    resp.close()
                    raise FileSizeLimitExceeded("File exceeds 2GB limit. Download canceled.")
        resp.close()

    if notify and status_msg_id:
        await stats.emit(
            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            title=title, kind="SpankBang download",
            label=f"SpankBang {target.get('label')}",
            log_interval=SPANKBANG_PROGRESS_INTERVAL,
            edit_interval=SPANKBANG_PROGRESS_INTERVAL,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                title=title, kind="SpankBang download",
                label=f"SpankBang {target.get('label')}",
                log_interval=SPANKBANG_PROGRESS_INTERVAL,
                edit_interval=SPANKBANG_PROGRESS_INTERVAL,
            )
        await asyncio.sleep(0.5)

    await write_task
    stats.log_done("SpankBang download", label=f"SpankBang {target.get('label')}")
    return {"path": out_path, "title": title}
