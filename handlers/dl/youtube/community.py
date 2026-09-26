import os
import re
import json
import uuid
import html
import logging
import asyncio
import aiohttp
from urllib.parse import urlparse, parse_qs
from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename

log = logging.getLogger(__name__)

_POST_PATH_RE = re.compile(r"/post/([A-Za-z0-9_-]+)", re.I)
_SHORTS_PATH_RE = re.compile(r"^/shorts/([A-Za-z0-9_-]+)", re.I)

WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
}


def is_youtube_post_url(url: str) -> bool:
    try:
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower()
        if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
            return False
        path = p.path or ""
        if _POST_PATH_RE.search(path):
            return True
        if "/community" in path.lower():
            qs = parse_qs(p.query)
            if "lb" in qs and qs["lb"]:
                return True
        return False
    except Exception:
        return False


def is_youtube_shorts_url(url: str) -> bool:
    try:
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower()
        if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
            return False
        path = p.path or ""
        return bool(_SHORTS_PATH_RE.search(path))
    except Exception:
        return False


def extract_post_id(url: str) -> str:
    try:
        p = urlparse((url or "").strip())
        m = _POST_PATH_RE.search(p.path or "")
        if m:
            return m.group(1)
        qs = parse_qs(p.query)
        if "lb" in qs and qs["lb"]:
            return qs["lb"][0]
    except Exception:
        pass
    return ""


def _walk_json(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk_json(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json(item)


def _to_full_res_url(img_url: str) -> str:
    # Google/YouTube image URLs on yt3.ggpht.com / *.googleusercontent.com:
    # replace =s\d+... suffix with =s0 for original full uncompressed resolution
    if not img_url:
        return ""
    if "=s" in img_url:
        return re.sub(r"=s\d+.*$", "=s0", img_url)
    return img_url


async def _download_file(session: aiohttp.ClientSession, url: str, dest_path: str) -> bool:
    try:
        async with session.get(url, headers={"User-Agent": WEB_HEADERS["User-Agent"]}, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                log.warning("Failed to fetch image | status=%s url=%s", r.status, url)
                return False
            data = await r.read()
            if not data:
                return False
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
    except Exception as e:
        log.warning("Error downloading image | url=%s err=%r", url, e)
        return False


async def download_youtube_post(
    url: str,
    bot=None,
    chat_id: int | None = None,
    status_msg_id: int | None = None,
) -> dict:
    post_id = extract_post_id(url)
    target_url = f"https://www.youtube.com/post/{post_id}" if post_id else url
    log.info("Fetching YouTube community post | url=%s post_id=%s", url, post_id)

    async with aiohttp.ClientSession() as session:
        async with session.get(target_url, headers=WEB_HEADERS, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"YouTube returned HTTP {resp.status} for community post")
            html_text = await resp.text()

        m = re.search(r'var ytInitialData\s*=\s*(\{.*?\});</script>', html_text, re.S)
        if not m:
            # Fallback inline data
            m = re.search(r'window\["ytInitialData"\]\s*=\s*(\{.*?\});</script>', html_text, re.S)
        if not m:
            raise RuntimeError("Gagal mengekstrak data YouTube Community Post")

        try:
            data = json.loads(m.group(1))
        except Exception as e:
            raise RuntimeError(f"Gagal parse JSON YouTube Community Post: {e}")

        post = None
        for k, v in _walk_json(data):
            if k in ("backstagePostRenderer", "sharedPostRenderer") and isinstance(v, dict):
                post = v
                break

        if not post:
            raise RuntimeError("Postingan komunitas YouTube tidak ditemukan atau sudah dihapus")

        author_runs = post.get("authorText", {}).get("runs", [])
        author = "".join(r.get("text", "") for r in author_runs).strip()
        content_runs = post.get("contentText", {}).get("runs", [])
        content = "".join(r.get("text", "") for r in content_runs).strip()

        # Attachment parsing
        attachment = post.get("backstageAttachment", {})
        image_urls: list[str] = []

        # Case 1: single image
        single = attachment.get("backstageImageRenderer", {}).get("image", {}).get("thumbnails", [])
        if single:
            best = max(single, key=lambda t: t.get("width", 0))
            best_url = _to_full_res_url(best.get("url", ""))
            if best_url:
                image_urls.append(best_url)

        # Case 2: carousel / multi-image
        multi = attachment.get("postMultiImageRenderer", {}).get("images", [])
        if multi:
            for item in multi:
                thumbs = item.get("backstageImageRenderer", {}).get("image", {}).get("thumbnails", [])
                if thumbs:
                    b = max(thumbs, key=lambda t: t.get("width", 0))
                    b_url = _to_full_res_url(b.get("url", ""))
                    if b_url:
                        image_urls.append(b_url)

        title_parts = []
        if author:
            title_parts.append(author)
        if content:
            title_parts.append(content)
        caption_text = "\n\n".join(title_parts).strip() or "YouTube Community Post"

        os.makedirs(TMP_DIR, exist_ok=True)
        job_id = uuid.uuid4().hex[:10]

        if not image_urls:
            # Case 3: text-only post (tidak ada gambar)
            log.info("YouTube community post is text-only | post_id=%s", post_id)
            if bot and chat_id:
                safe_author = html.escape(author or "YouTube Post")
                safe_content = html.escape(content) if content else "<i>(Postingan tanpa teks)</i>"
                msg_text = (
                    f"📝 <b>{safe_author}</b>\n\n"
                    f"{safe_content}"
                )
                await bot.send_message(chat_id=chat_id, text=msg_text, parse_mode="HTML")
                return {"handled": True, "title": caption_text}
            raise RuntimeError("Postingan komunitas ini hanya berisi teks (tidak ada media untuk diunduh).")

        # Download semua gambar
        saved_paths: list[str] = []
        for idx, i_url in enumerate(image_urls):
            dest = f"{TMP_DIR}/{job_id}_{idx}.jpg"
            ok = await _download_file(session, i_url, dest)
            if ok and os.path.exists(dest) and os.path.getsize(dest) > 0:
                saved_paths.append(dest)

        if not saved_paths:
            raise RuntimeError("Gagal mengunduh gambar dari postingan komunitas YouTube")

        if len(saved_paths) == 1:
            log.info("YouTube community post downloaded 1 image | file=%s", saved_paths[0])
            return {
                "path": saved_paths[0],
                "title": caption_text,
                "source": "youtube",
                "kind": "photo",
            }

        log.info("YouTube community post downloaded %d images | job_id=%s", len(saved_paths), job_id)
        return {
            "items": [{"path": p, "type": "photo"} for p in saved_paths],
            "title": caption_text,
            "source": "youtube",
            "kind": "album",
        }
