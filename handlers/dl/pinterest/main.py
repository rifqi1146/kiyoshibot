import os
import re
import json
import time
import uuid
import html
import shutil
import asyncio
import aiohttp
import aiofiles
import logging
import subprocess
from urllib.parse import urlparse, urlencode
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR
from handlers.dl.utils import sanitize_filename, is_invalid_video
from handlers.dl.ytdlp import ytdlp_download

try:
    from handlers.dl.constants import BASE_DIR
except Exception:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

DEBUG_PINTEREST = True
PIN_RESOURCE_ENDPOINT = "https://www.pinterest.com/resource/PinResource/get/"
SHORTENER_API = "https://api.pinterest.com/url_shortener/{}/redirect/"
COOKIES_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "..", "data", "cookies.txt"))
_COOKIE_HEADER_CACHE = None

PIN_RE = re.compile(r"https?://(?:[^/]+\.)?pinterest\.[^/]+/pin/(?:[\w-]+--)?(\d+)", re.I)
SHORT_RE = re.compile(r"https?://(?:www\.)?pin\.[^/]+/([A-Za-z0-9_-]+)", re.I)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pinterest.com/",
    "X-Pinterest-Pws-Handler": "www/[username].js",
}

def _dbg(msg: str, *args):
    if DEBUG_PINTEREST:
        log.warning("PINDbg | " + msg, *args)

def _clip(text: str, limit: int = 350) -> str:
    text = str(text or "").replace("\n", "\\n").replace("\r", "\\r")
    return text if len(text) <= limit else text[:limit] + "...<cut>"

def is_pinterest_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return False
    host = (urlparse(text).hostname or "").lower()
    return "pinterest." in host or host.startswith("pin.")

def _load_cookie_header(path: str) -> str:
    global _COOKIE_HEADER_CACHE
    if _COOKIE_HEADER_CACHE is not None:
        return _COOKIE_HEADER_CACHE
    if not path or not os.path.exists(path):
        _COOKIE_HEADER_CACHE = ""
        return _COOKIE_HEADER_CACHE
    pairs = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain = (parts[0] or "").strip().lower()
                    name = (parts[5] or "").strip()
                    value = (parts[6] or "").strip()
                    if name and "pinterest." in domain:
                        pairs.append(f"{name}={value}")
                    continue
                if "=" in line and "\t" not in line and not line.lower().startswith(("http://", "https://")):
                    name, value = line.split("=", 1)
                    name = name.strip()
                    value = value.strip()
                    if name:
                        pairs.append(f"{name}={value}")
        _COOKIE_HEADER_CACHE = "; ".join(pairs)
        _dbg("cookie loaded | path=%s pairs=%s", path, len(pairs))
        return _COOKIE_HEADER_CACHE
    except Exception as e:
        _dbg("cookie load failed | err=%r", e)
        _COOKIE_HEADER_CACHE = ""
        return _COOKIE_HEADER_CACHE

def _build_headers(referer: str | None = None, accept: str | None = None) -> dict:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    if accept:
        headers["Accept"] = accept
    cookie = _load_cookie_header(COOKIES_PATH)
    if cookie:
        headers["Cookie"] = cookie
    return headers

def _build_pin_request_params(pin_id: str) -> str:
    payload = {
        "options": {
            "field_set_key": "unauth_react_main_pin",
            "id": pin_id,
        }
    }
    return urlencode({"data": json.dumps(payload, separators=(",", ":"))})

def _extract_pin_id(url: str) -> str:
    m = PIN_RE.search(url or "")
    return (m.group(1) if m else "").strip()

async def _resolve_short_url(url: str) -> str:
    m = SHORT_RE.search(url or "")
    if not m:
        return url
    short_id = m.group(1).strip()
    api_url = SHORTENER_API.format(short_id)
    session = await get_http_session()
    headers = _build_headers("https://www.pinterest.com/")
    _dbg("short resolve start | url=%s api=%s", url, api_url)
    try:
        async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as resp:
            loc = resp.headers.get("Location") or resp.headers.get("location")
            _dbg("short api response | status=%s location=%s", resp.status, loc)
            if loc:
                return loc
    except Exception as e:
        _dbg("short api failed | err=%r", e)
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
        final = str(resp.url)
        _dbg("short normal resolve | status=%s final=%s", resp.status, final)
        return final

async def _get_pin_data(pin_id: str) -> dict:
    session = await get_http_session()
    req_url = PIN_RESOURCE_ENDPOINT + "?" + _build_pin_request_params(pin_id)
    headers = _build_headers("https://www.pinterest.com/")
    _dbg("pin api fetch | pin_id=%s url=%s", pin_id, req_url)
    async with session.get(req_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as resp:
        text = await resp.text()
        _dbg("pin api response | status=%s len=%s preview=%s", resp.status, len(text), _clip(text, 250))
        if resp.status != 200:
            raise RuntimeError(f"Pinterest API bad response: HTTP {resp.status}")
        try:
            data = json.loads(text)
        except Exception as e:
            raise RuntimeError(f"failed to parse Pinterest response: {e}") from e
    pin_data = (((data or {}).get("resource_response") or {}).get("data") or {})
    if not isinstance(pin_data, dict) or not pin_data:
        raise RuntimeError("Pinterest pin data not found")
    return pin_data

def _pick_best_video(videos: dict) -> dict | None:
    video_list = (videos or {}).get("video_list") or {}
    if not isinstance(video_list, dict) or not video_list:
        return None
    formats = []
    for key, video in video_list.items():
        if not isinstance(video, dict):
            continue
        url = str(video.get("url") or "").strip()
        if not url:
            continue
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        duration = int(video.get("duration") or 0)
        thumb = str(video.get("thumbnail") or "").strip()
        is_hls = "HLS" in str(key).upper() or ".m3u8" in url.lower()
        formats.append({
            "type": "video",
            "format_id": str(key),
            "url": url,
            "width": width,
            "height": height,
            "duration": duration // 1000 if duration > 1000 else duration,
            "thumbnail": thumb,
            "is_hls": is_hls,
        })
    if not formats:
        return None
    direct = [x for x in formats if not x["is_hls"]]
    pool = direct or formats
    pool.sort(key=lambda x: ((x.get("width") or 0) * (x.get("height") or 0), x.get("height") or 0), reverse=True)
    return pool[0]

def _extract_story_video(story: dict) -> dict | None:
    pages = (story or {}).get("pages") or []
    if not isinstance(pages, list):
        return None
    for page in pages:
        blocks = (page or {}).get("blocks") or []
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if int(block.get("block_type") or 0) == 3 and isinstance(block.get("video"), dict):
                picked = _pick_best_video(block.get("video"))
                if picked:
                    return picked
    return None

def _extract_story_images(story: dict) -> list[dict]:
    pages = (story or {}).get("pages") or []
    if not isinstance(pages, list):
        return []
    items = []
    for page in pages:
        image = (page or {}).get("image") or {}
        originals = (((image.get("images") or {}).get("originals")) or {})
        url = str(originals.get("url") or "").strip()
        if url:
            items.append({
                "type": "photo",
                "format_id": "story_photo",
                "url": url,
                "width": int(originals.get("width") or 0),
                "height": int(originals.get("height") or 0),
            })
    return items

def _extract_pin_media(pin: dict) -> dict:
    title = str(pin.get("title") or pin.get("grid_title") or pin.get("description") or "Pinterest Media").strip()
    desc = str(pin.get("description") or "").strip()
    items = []
    videos = pin.get("videos")
    if isinstance(videos, dict):
        picked = _pick_best_video(videos)
        if picked:
            items.append(picked)
            return {"title": title, "desc": desc, "items": items}
    story = pin.get("story_pin_data")
    if isinstance(story, dict):
        picked = _extract_story_video(story)
        if picked:
            items.append(picked)
            return {"title": title, "desc": desc, "items": items}
    images = pin.get("images")
    orig = ((images or {}).get("orig") or {}) if isinstance(images, dict) else {}
    image_url = str(orig.get("url") or "").strip()
    if image_url:
        items.append({
            "type": "photo",
            "format_id": "photo",
            "url": image_url,
            "width": int(orig.get("width") or 0),
            "height": int(orig.get("height") or 0),
        })
        return {"title": title, "desc": desc, "items": items}
    if isinstance(story, dict):
        story_images = _extract_story_images(story)
        if story_images:
            return {"title": title, "desc": desc, "items": story_images}
    embed = pin.get("embed")
    if isinstance(embed, dict) and str(embed.get("type") or "").lower() == "gif":
        src = str(embed.get("src") or "").strip()
        if src:
            items.append({
                "type": "video",
                "format_id": "gif",
                "url": src,
                "width": 0,
                "height": 0,
                "duration": 0,
                "thumbnail": "",
                "is_hls": False,
            })
            return {"title": title, "desc": desc, "items": items}
    raise RuntimeError(f"no media found for pin ID: {pin.get('id') or '-'}")

def _format_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "0 B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"

def _format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec <= 0:
        return "0 B/s"
    value = float(bytes_per_sec)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{int(value)} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB/s"

def _format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

async def _safe_edit_status(bot, chat_id, status_msg_id, text: str):
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass

async def _safe_edit_progress(bot, chat_id, status_msg_id, title: str, downloaded: int, total: int = 0, speed_bps: float = 0.0, eta_seconds: float | None = None):
    if not status_msg_id:
        return
    lines = [f"<b>{html.escape(title)}</b>", ""]
    if total > 0:
        pct = min(downloaded * 100 / total, 100.0)
        lines.append(f"<code>{html.escape(_format_size(downloaded))}/{html.escape(_format_size(total))} downloaded</code>")
        lines.append(f"<code>{pct:.1f}%</code>")
    else:
        lines.append(f"<code>{html.escape(_format_size(downloaded))} downloaded</code>")
    if speed_bps > 0:
        lines.append(f"<code>Speed: {html.escape(_format_speed(speed_bps))}</code>")
    if eta_seconds is not None and eta_seconds >= 0 and total > 0 and speed_bps > 0:
        lines.append(f"<code>ETA: {html.escape(_format_eta(eta_seconds))}</code>")
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text="\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass

async def _probe_total_bytes(session, url: str, headers: dict | None = None) -> int:
    try:
        async with session.head(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            if total > 0:
                return total
    except Exception:
        pass
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

async def _aria2c_download_with_progress(session, media_url: str, out_path: str, bot, chat_id, status_msg_id, title_text: str, headers: dict | None = None):
    aria2 = shutil.which("aria2c")
    if not aria2:
        raise RuntimeError("aria2c not found in PATH")
    total = await _probe_total_bytes(session, media_url, headers=headers)
    out_dir = os.path.dirname(out_path) or "."
    out_name = os.path.basename(out_path)
    cmd = [
        aria2, "--dir", out_dir, "--out", out_name, "--file-allocation=none", "--allow-overwrite=true",
        "--auto-file-renaming=false", "--continue=true", "--max-connection-per-server=8", "--split=8",
        "--min-split-size=1M", "--summary-interval=0", "--download-result=hide", "--console-log-level=warn"
    ]
    for k, v in (headers or {}).items():
        if v:
            cmd.extend(["--header", f"{k}: {v}"])
    cmd.append(media_url)
    _dbg("aria2c start | out=%s url=%s", out_path, _clip(media_url))
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    last_edit = -10.0
    last_sample_size = 0
    last_sample_ts = time.time()
    while proc.returncode is None:
        await asyncio.sleep(0.7)
        if not os.path.exists(out_path):
            continue
        try:
            downloaded = os.path.getsize(out_path)
        except Exception:
            continue
        if downloaded <= 0:
            continue
        now = time.time()
        elapsed = max(now - last_sample_ts, 0.001)
        speed_bps = max(downloaded - last_sample_size, 0) / elapsed
        eta = ((total - downloaded) / speed_bps) if total > 0 and speed_bps > 0 and downloaded <= total else None
        if now - last_edit < 3 and last_edit >= 0:
            continue
        await _safe_edit_progress(bot, chat_id, status_msg_id, title_text, downloaded, total, speed_bps, eta)
        last_edit = now
        last_sample_size = downloaded
        last_sample_ts = now
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore").strip() if stderr else ""
        raise RuntimeError(err or f"aria2c exited with code {proc.returncode}")

async def _aiohttp_download_with_progress(session, media_url: str, out_path: str, bot, chat_id, status_msg_id, title_text: str, headers: dict | None = None):
    async with session.get(media_url, headers=headers, timeout=aiohttp.ClientTimeout(total=600), allow_redirects=True) as r:
        if r.status >= 400:
            raise RuntimeError(f"Download failed: HTTP {r.status}")
        total = int(r.headers.get("Content-Length", 0) or 0)
        downloaded = 0
        last_edit = -10.0
        last_sample_size = 0
        last_sample_ts = time.time()
        async with aiofiles.open(out_path, "wb") as f:
            async for chunk in r.content.iter_chunked(64 * 1024):
                if not chunk:
                    continue
                await f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                elapsed = max(now - last_sample_ts, 0.001)
                speed_bps = max(downloaded - last_sample_size, 0) / elapsed
                eta = ((total - downloaded) / speed_bps) if total > 0 and speed_bps > 0 and downloaded <= total else None
                if now - last_edit < 3 and last_edit >= 0:
                    continue
                await _safe_edit_progress(bot, chat_id, status_msg_id, title_text, downloaded, total, speed_bps, eta)
                last_edit = now
                last_sample_size = downloaded
                last_sample_ts = now

async def _download_with_best_engine(session, media_url: str, out_path: str, bot, chat_id, status_msg_id, title_text: str, headers: dict | None = None):
    try:
        await _aria2c_download_with_progress(session, media_url, out_path, bot, chat_id, status_msg_id, title_text, headers=headers)
    except Exception as e:
        log.warning("Pinterest aria2c failed, fallback aiohttp | err=%r", e)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        await _aiohttp_download_with_progress(session, media_url, out_path, bot, chat_id, status_msg_id, title_text, headers=headers)

async def _ffmpeg_hls_download(media_url: str, out_path: str, bot, chat_id, status_msg_id, title_text: str, headers: dict | None = None):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH")
    await _safe_edit_status(bot, chat_id, status_msg_id, f"<b>{html.escape(title_text)}</b>\n\n<code>Processing HLS stream...</code>")
    header_text = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items() if v)
    cmd = [ffmpeg, "-y"]
    if header_text:
        cmd += ["-headers", header_text]
    cmd += ["-i", media_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", out_path]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore").strip() if stderr else ""
        raise RuntimeError(err or f"ffmpeg exited with code {proc.returncode}")

def _guess_ext(item: dict) -> str:
    url = str(item.get("url") or "").lower()
    media_type = str(item.get("type") or "").lower()
    if media_type == "video":
        if item.get("is_hls") or ".m3u8" in url:
            return ".mp4"
        if ".gif" in url:
            return ".gif"
        if ".webm" in url:
            return ".webm"
        if ".mov" in url:
            return ".mov"
        return ".mp4"
    if ".png" in url:
        return ".png"
    if ".webp" in url:
        return ".webp"
    if ".jpeg" in url:
        return ".jpeg"
    return ".jpg"

async def _download_one_item(session, item: dict, title: str, bot, chat_id, status_msg_id, idx: int, total: int) -> dict:
    media_type = str(item.get("type") or "").strip().lower()
    media_url = str(item.get("url") or "").strip()
    if not media_url:
        raise RuntimeError("Pinterest media URL empty")
    ext = _guess_ext(item)
    safe_title = sanitize_filename(title or "Pinterest Media")
    out_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_{safe_title}_{idx}{ext}")
    headers = _build_headers("https://www.pinterest.com/")
    label = "Pinterest video" if media_type == "video" else "Pinterest photo"
    title_text = f"Downloading {label}... ({idx}/{total})"
    if media_type == "video" and (item.get("is_hls") or ".m3u8" in media_url.lower()):
        await _ffmpeg_hls_download(media_url, out_path, bot, chat_id, status_msg_id, title_text, headers=headers)
    else:
        await _download_with_best_engine(session, media_url, out_path, bot, chat_id, status_msg_id, title_text, headers=headers)
    if media_type == "video" and ext != ".gif" and is_invalid_video(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass
        raise RuntimeError("Invalid Pinterest video file")
    return {"type": "video" if media_type == "video" else "photo", "path": out_path}

async def _download_pin_items(parsed: dict, bot, chat_id, status_msg_id) -> dict:
    title = (parsed.get("title") or "Pinterest Media").strip()
    items = parsed.get("items") or []
    if not items:
        raise RuntimeError("Pinterest media items empty")
    session = await get_http_session()
    downloaded = []
    total = len(items)
    for idx, item in enumerate(items, start=1):
        downloaded.append(await _download_one_item(session, item, title, bot, chat_id, status_msg_id, idx, total))
    if len(downloaded) == 1:
        return {"path": downloaded[0]["path"], "title": title, "desc": parsed.get("desc") or ""}
    return {"items": downloaded, "title": title, "desc": parsed.get("desc") or ""}

async def pinterest_scrape_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    del fmt_key,format_id,has_audio
    if not metadata_ready:
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Scraping Pinterest media...</b>")
    url=(raw_url or "").strip()
    if SHORT_RE.search(url):
        url=await _resolve_short_url(url)
    pin_id=_extract_pin_id(url)
    if not pin_id:
        raise RuntimeError("failed to extract Pinterest pin ID")
    pin_data=await _get_pin_data(pin_id)
    parsed=_extract_pin_media(pin_data)
    result=await _download_pin_items(parsed,bot,chat_id,status_msg_id)
    _dbg("pinterest scrape success | pin_id=%s result=%s",pin_id,"album" if result.get("items") else "single")
    return result

async def pinterest_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    try:
        return await pinterest_scrape_download(
            raw_url=raw_url,
            fmt_key=fmt_key,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            format_id=format_id,
            has_audio=has_audio,
            metadata_ready=metadata_ready,
        )
    except Exception as e:
        log.exception("Pinterest scraping failed, fallback to yt-dlp | url=%s err=%r",raw_url,e)
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Pinterest scraping failed</b>\n\n<i>Fallback to yt-dlp...</i>")
        return await ytdlp_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio)
        