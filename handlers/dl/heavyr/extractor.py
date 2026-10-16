import asyncio
import logging
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.progress import TransferStats
from handlers.dl.proc import spawn_process, DEVNULL, PIPE
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename

from .constants import (
    UA,
    HTTP_TIMEOUT,
    HEAVYR_EMBED_ORIGIN,
    HEAVYR_REFERER,
    HEAVYR_ARIA2_CONNS,
    HEAVYR_PROGRESS_INTERVAL,
)

log = logging.getLogger(__name__)

_VIDEO_FILE_RE = re.compile(
    r"<video[^>]*id=[\"']video-file[\"'][^>]*>(.*?)</video>",
    re.S | re.I,
)
_SOURCE_SRC_RE = re.compile(r"<source[^>]+src=[\"']([^\"']+)[\"']", re.I)
_POSTER_RE = re.compile(r"<video[^>]+poster=[\"']([^\"']+)[\"']", re.I)
_FLOW_TITLE_RE = re.compile(
    r"<a\b[^>]*class=[\"'][^\"']*flow-title[^\"']*[\"'][^>]*>(.*?)</a>",
    re.S | re.I,
)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']")
_VIDEO_ID_IN_PATH_RE = re.compile(r"/video/(\d+)/")
_EXTINF_RE = re.compile(r"#EXTINF:([\d.]+)", re.I)
_BYTERANGE_RE = re.compile(r"#EXT-X-BYTERANGE:(\d+)@(\d+)", re.I)
_MAP_URI_RE = re.compile(r'#EXT-X-MAP:URI="([^"]+)"', re.I)
_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF", re.I)
_KEY_RE = re.compile(r"#EXT-X-KEY", re.I)
_CONTENT_RANGE_RE = re.compile(r"/(\d+)\s*$")


def _extract_video_id(url: str) -> str | None:
    m = re.search(r"heavy-r\.com/(?:video|embed(?:small)?)/(\d+)", (url or ""), re.I)
    return m.group(1) if m else None


def _looks_like_post_page(url: str) -> bool:
    return bool(re.search(r"heavy-r\.com/video/\d+", (url or ""), re.I))


def _extract_source(body: str) -> str:
    vm = _VIDEO_FILE_RE.search(body)
    scope = vm.group(1) if vm else body
    m = _SOURCE_SRC_RE.search(scope)
    return m.group(1).strip() if m else ""


def _parse_post_metadata(body: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    title = ""
    og_t = soup.find("meta", property="og:title")
    if og_t and og_t.get("content"):
        title = og_t.get("content").strip()
    if not title:
        h1 = soup.find("h1", class_="video-title") or soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = re.sub(r"\s*-\s*Heavy-R\.com.*$", "", title or "", flags=re.I).strip()

    thumbnail = ""
    img = soup.find("meta", property="og:image")
    if img and img.get("content"):
        thumbnail = img.get("content").strip()
    if not thumbnail:
        pm = _POSTER_RE.search(body)
        if pm:
            thumbnail = pm.group(1).strip()
    return {"title": title, "thumbnail": thumbnail}


def _fetch_post_page(url: str, session: curl_requests.Session) -> dict | None:
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
    except Exception as e:
        log.debug("HeavyR post fetch failed | url=%s err=%r", url, e)
        return None
    if resp.status_code != 200:
        return None
    body = resp.text
    if 'id="video-file"' not in body:
        return None
    source = _extract_source(body)
    if not source:
        return None
    meta = _parse_post_metadata(body)
    return {
        "title": meta["title"],
        "thumbnail": meta["thumbnail"],
        "source": source,
    }


def _fetch_embed_page(vid: str, session: curl_requests.Session) -> dict | None:
    url = "%s/embed/%s/" % (HEAVYR_EMBED_ORIGIN, vid)
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
    except Exception as e:
        log.debug("HeavyR embed fetch failed | vid=%s err=%r", vid, e)
        return None
    if resp.status_code != 200:
        return None
    body = resp.text

    ft = _FLOW_TITLE_RE.search(body)
    if not ft:
        log.debug("HeavyR embed tanpa flow-title | vid=%s", vid)
        return None
    anchor = ft.group(0)
    href_m = _HREF_RE.search(anchor)
    hid_m = _VIDEO_ID_IN_PATH_RE.search(href_m.group(1) if href_m else "")
    real_id = hid_m.group(1) if hid_m else None
    if real_id != vid:
        log.info(
            "HeavyR embed ID mismatch (video deleted) | vid=%s real=%s",
            vid, real_id,
        )
        return None

    source = _extract_source(body)
    if not source or ".mp4" not in source.lower():
        log.debug("HeavyR embed tanpa source MP4 | vid=%s", vid)
        return None

    title = re.sub(r"<[^>]+>", "", ft.group(1)).strip()
    poster_m = _POSTER_RE.search(body)
    return {
        "title": title,
        "poster": poster_m.group(1).strip() if poster_m else "",
        "source": source,
    }


def _resolve_hls(playlist_url: str, session: curl_requests.Session) -> tuple[str, int, int]:
    resp = session.get(playlist_url, timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} dari playlist HLS Heavy-R")
    text = resp.text

    if _KEY_RE.search(text):
        raise RuntimeError(
            "Heavy-R HLS playlist is encrypted (#EXT-X-KEY) — this path is not supported yet"
        )

    if _STREAM_INF_RE.search(text):
        lines = [
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not lines:
            raise RuntimeError("Master playlist HLS Heavy-R tanpa varian")
        return _resolve_hls(urljoin(playlist_url, lines[0]), session)

    map_m = _MAP_URI_RE.search(text)
    if not map_m:
        raise RuntimeError("Playlist HLS Heavy-R tanpa #EXT-X-MAP")
    m4s_url = urljoin(playlist_url, map_m.group(1))

    ranges = _BYTERANGE_RE.findall(text)
    size = (int(ranges[-1][0]) + int(ranges[-1][1])) if ranges else 0

    durs = [float(x) for x in _EXTINF_RE.findall(text)]
    duration = int(round(sum(durs))) if durs else 0
    return m4s_url, size, duration


def _probe_size(
    url: str,
    referer: str,
    session: curl_requests.Session,
) -> int:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer

    try:
        resp = session.get(
            url,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        cr = resp.headers.get("Content-Range") or ""
        m = _CONTENT_RANGE_RE.search(cr)
        if m:
            return int(m.group(1))
        if resp.status_code == 200:
            cl = int(resp.headers.get("Content-Length") or 0)
            if cl > 0:
                return cl
    except Exception as e:
        log.debug("HeavyR Range probe failed | err=%r", e)

    try:
        resp = session.head(
            url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True,
        )
        if resp.status_code == 200:
            cl = int(resp.headers.get("Content-Length") or 0)
            if cl > 0:
                return cl
    except Exception as e:
        log.debug("HeavyR HEAD probe failed | err=%r", e)
    return 0


def scrape_post(url: str) -> dict:
    vid = _extract_video_id(url)
    if not vid:
        raise RuntimeError(f"Not a valid Heavy-R video URL: {url}")

    session = curl_requests.Session(
        impersonate="chrome", headers={"User-Agent": UA}
    )

    title = ""
    thumbnail = ""
    source = ""
    source_type = ""
    referer = HEAVYR_REFERER
    duration = 0
    filesize = 0
    hls_playlist = ""

    if _looks_like_post_page(url):
        post = _fetch_post_page(url, session)
        if post:
            title = post["title"]
            thumbnail = post["thumbnail"]
            src = post["source"]
            low = src.lower()
            if ".mp4" in low:
                source, source_type = src, "mp4"
            elif ".m3u8" in low:
                hls_playlist = src
                log.debug("HeavyR post menyajikan HLS | vid=%s", vid)
            referer = url
        else:
            log.debug("HeavyR post bukan halaman video | url=%s", url)

    if source_type != "mp4":
        emb = _fetch_embed_page(vid, session)
        if emb:
            source, source_type = emb["source"], "mp4"
            if not title:
                title = emb["title"]
            if not thumbnail:
                thumbnail = emb["poster"]
            log.info("HeavyR resolve via embed MP4 | vid=%s", vid)

    if source_type != "mp4" and hls_playlist:
        m4s_url, filesize, duration = _resolve_hls(hls_playlist, session)
        source, source_type = m4s_url, "hls_m4s"
        referer = HEAVYR_REFERER
        log.info("HeavyR resolve via file m4s | vid=%s", vid)

    if not source:
        raise FileNotFoundError(
            f"Heavy-R: video {vid} not found or unsupported host"
        )

    if source_type == "mp4":
        filesize = _probe_size(source, referer, session)

    title = sanitize_filename(title or f"Heavy-R {vid}", 100)

    return {
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "width": 1280,
        "height": 720,
        "download_url": source,
        "referer": referer,
        "filesize": filesize,
        "source_type": source_type,
    }


async def _download_aria2c(
    dl_url: str,
    referer: str,
    out_path: str,
    total_size: int,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    notify: bool,
) -> bool:
    from handlers.dl.aria2 import _resolve_aria2
    aria2 = _resolve_aria2()
    if not aria2:
        return False

    out_dir = os.path.dirname(os.path.abspath(out_path))
    out_file = os.path.basename(out_path)

    cmd = [
        aria2,
        f"-x{HEAVYR_ARIA2_CONNS}",
        f"-s{HEAVYR_ARIA2_CONNS}",
        "-k1M",
        "--file-allocation=none",
        "--summary-interval=0",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        f"--header=User-Agent: {UA}",
    ]
    if referer:
        cmd.append(f"--header=Referer: {referer}")
    cmd += [
        f"--dir={out_dir}",
        f"--out={out_file}",
        dl_url,
    ]

    proc = await spawn_process(
        *cmd,
        stdout=DEVNULL,
        stderr=PIPE,
    )

    stats = TransferStats(total_size)

    async def _poll_progress():
        while proc.returncode is None:
            if os.path.exists(out_path):
                cur_size = os.path.getsize(out_path)
                stats.sample(cur_size)
                if cur_size > MAX_TG_SIZE:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise FileSizeLimitExceeded("File exceeds 2GB limit. Download canceled.")
                if notify and status_msg_id:
                    await stats.emit(
                        bot=bot,
                        chat_id=chat_id,
                        status_msg_id=status_msg_id,
                        title=title,
                        kind="HeavyR download",
                        label="Heavy-R MP4",
                        log_interval=HEAVYR_PROGRESS_INTERVAL,
                        edit_interval=HEAVYR_PROGRESS_INTERVAL,
                    )
            await asyncio.sleep(0.5)

    poll_task = asyncio.create_task(_poll_progress())
    _, stderr_b = await proc.communicate()
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass

    if proc.returncode != 0 or not os.path.exists(out_path):
        err = (stderr_b or b"").decode("utf-8", errors="ignore").strip()
        log.warning("HeavyR aria2c failed | code=%s err=%s", proc.returncode, err[:200])
        return False

    final_size = os.path.getsize(out_path)
    stats.sample(final_size)
    if notify and status_msg_id:
        await stats.emit(
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            kind="HeavyR download",
            label="Heavy-R MP4",
            log_interval=HEAVYR_PROGRESS_INTERVAL,
            edit_interval=HEAVYR_PROGRESS_INTERVAL,
        )
    stats.log_done("HeavyR download", label="Heavy-R MP4")
    return True


async def download_video(
    url: str,
    out_path: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    notify: bool = True,
) -> dict:
    post = await asyncio.to_thread(scrape_post, url)
    dl_url = post.get("download_url") or ""
    if not dl_url:
        raise RuntimeError("Failed to get Heavy-R download link.")
    referer = post.get("referer") or HEAVYR_REFERER

    total = int(post.get("filesize") or 0)
    if total > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total / 1024 ** 3:.2f} GB). Download canceled."
        )

    try:
        ok = await _download_aria2c(
            dl_url=dl_url,
            referer=referer,
            out_path=out_path,
            total_size=total,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            notify=notify,
        )
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return {"path": out_path, "title": title}
    except FileSizeLimitExceeded:
        raise
    except Exception as e:
        log.warning("HeavyR aria2c exception, fallback ke streaming | err=%r", e)

    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer

    def _get():
        resp = curl_requests.get(
            dl_url,
            headers=headers,
            impersonate="chrome",
            stream=True,
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {resp.status_code} saat mengunduh video Heavy-R.")
        return resp

    resp = await asyncio.to_thread(_get)
    try:
        hdr_total = int(resp.headers.get("Content-Length") or 0)
        if hdr_total > 0:
            total = hdr_total
    except Exception:
        pass

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
            kind="HeavyR download",
            label="Heavy-R MP4",
            log_interval=HEAVYR_PROGRESS_INTERVAL,
            edit_interval=HEAVYR_PROGRESS_INTERVAL,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                title=title,
                kind="HeavyR download",
                label="Heavy-R MP4",
                log_interval=HEAVYR_PROGRESS_INTERVAL,
                edit_interval=HEAVYR_PROGRESS_INTERVAL,
            )
        await asyncio.sleep(0.5)

    await write_task
    stats.log_done("HeavyR download", label="Heavy-R MP4")
    return {"path": out_path, "title": title}
