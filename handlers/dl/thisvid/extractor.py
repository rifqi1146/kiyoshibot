import os
import re
import html as html_lib
import logging
import asyncio
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import FileSizeLimitExceeded
from handlers.dl.progress import TransferStats
from .constants import (
    UA,
    HTTP_TIMEOUT,
    THISVID_REFERER,
    THISVID_ARIA2_CONNS,
    THISVID_PROGRESS_INTERVAL,
)
log = logging.getLogger(__name__)

_FLASHVARS_RE = re.compile(r"var\s+flashvars\s*=\s*(\{.*?\})\s*;", re.S)
_FLASHVARS_PAIR_RE = re.compile(r"([A-Za-z0-9_]+)\s*:\s*'((?:\\.|[^'\\])*)'")
_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_PAGE_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)
_OG_IMG_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_OG_W_RE = re.compile(r'og:video:width"\s+content="(\d+)"', re.I)
_OG_H_RE = re.compile(r'og:video:height"\s+content="(\d+)"', re.I)
_DUR_DESC_RE = re.compile(r'<span class="title">Duration:</span>\s*<span class="title-description">([^<]*)<', re.I)
_DUR_SPAN_RE = re.compile(r'<span class="duration">([^<]*)<', re.I)
_TITLE_SUFFIX_RE = re.compile(r"\s*[-|:]\s*(?:ThisVid(?:\.com|\s+tube)?).*$", re.I)

def _clean_title(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", "", text)
    clean = html_lib.unescape(clean).strip()
    clean = _TITLE_SUFFIX_RE.sub("", clean).strip()
    if clean.lower().startswith("video:"):
        clean = clean[6:].strip()
    return clean

def _parse_duration(text: str) -> int:
    if not text:
        return 0
    parts = [p.strip() for p in text.split(":") if p.strip().isdigit()]
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    try:
        return int(float(text))
    except Exception:
        return 0

def _probe_format_size(url: str) -> int:
    try:
        resp = curl_requests.get(
            url,
            headers={"User-Agent": UA, "Referer": THISVID_REFERER, "Range": "bytes=0-0"},
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
        log.debug("ThisVid size probe failed | url=%s err=%r", url, e)
    return 0

def scrape_post(raw_url: str) -> dict:
    url = (raw_url or "").strip()
    headers = {"User-Agent": UA, "Referer": THISVID_REFERER}
    sess = curl_requests.Session(impersonate="chrome")
    try:
        r = sess.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except Exception as e:
        log.warning("ThisVid fetch failed | url=%s err=%r", url, e)
        raise RuntimeError(f"Failed to fetch ThisVid page: {e}")

    if r.status_code == 404:
        raise FileNotFoundError("Video not found on ThisVid.")
    if r.status_code != 200:
        raise RuntimeError(f"ThisVid returned HTTP {r.status_code}")

    html_text = r.text or ""
    if "This video is a private video" in html_text:
        raise RuntimeError("ThisVid video is private and requires login.")

    m_fv = _FLASHVARS_RE.search(html_text)
    if not m_fv:
        raise RuntimeError("Failed to extract player configuration from ThisVid page.")

    pairs = _FLASHVARS_PAIR_RE.findall(m_fv.group(1))
    data = {k: v.replace(r"\'", "'").replace(r'\"', '"').replace(r"\\", "\\") for k, v in pairs}

    raw_title = data.get("video_title")
    title = _clean_title(raw_title) if raw_title else ""
    if not title:
        m_h1 = _TITLE_RE.search(html_text)
        if m_h1:
            title = _clean_title(m_h1.group(1))
    if not title:
        m_pt = _PAGE_TITLE_RE.search(html_text)
        if m_pt:
            title = _clean_title(m_pt.group(1))
    if not title:
        title = "ThisVid Video"

    dur_text = ""
    m_dur = _DUR_DESC_RE.search(html_text)
    if m_dur:
        dur_text = m_dur.group(1).strip()
    else:
        m_dur2 = _DUR_SPAN_RE.search(html_text)
        if m_dur2:
            dur_text = m_dur2.group(1).strip()
    duration = _parse_duration(dur_text)

    thumbnail = (data.get("preview_url") or data.get("preview_url1") or "").strip()
    if thumbnail.startswith("//"):
        thumbnail = f"https:{thumbnail}"
    if not thumbnail:
        m_img = _OG_IMG_RE.search(html_text)
        if m_img:
            thumbnail = m_img.group(1).strip()

    og_w = 0
    og_h = 0
    m_w = _OG_W_RE.search(html_text)
    if m_w:
        try:
            og_w = int(m_w.group(1))
        except Exception:
            og_w = 0
    m_h = _OG_H_RE.search(html_text)
    if m_h:
        try:
            og_h = int(m_h.group(1))
        except Exception:
            og_h = 0

    raw_candidates = []
    v_main = data.get("video_url")
    if v_main and ("get_file" in v_main or ".mp4" in v_main):
        if data.get("video_url_fhd") == "1":
            h_main = 1080
            lbl_main = "1080p"
        elif data.get("video_url_hd") == "1":
            h_main = 720
            lbl_main = "720p"
        elif og_h > 0:
            if og_w > 0 and og_w < og_h:
                h_main = og_w
                lbl_main = f"{og_w}p"
            else:
                h_main = og_h
                lbl_main = f"{og_h}p"
        else:
            h_main = 480
            lbl_main = "480p"
        raw_candidates.append((lbl_main, h_main, v_main))

    for a_url_key, a_txt_key in (
        ("video_alt_url", "video_alt_url_text"),
        ("video_alt_url2", "video_alt_url2_text"),
        ("video_alt_url3", "video_alt_url3_text"),
    ):
        v_alt = data.get(a_url_key)
        if not v_alt or not ("get_file" in v_alt or ".mp4" in v_alt):
            continue
        txt = (data.get(a_txt_key) or "").strip()
        m_digits = re.search(r"(\d+)p?", txt, re.I)
        h_alt = int(m_digits.group(1)) if m_digits else 240
        lbl_alt = f"{h_alt}p"
        raw_candidates.append((lbl_alt, h_alt, v_alt))

    seen_heights = set()
    variants = []
    res_list = []
    for lbl, h_val, url_val in raw_candidates:
        if h_val in seen_heights:
            continue
        seen_heights.add(h_val)
        size = _probe_format_size(url_val)
        var_entry = {
            "format_id": lbl,
            "height": h_val,
            "label": lbl,
            "url": url_val,
            "filesize": size,
            "total_size": size,
            "has_audio": True,
        }
        variants.append(var_entry)
        res_list.append({
            "height": h_val,
            "format_id": lbl,
            "label": lbl,
            "filesize": size,
            "total_size": size,
            "has_audio": True,
        })

    if not variants:
        raise RuntimeError("No downloadable video stream found on ThisVid page.")

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
    target = next((v for v in all_vars if v["format_id"].lower() == chosen or str(v["height"]) == chosen), None)
    if not target and all_vars:
        target = all_vars[0]
    if not target:
        raise RuntimeError("Selected video resolution not found.")

    dl_url = target["url"]
    headers = {"User-Agent": UA, "Referer": THISVID_REFERER}
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
            conns=THISVID_ARIA2_CONNS,
            kind="ThisVid download",
            label=f"ThisVid {target.get('label')}",
            title=title,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            notify=notify,
            edit_interval=THISVID_PROGRESS_INTERVAL,
            timeout=HTTP_TIMEOUT * 12,
        )
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return {"path": out_path, "title": title}
    except FileSizeLimitExceeded:
        raise
    except Exception as e:
        log.warning("ThisVid aria2c failed, falling back to streaming | err=%r", e)

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
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            kind="ThisVid download",
            label=f"ThisVid {target.get('label')}",
            log_interval=THISVID_PROGRESS_INTERVAL,
            edit_interval=THISVID_PROGRESS_INTERVAL,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                title=title,
                kind="ThisVid download",
                label=f"ThisVid {target.get('label')}",
                log_interval=THISVID_PROGRESS_INTERVAL,
                edit_interval=THISVID_PROGRESS_INTERVAL,
            )
        await asyncio.sleep(0.5)

    await write_task
    stats.log_done("ThisVid download", label=f"ThisVid {target.get('label')}")
    return {"path": out_path, "title": title}
