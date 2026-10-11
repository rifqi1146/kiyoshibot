"""MovieBox URL parser and direct stream extractor."""
import asyncio
import logging
import os
import re
import time
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as curl_requests
from handlers.dl.constants import MAX_TG_SIZE, TMP_DIR
from handlers.dl.progress import TransferStats
from handlers.dl.utils import FileSizeLimitExceeded
from handlers.moviebox.scraper import fetch_detail, fetch_streams

from .constants import (
    BASE_URL,
    HTTP_TIMEOUT,
    MOVIEBOX_ARIA2_CONNS,
    MOVIEBOX_PROGRESS_INTERVAL,
    UA,
)

log = logging.getLogger(__name__)

_VARIANT_CACHE: dict[str, dict] = {}
_CACHE_TTL = 300.0


def parse_moviebox_url(url: str) -> dict:
    """Parse MovieBox URL into structured dictionary.

    Supports:
    - `https://officialmoviebox.com/movies/<slug>?id=<subjectId>&type=/tv/detail&detailSe=1&detailEp=2`
    - `https://officialmoviebox.com/moviesDetail/<slug>`
    """
    raw = (url or "").strip()
    parsed = urlparse(raw)
    path = parsed.path.strip("/")
    qs = parse_qs(parsed.query)

    slug = ""
    if "/movies/" in raw or path.startswith("movies/"):
        slug = path.split("movies/")[-1].split("/")[0]
    elif "/moviesDetail/" in raw or path.startswith("moviesDetail/"):
        slug = path.split("moviesDetail/")[-1].split("/")[0]
    else:
        parts = [p for p in path.split("/") if p]
        if parts:
            slug = parts[-1]

    # Extract query params if present
    sid = (qs.get("id") or [""])[0]
    stype = (qs.get("type") or [""])[0]
    se_str = (qs.get("detailSe") or [""])[0]
    ep_str = (qs.get("detailEp") or [""])[0]

    se = int(se_str) if se_str.isdigit() else 0
    ep = int(ep_str) if ep_str.isdigit() else 0
    is_tv = "/tv/" in stype.lower() if stype else False

    return {
        "slug": slug,
        "subject_id": sid,
        "season": se,
        "episode": ep,
        "is_tv": is_tv,
        "raw_url": raw,
    }


def resolve_moviebox_streams(
    url: str,
    season: int | None = None,
    episode: int | None = None,
) -> tuple[dict, list[dict]]:
    """Resolve direct MP4 streams for a MovieBox URL.

    Returns:
    (detail_dict, variants_list)
    """
    parsed = parse_moviebox_url(url)
    slug = parsed["slug"]
    se = int(season) if season is not None else parsed["season"]
    ep = int(episode) if episode is not None else parsed["episode"]

    # If subject_id missing, fetch detail by slug
    sid = parsed["subject_id"]
    detail = fetch_detail(sid if sid else slug)
    if not detail:
        raise RuntimeError(f"MovieBox detail not found for {slug or url}.")

    sid = detail.get("subject_id") or sid
    slug = detail.get("slug") or slug
    is_tv = detail.get("is_tv", False)

    # For TV Series default to season 1, ep 1 if not specified
    if is_tv and se == 0 and ep == 0:
        se = 1
        ep = 1

    streams = fetch_streams(sid, se=se, ep=ep, slug=slug, is_tv=is_tv)
    if not streams:
        raise RuntimeError(
            f"No video streams found for {detail.get('title')} "
            + (f"S{se}E{ep}" if is_tv else "")
        )

    variants = []
    for s in streams:
        res = s.get("resolution") or "480"
        try:
            h = int(re.sub(r"\D", "", res))
        except ValueError:
            h = 480
        fid = f"{h}p"
        variants.append({
            "format_id": fid,
            "label": f"{h}p",
            "height": h,
            "width": 1920 if h >= 1080 else (1280 if h >= 720 else (854 if h >= 480 else 640)),
            "filesize": s.get("size") or 0,
            "total_size": s.get("size") or 0,
            "url": s.get("url"),
            "codec": s.get("codec") or "h264",
            "format": s.get("format") or "MP4",
            "ext": "mp4",
            "has_audio": True,
            "fps": "-",
            "season": se,
            "episode": ep,
        })

    # Dedup by height, pick largest filesize
    dedup = {}
    for v in variants:
        prev = dedup.get(v["height"])
        if not prev or v["total_size"] > prev["total_size"]:
            dedup[v["height"]] = v
    clean_vars = sorted(dedup.values(), key=lambda x: x["height"], reverse=True)

    return detail, clean_vars


async def download_video(
    url: str,
    format_id: str,
    out_path: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    season: int | None = None,
    episode: int | None = None,
    notify: bool = True,
) -> dict:
    """Download MovieBox video to `out_path` via aria2c or streaming curl_cffi."""
    detail, variants = await asyncio.to_thread(
        resolve_moviebox_streams, url, season=season, episode=episode
    )
    if not variants:
        raise RuntimeError("No video streams available to download.")

    chosen_fid = (format_id or "").strip().lower()
    target = next((v for v in variants if v["format_id"].lower() == chosen_fid), None)
    if not target:
        target = variants[0]

    dl_url = target["url"]
    slug = detail.get("slug") or ""
    sid = detail.get("subject_id") or ""
    se = target.get("season", 0)
    ep = target.get("episode", 0)
    is_tv = detail.get("is_tv", False)

    referer = (
        f"{BASE_URL}/movies/{slug}?id={sid}"
        f"&type={'/tv/detail' if is_tv else '/movie/detail'}"
        f"&detailSe={se}&detailEp={ep}&lang=en"
    )
    headers = {
        "User-Agent": UA,
        "Referer": referer,
        "Origin": BASE_URL,
    }

    total_hint = int(target.get("total_size") or target.get("filesize") or 0)
    if total_hint > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total_hint / 1024 ** 3:.2f} GB). Download canceled."
        )

    label = f"MovieBox {target.get('label')}"

    # Engine 1: aria2c 16 connections
    try:
        from handlers.dl.aria2 import download_aria2
        ok = await download_aria2(
            dl_url,
            out_path,
            headers=headers,
            total_size=total_hint,
            kind="MovieBox download",
            label=label,
            title=title,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            notify=notify,
            edit_interval=MOVIEBOX_PROGRESS_INTERVAL,
            timeout=HTTP_TIMEOUT * 12,
        )
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return {"path": out_path, "title": title}
    except FileSizeLimitExceeded:
        raise
    except Exception as e:
        log.warning("MovieBox aria2c exception, fallback streaming | err=%r", e)

    # Engine 2: streaming curl_cffi fallback
    def _get():
        return curl_requests.get(
            dl_url,
            headers=headers,
            impersonate="chrome",
            stream=True,
            timeout=HTTP_TIMEOUT,
        )

    resp = await asyncio.to_thread(_get)
    if resp.status_code not in (200, 206):
        raise RuntimeError(f"HTTP {resp.status_code} while downloading MovieBox video.")

    try:
        total = int(resp.headers.get("Content-Length") or 0)
    except Exception:
        total = 0
    if total > MAX_TG_SIZE:
        resp.close()
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total / 1024 ** 3:.2f} GB). Download canceled."
        )

    stats = TransferStats(total)

    def _write():
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 512):
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
            kind="MovieBox download",
            label=label,
            log_interval=MOVIEBOX_PROGRESS_INTERVAL,
            edit_interval=MOVIEBOX_PROGRESS_INTERVAL,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                title=title,
                kind="MovieBox download",
                label=label,
                log_interval=MOVIEBOX_PROGRESS_INTERVAL,
                edit_interval=MOVIEBOX_PROGRESS_INTERVAL,
            )
        await asyncio.sleep(0.5)

    await write_task
    stats.log_done("MovieBox download", label=label)
    return {"path": out_path, "title": title}
