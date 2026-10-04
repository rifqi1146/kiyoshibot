"""Scraper BDSMLust (bdsmlust.com)

FLOW SCRAPER
------------
1. Terima URL post -> `scrape_post(url)`.
2. Ambil HTML post (curl_cffi impersonate Chrome). HTTP 404 -> `FileNotFoundError`.
3. Baca metadata:
   - Judul: `<h1>` -> fallback `og:title` -> `<title>`. Buang suffix
     ` - BDSM Tube BDSMLust` dan prefix `Watch `.
   - Durasi: `<meta itemprop="duration" content="P0DT0H42M27S">` (ISO 8601)
     -> detik. Fallback JSON-LD `"duration":"PT42M27S"`.
   - Thumbnail: `og:image`.
4. Temukan iframe embed di HTML:
   Jalur A (bdsmstreak.com/embed/<id>):
     - GET halaman embed (Referer = post).
     - Ambil `<source src>` (biasanya `https://cdn1.mespeaks.com/<id>.mp4`).
     - Poster dari `<video poster>`.
   Jalur B (vid-bx.com/embed/<id> atau bdsmx.tube/embed/<id>):
     - Ekstrak `<id>` dari path embed.
     - GET `https://bdsmx.tube/api/videofile.php?video_id=<id>`.
     - JSON array; ambil `video_url` (string base64 obfuscate dengan homoglyph
       Cyrillic). Decode -> path relatif `/get_file/1/<hash>/.../<id>_sd.mp4/?...`
     - URL final = `https://bdsmx.tube` + path.
5. HEAD request (allow_redirects=True) untuk verifikasi `Content-Length`.
6. Return dict `{title, duration, thumbnail, width, height, download_url,
   filesize}`.

FLOW DOWNLOADER
---------------
1. `download_video(url, out_path, ...)` -> `scrape_post` dapat `download_url`.
2. Coba aria2c paralel (`BDSMLUST_ARIA2_CONNS=16`); aria2c mengikuti redirect
   302 ke CDN ahcdn otomatis.
3. Fallback: streaming `curl_cffi` chunk 512KB jika aria2c gagal.
4. `TransferStats` untuk progress bar Telegram + speed/ETA + batas `MAX_TG_SIZE`.
5. Return dict `{"path", "title"}`; pemanggil (`main.py`) menangani mp3.
"""
import asyncio
import base64
import html as html_mod
import logging
import os
import re

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename
from handlers.dl.progress import TransferStats

from .constants import (
    UA,
    HTTP_TIMEOUT,
    BDSMLUST_ARIA2_CONNS,
    BDSMLUST_PROGRESS_INTERVAL,
)

log = logging.getLogger(__name__)

# Tabel homoglyph Cyrillic -> ASCII untuk decode `video_url` KVS.
# Base64 alphabet A-Z a-z 0-9 + / ; situs menyisipkan karakter Cyrillic yang
# mirip huruf Latin untuk mengaburkan decoder sederhana.
_HOMOGLYPH_MAP = {
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K",
    "\u041c": "M", "\u041d": "H", "\u041e": "O", "\u0420": "P",
    "\u0421": "C", "\u0422": "T", "\u0423": "Y", "\u0425": "X",
    "\u0406": "I", "\u0417": "3", "\u0405": "S", "\u0492": "F",
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
    "\u0441": "c", "\u0445": "x", "\u0443": "y", "\u0456": "i",
    "\u043a": "k", "\u043c": "m", "\u043d": "h", "\u0442": "t",
    "\u0432": "b", "\u04ae": "Y",
}

_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*BDSM\s+Tube\s+BDSMLust.*$", re.I)
_DURATION_RE = re.compile(
    r"P(?:0?DT)?(?:0?H)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
    re.I,
)


def _decode_video_url(raw: str) -> str:
    """Decode `video_url` dari API bdsmx.tube (base64 + homoglyph)."""
    norm = "".join(_HOMOGLYPH_MAP.get(ch, ch) for ch in raw)
    # ',' dan '~' adalah obfuscation tambahan; kembalikan ke bentuk b64 valid
    clean = norm.replace(",", "/").replace("~", "")
    pad = (-len(clean)) % 4
    b = base64.b64decode(clean + "=" * pad)
    return b.decode("utf-8", errors="replace")


def _parse_iso8601_duration(s: str) -> int:
    """Parse `P0DT0H42M27S` -> detik (int)."""
    m = _DURATION_RE.match(s.strip())
    if not m:
        return 0
    h, mn, sec = m.groups()
    total = 0
    if h:
        total += int(h) * 3600
    if mn:
        total += int(mn) * 60
    if sec:
        total += int(sec)
    return total


def _probe_direct_size(url: str, referer: str, session: curl_requests.Session) -> int:
    """Ukuran file via HEAD (allow_redirects) atau Range fallback."""
    headers = {"User-Agent": UA, "Referer": referer} if referer else {"User-Agent": UA}
    try:
        resp = session.head(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            sz = int(resp.headers.get("Content-Length") or 0)
            if sz > 0:
                return sz
    except Exception as e:
        log.debug("BDSMLust HEAD probe failed | err=%r", e)

    try:
        resp = session.get(
            url,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        cr = resp.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        if resp.status_code in (200, 206):
            return int(resp.headers.get("Content-Length") or 0)
    except Exception as e:
        log.debug("BDSMLust Range probe failed | err=%r", e)
    return 0


def _resolve_embed_streak(
    iframe_src: str,
    post_url: str,
    session: curl_requests.Session,
) -> dict:
    """Jalur A: bdsmstreak.com/embed/<id> -> <source src> langsung."""
    url = iframe_src if iframe_src.startswith("http") else "https:" + iframe_src
    resp = session.get(url, headers={"Referer": post_url}, timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} dari embed bdsmstreak")
    text = resp.text
    src_m = re.search(
        r"<source[^>]+src\s*=\s*[\"']([^\"']+)[\"']",
        text,
        re.I,
    )
    if not src_m:
        raise RuntimeError("Tag <source> tidak ditemukan di embed bdsmstreak")
    poster_m = re.search(
        r"<video[^>]+poster\s*=\s*[\"']([^\"']+)[\"']",
        text,
        re.I,
    )
    return {
        "download_url": src_m.group(1),
        "poster": poster_m.group(1) if poster_m else "",
        "referer": "https://bdsmstreak.com/",
    }


def _resolve_embed_bdsmx(
    iframe_src: str,
    post_url: str,
    session: curl_requests.Session,
) -> dict:
    """Jalur B: vid-bx.com / bdsmx.tube embed -> API videofile -> get_file."""
    url = iframe_src if iframe_src.startswith("http") else "https:" + iframe_src

    # `vid-bx.com` me-redirect ke `bdsmx.tube`; ambil video_id dari path.
    m = re.search(r"/embed/(\d+)", url)
    if not m:
        raise RuntimeError(f"ID video tidak ditemukan pada embed: {url}")
    video_id = m.group(1)

    api_url = f"https://bdsmx.tube/api/videofile.php?video_id={video_id}"
    resp = session.get(
        api_url,
        headers={"Referer": "https://bdsmx.tube/"},
        timeout=HTTP_TIMEOUT,
        allow_redirects=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} dari API videofile bdsmx")
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Respons API videofile bukan JSON: {e}") from e
    if not data or not isinstance(data, list):
        raise RuntimeError("Respons API videofile kosong / format salah")
    entry = data[0]
    raw_b64 = entry.get("video_url") or ""
    if not raw_b64:
        raise RuntimeError("Field video_url kosong pada API videofile")

    rel_path = _decode_video_url(raw_b64)
    download_url = "https://bdsmx.tube" + rel_path
    return {
        "download_url": download_url,
        "poster": "",
        "referer": "https://bdsmx.tube/",
    }


def scrape_post(url: str) -> dict:
    """Scrape metadata + download URL dari post bdsmlust.com."""
    session = curl_requests.Session(impersonate="chrome", headers={"User-Agent": UA})
    resp = session.get(url, timeout=HTTP_TIMEOUT)

    if resp.status_code == 404:
        raise FileNotFoundError(f"Video tidak ditemukan (HTTP 404): {url}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} saat mengakses BDSMLust")

    soup = BeautifulSoup(resp.text, "html.parser")

    # 1. Judul
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og_t = soup.find("meta", property="og:title")
        if og_t and og_t.get("content"):
            title = og_t.get("content").strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = html_mod.unescape(title or "BDSMLust Video")
    title = _TITLE_SUFFIX_RE.sub("", title)
    title = re.sub(r"^Watch\s+", "", title, flags=re.I).strip()
    title = sanitize_filename(title, 100)

    # 2. Durasi (detik)
    duration = 0
    dur_meta = soup.find("meta", attrs={"itemprop": "duration"})
    if dur_meta and dur_meta.get("content"):
        duration = _parse_iso8601_duration(dur_meta.get("content"))
    if not duration:
        m = re.search(r'"duration"\s*:\s*"(PT[^"]+)"', resp.text)
        if m:
            duration = _parse_iso8601_duration(m.group(1))

    # 3. Thumbnail
    thumbnail = ""
    img_meta = soup.find("meta", property="og:image")
    if img_meta and img_meta.get("content"):
        thumbnail = img_meta.get("content").strip()

    # 4. Dimensi (bdsmlust tidak ekspos; default 1280x720)
    width, height = 1280, 720

    # 5. Cari iframe embed
    iframe_srcs = []
    for ifr in soup.find_all("iframe"):
        src = ifr.get("data-src") or ifr.get("src") or ""
        if src:
            iframe_srcs.append(src)

    download_url = ""
    referer = url
    embed_type = ""

    for src in iframe_srcs:
        low = src.lower()
        if "bdsmstreak.com/embed/" in low:
            info = _resolve_embed_streak(src, url, session)
            download_url = info["download_url"]
            referer = info["referer"]
            embed_type = "bdsmstreak"
            if info.get("poster") and not thumbnail:
                thumbnail = info["poster"]
            break
        if "vid-bx.com/embed/" in low or "bdsmx.tube/embed/" in low:
            info = _resolve_embed_bdsmx(src, url, session)
            download_url = info["download_url"]
            referer = info["referer"]
            embed_type = "bdsmx"
            break

    # Fallback: tag <video><source> langsung di halaman post
    if not download_url:
        for video in soup.find_all("video"):
            for source in video.find_all("source"):
                s = source.get("src") or ""
                if s and ".mp4" in s.lower():
                    download_url = s
                    referer = url
                    embed_type = "direct"
                    break
            if download_url:
                break

    if not download_url:
        raise RuntimeError(
            "Tidak ditemukan embed video pada post BDSMLust ini. "
            "Mungkin post telah dihapus atau menggunakan host yang belum didukung."
        )

    # 6. HEAD probe ukuran
    filesize = _probe_direct_size(download_url, referer, session)

    return {
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "width": width,
        "height": height,
        "download_url": download_url,
        "referer": referer,
        "filesize": filesize,
        "embed_type": embed_type,
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
    """Download paralel multi-koneksi via aria2c."""
    from handlers.dl.aria2 import _resolve_aria2
    aria2 = _resolve_aria2()
    if not aria2:
        return False

    out_dir = os.path.dirname(os.path.abspath(out_path))
    out_file = os.path.basename(out_path)

    cmd = [
        aria2,
        f"-x{BDSMLUST_ARIA2_CONNS}",
        f"-s{BDSMLUST_ARIA2_CONNS}",
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

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
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
                        kind="BDSMLust download",
                        label="BDSMLust MP4",
                        log_interval=BDSMLUST_PROGRESS_INTERVAL,
                        edit_interval=BDSMLUST_PROGRESS_INTERVAL,
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
        log.warning("BDSMLust aria2c failed | code=%s err=%s", proc.returncode, err[:200])
        return False

    final_size = os.path.getsize(out_path)
    stats.sample(final_size)
    if notify and status_msg_id:
        await stats.emit(
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            kind="BDSMLust download",
            label="BDSMLust MP4",
            log_interval=BDSMLUST_PROGRESS_INTERVAL,
            edit_interval=BDSMLUST_PROGRESS_INTERVAL,
        )
    stats.log_done("BDSMLust download", label="BDSMLust MP4")
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
        raise RuntimeError("Gagal mendapatkan link download BDSMLust.")
    referer = post.get("referer") or url

    total = int(post.get("filesize") or 0)
    if total > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total / 1024 ** 3:.2f} GB). Download canceled."
        )

    # 1. aria2c paralel
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
        log.warning("BDSMLust aria2c exception, fallback ke streaming | err=%r", e)

    # 2. Fallback streaming curl_cffi
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
            raise RuntimeError(f"HTTP {resp.status_code} saat mengunduh video BDSMLust.")
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
            kind="BDSMLust download",
            label="BDSMLust MP4",
            log_interval=BDSMLUST_PROGRESS_INTERVAL,
            edit_interval=BDSMLUST_PROGRESS_INTERVAL,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                title=title,
                kind="BDSMLust download",
                label="BDSMLust MP4",
                log_interval=BDSMLUST_PROGRESS_INTERVAL,
                edit_interval=BDSMLUST_PROGRESS_INTERVAL,
            )
        await asyncio.sleep(0.5)

    await write_task
    stats.log_done("BDSMLust download", label="BDSMLust MP4")
    return {"path": out_path, "title": title}
