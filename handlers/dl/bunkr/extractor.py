"""Scraper Bunkr (bunkr.cr / bunkr.ph / ...)

FLOW SCRAPER
------------
1. Album: `scrape_album(url)` -> `GET /a/<id>`, baca tiap `.theItem`:
   - `title` = nama file, span `type-Image`/`type-Video` = tipe
   - `p.theName` = nama, `p.theSize` = ukuran, `img.grid-images_box-img[src]` = thumb
   - `a[href^="/f/"]` = halaman file
2. Halaman file: `resolve_file(page_url)` -> rantai 3 lapis:
     GET  <origin>/f/<id>          -> link `dl.<host>/file/<id>`
     GET  dl.<host>/file/<id>      -> <div id="download-btn" data-id="...">
     POST <origin>/api/_001_v2     {"id": data-id} -> {mediafiles, path, original}
     GET  glb-apisign.cdn.cr/sign?path=<path> -> {token, ex}
   URL final = `mediafiles + path + ?token=&ex=&n=<original>`
   Token bertanggal (field `ex` = epoch); URL tidak boleh di-cache lama.
3. Unduh CDN: `download_to_file` streaming dengan TransferStats (jendela 1s)
   + progress bar + batas MAX_TG_SIZE.
4. Album: `download_album` mengunduh semua media senyap lalu mengembalikan
   `{items:[{path, type}], title}` -> `service._send_media_group_result`
   memecah per 10 file (batas Telegram).
5. `extract_audio` (ffmpeg libmp3lame) untuk fmt_key == "mp3".
6. Nama file Bunkr ter-encode ganda (UTF-8 bytes dibaca sebagai Latin-1)
   -> `_fix_name` mengembalikan ke UTF-8, mis. `çèå...cos(31).mp4`
   menjadi `王者原神星铁cos(31).mp4`.
7. Parsing HTML WAJIB dari `r.content.decode("utf-8")`, bukan `r.text`
   (Content-Type-nya `text/html` tanpa charset).
"""
import os
import re
import time
import html as html_mod
import logging
import asyncio
from urllib.parse import urljoin, urlsplit, quote, urlencode

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, progress_bar, FileSizeLimitExceeded
from handlers.dl.progress import (
    render_progress_text,
    edit_status,
    TransferStats,
    PROGRESS_EDIT_INTERVAL,
    PROGRESS_LOG_INTERVAL,
)

from .constants import (
    UA,
    _HTTP_TIMEOUT,
    SIGN_URL,
    BUNKR_PROGRESS_INTERVAL,
    _VIDEO_EXT,
    _IMAGE_EXT,
    BUNKR_ALBUM_CONCURRENCY,
)

log = logging.getLogger(__name__)


def _new_session():
    """Satu session per panggilan besar (curl_cffi Session tidak thread-safe)."""
    return curl_requests.Session(impersonate="chrome")


def _html(resp) -> str:
    """Dekode HTML selalu sebagai UTF-8 (Content-Type tanpa charset)."""
    return resp.content.decode("utf-8", "replace")


async def _safe_edit_status(bot, chat_id, status_msg_id, text):
    await edit_status(
        bot, chat_id, status_msg_id, text,
        min_interval=PROGRESS_EDIT_INTERVAL,
        label="Bunkr progress",
    )
    return 0.0


def _fix_name(name: str) -> str:
    """Bunkr menyajikan nama file ter-encode ganda (UTF-8 bytes dibaca Latin-1).

    Nama asli `王者原神星铁cos(31).mp4` sampai ke client sebagai mojibake
    `çèåæçæéèècos(31).mp4`. Kembalikan ke UTF-8 kalau memang bisa.
    """
    name = name or ""
    try:
        fixed = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    if fixed != name and "?" not in fixed:
        return fixed
    return name


def extension_for(name: str | None, default: str = ".mp4") -> str:
    """Ekstensi nama file, fallback `default`."""
    path = urlsplit(name or "").path.lower()
    for ext in _VIDEO_EXT + _IMAGE_EXT:
        if path.endswith(ext):
            return ext
    return default


def _item_type(it) -> str:
    for sp in it.find_all("span"):
        for c in (sp.get("class") or []):
            if c.startswith("type-"):
                return "video" if c[5:].lower() == "video" else "image"
    return "image"


def parse_album_html(html: str, base: str = "") -> list:
    """Ambil daftar item album dari HTML `/a/<id>`."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for it in soup.find_all(class_="theItem"):
        href = None
        for a in it.find_all("a", href=True):
            if "/f/" in a["href"]:
                href = a["href"]
                break
        if not href:
            continue
        name_el = it.find(class_="theName")
        size_el = it.find(class_="theSize")
        thumb_el = it.find("img", class_="grid-images_box-img")
        name = name_el.get_text(strip=True) if name_el else (it.get("title") or "")
        out.append({
            "page": href if href.startswith("http") else base + href,
            "name": _fix_name(name),
            "type": _item_type(it),
            "size_text": size_el.get_text(strip=True) if size_el else "",
            "thumb": (thumb_el.get("src") if thumb_el else "") or "",
        })
    return out


def scrape_album(url: str) -> dict:
    """-> {title, items:[{page,name,type,size_text,thumb}], url}"""
    sess = _new_session()
    try:
        r = sess.get(url, headers={"User-Agent": UA}, timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"Bunkr album HTTP {r.status_code}")
        text = _html(r)
        base = "{0.scheme}://{0.netloc}".format(urlsplit(url))
        soup = BeautifulSoup(text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else "Bunkr"
        title = re.sub(r"\s*\|\s*Bunkr\s*$", "", title).strip() or "Bunkr"
        items = parse_album_html(text, base)
        if not items:
            raise RuntimeError("Album Bunkr tidak berisi media")
        return {"title": _fix_name(title), "items": items, "url": url}
    finally:
        sess.close()


def resolve_file(page_url: str, *, retries: int = 3) -> dict:
    """Halaman `/f/<id>` -> URL CDN bertanda-tangan + nama file asli.

    Lihat FLOW SCRAPER langkah 2 untuk rantai lengkapnya. Blocking ringan
    (2-3 request HTTP, ~1s) — panggil dari `asyncio.to_thread` kalau dipakai
    dari coroutine.
    """
    sess = _new_session()
    last_err = None
    try:
        for attempt in range(retries):
            try:
                r = sess.get(page_url, headers={"User-Agent": UA}, timeout=_HTTP_TIMEOUT)
                if r.status_code != 200:
                    raise RuntimeError(f"halaman file HTTP {r.status_code}")
                text = _html(r)
                soup = BeautifulSoup(text, "html.parser")
                btn = soup.find(id="download-btn")
                # Origin API = halaman download (`dl.<host>`), bukan halaman /f/.
                api_origin = "{0.scheme}://{0.netloc}".format(urlsplit(r.url))

                # Kalau halaman /f/ hanya berisi link ke halaman download,
                # ikuti dulu sebelum mencari data-id.
                if btn is None:
                    dl = next(
                        (a["href"] for a in soup.find_all("a", href=True)
                         if re.search(r"/file/\d+", a["href"])),
                        None,
                    )
                    if dl is None:
                        raise RuntimeError("link halaman download tidak ada")
                    r = sess.get(
                        urljoin(page_url, dl),
                        headers={"User-Agent": UA, "Referer": page_url},
                        timeout=_HTTP_TIMEOUT,
                    )
                    if r.status_code != 200:
                        raise RuntimeError(f"halaman download HTTP {r.status_code}")
                    text = _html(r)
                    soup = BeautifulSoup(text, "html.parser")
                    btn = soup.find(id="download-btn")
                    api_origin = "{0.scheme}://{0.netloc}".format(urlsplit(r.url))

                data_id = btn.get("data-id") if btn is not None else None
                if not data_id:
                    raise RuntimeError("download-btn data-id tidak ada")

                m_name = re.search(r'ogname\s*=\s*"([^"]*)"', text)
                ogname = m_name.group(1) if m_name else ""

                api = sess.post(
                    api_origin + "/api/_001_v2",
                    headers={
                        "User-Agent": UA,
                        "Referer": page_url,
                        "Content-Type": "application/json",
                    },
                    json={"id": data_id},
                    timeout=_HTTP_TIMEOUT,
                )
                if api.status_code != 200:
                    raise RuntimeError(f"api/_001_v2 HTTP {api.status_code}")
                meta = api.json()
                mediafiles = (meta.get("mediafiles") or "").rstrip("/")
                path = meta.get("path") or ""
                if not mediafiles or not path:
                    raise RuntimeError(f"meta tidak lengkap: {meta!r}")

                sign = sess.get(
                    SIGN_URL + "?path=" + quote(path),
                    headers={"User-Agent": UA},
                    timeout=_HTTP_TIMEOUT,
                )
                if sign.status_code != 200:
                    raise RuntimeError(f"sign HTTP {sign.status_code}")
                sd = sign.json()
                params = {"token": sd.get("token", ""), "ex": sd.get("ex", "")}
                original = meta.get("original") or ogname
                if original:
                    params["n"] = _fix_name(original)
                return {
                    "url": mediafiles + path + "?" + urlencode(params),
                    "name": _fix_name(original or ogname),
                    "referer": page_url,
                    "data_id": data_id,
                }
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"resolve Bunkr gagal: {last_err!r}")
    finally:
        sess.close()


def _content_length(resp) -> int:
    try:
        return int(resp.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        return 0


async def download_to_file(
    url: str,
    out_path: str,
    bot,
    chat_id,
    status_msg_id,
    title: str,
    notify: bool = True,
    referer: str = "",
) -> int:
    """Stream file Bunkr ke `out_path` dengan progress + limit 2GB.

    `notify=False` dipakai jalur album: unduh senyap supaya tidak membanjiri
    pesan status. Progress `TransferStats` memakai jendela 1s (bukan delta
    antar-chunk) supaya speed yang tampil adalah throughput riil.
    """
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer

    def _get():
        resp = curl_requests.get(
            url, headers=headers, impersonate="chrome",
            timeout=_HTTP_TIMEOUT, stream=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Gagal mengunduh Bunkr ({resp.status_code})")
        return resp

    resp = await asyncio.to_thread(_get)
    total = _content_length(resp)
    if total > MAX_TG_SIZE:
        resp.close()
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total / 1024 / 1024 / 1024:.2f} GB). Download canceled."
        )

    status = {"downloaded": 0}
    stats = TransferStats(total)

    def _write():
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                status["downloaded"] += len(chunk)
                if status["downloaded"] > MAX_TG_SIZE:
                    resp.close()
                    raise FileSizeLimitExceeded("File exceeds 2GB limit. Download canceled.")
        try:
            resp.close()
        except Exception:
            pass

    if notify and status_msg_id:
        await _safe_edit_status(
            bot, chat_id, status_msg_id,
            render_progress_text(sanitize_filename(title, 80), downloaded=0, total=total),
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    try:
        while True:
            await asyncio.sleep(0.7)
            downloaded = status["downloaded"]
            if downloaded > 0:
                stats.sample(downloaded)
                await stats.emit(
                    bot=bot if (notify and status_msg_id) else None,
                    chat_id=chat_id,
                    status_msg_id=status_msg_id,
                    title=sanitize_filename(title, 80),
                    kind="Bunkr download",
                    label=os.path.basename(out_path),
                    log_interval=PROGRESS_LOG_INTERVAL,
                    edit_interval=BUNKR_PROGRESS_INTERVAL,
                )
            if write_task.done():
                break
        exc = write_task.exception()
        if exc:
            raise exc
    finally:
        if not write_task.done():
            write_task.cancel()

    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError("Gagal mengunduh file Bunkr (kosong)")

    downloaded = os.path.getsize(out_path)
    stats.sample(downloaded)
    stats.log_done("Bunkr download", label=os.path.basename(out_path), size=downloaded)
    if notify and status_msg_id:
        await stats.emit(
            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            title=sanitize_filename(title, 80),
            kind="Bunkr download",
            label=os.path.basename(out_path),
            log_interval=0.0, edit_interval=0.0,
        )
    return downloaded


def _album_interval(total: int) -> float:
    """Interval edit status album: 3s untuk sedikit, 5s untuk banyak."""
    if total <= 5:
        return 3.0
    if total >= 50:
        return 5.0
    return 3.0 + 2.0 * (total - 5) / 45.0


async def download_album(
    medias_list: list, title: str, bot, chat_id, status_msg_id,
    out_dir: str, tag: str = "bunkr", item_label: str = "media",
) -> dict:
    """Unduh semua media album, kembalikan `{items:[{path,type}], title}`.

    Resolve + unduh dijalankan **paralel** (Semaphore `BUNKR_ALBUM_CONCURRENCY`)
    supaya album besar tidak lagi memakan waktu ~2x dari yang perlu. Progres
    pesan status diperbarui saat tiap item selesai, tetap memakai
    `_album_interval(total)` agar aman dari rate limit.
    """
    medias = [m for m in (medias_list or []) if m.get("page")]
    total = len(medias)
    if not total:
        raise RuntimeError("Tidak ada media yang bisa diunduh di post ini")

    interval = _album_interval(total)
    escaped_title = html_mod.escape(sanitize_filename(title, 80))

    def _render(done_count: int) -> str:
        pct = (done_count * 100.0 / total) if total else 0.0
        return (
            f"<b>{escaped_title}</b>\n\n"
            f"<code>{progress_bar(pct)}</code>\n"
            f"<code>{done_count}/{total} {item_label}</code>"
        )

    if status_msg_id:
        wait = await _safe_edit_status(bot, chat_id, status_msg_id, _render(0))

    items: list = []
    failures = 0
    progress_lock = asyncio.Lock()
    state = {"last_edit": time.time(), "flood_until": 0.0}
    sem = asyncio.Semaphore(max(1, BUNKR_ALBUM_CONCURRENCY))

    async def _one(idx: int, item: dict):
        nonlocal failures
        async with sem:
            try:
                info = await asyncio.to_thread(resolve_file, item["page"])
            except Exception as e:
                failures += 1
                log.warning("Bunkr resolve gagal | %s -> %r", item.get("name"), e)
                return

            m_type = item.get("type") or (
                "video" if info["name"].lower().endswith(_VIDEO_EXT) else "image"
            )
            def_ext = ".mp4" if m_type == "video" else ".jpg"
            prefix = "vid" if m_type == "video" else "img"
            name = f"{tag}_{prefix}_{idx:03d}{extension_for(info['name'], def_ext)}"
            out = os.path.join(out_dir, name)

            try:
                await download_to_file(
                    info["url"], out, bot, chat_id, status_msg_id,
                    title, notify=False, referer=info.get("referer") or "",
                )
            except Exception as e:
                failures += 1
                log.warning("Bunkr download gagal | %s -> %r", info.get("name"), e)
                if os.path.exists(out):
                    try:
                        os.remove(out)
                    except OSError:
                        pass
                return

            async with progress_lock:
                items.append({"path": out, "type": m_type})
                now = time.time()
                done = len(items)
                if status_msg_id and now >= state["flood_until"] and (
                    done == total or now - state["last_edit"] >= interval
                ):
                    wait = await _safe_edit_status(
                        bot, chat_id, status_msg_id, _render(done)
                    )
                    state["last_edit"] = now
                    if wait:
                        state["flood_until"] = now + wait

    await asyncio.gather(*(_one(idx, item) for idx, item in enumerate(medias, 1)))

    if not items:
        raise RuntimeError(
            f"Semua media Bunkr gagal diunduh ({failures}/{total} gagal)"
        )
    if failures:
        log.warning("Bunkr album selesai dengan kegagalan | ok=%d gagal=%d", len(items), failures)
    return {"items": items, "title": sanitize_filename(title or "Bunkr", 100)}


def extract_audio(src_path: str, out_path: str) -> str:
    """Ekstrak trek audio (ffmpeg libmp3lame) -> out_path."""
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path,
    ]
    res = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=600,
    )
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg gagal: {(res.stderr or '').strip()[-400:]}")
    return out_path
