"""Downloader Anichin (anichin.moe) — Donghua Sub Indo.

FLOW DOWNLOADER
---------------
1. `is_anichin_url(url)`: host `anichin.moe` + path episode
   (`/<slug>-episode-<N>-subtitle-indonesia/`).
2. `probe_anichin(url)`: scrape halaman episode -> daftar mirror resolusi
   (dari `div.soraurlx`) -> `res_list` untuk resolution picker. Di-cache
   `_PROBE_CACHE` (TTL 5 menit) supaya callback resolusi memakai link yang sama.
3. `anichin_download(...)`:
   a. Ambil mirror sesuai `format_id` (label resolusi, mis. "720p").
   b. Resolve chain: shortener -> `mirrored.to/multilinks` ->
      `mirrored.to/files/<uid>/..._links` -> `/mirstats.php` -> POST `/out_url`
      -> link file host asli (GoogleDrive/Buzzheavier/...).
   c. Unduh:
      - GoogleDrive -> `_download_gdrive` (endpoint `drive.usercontent.google.com`).
      - Buzzheavier -> CDN langsung (`buzzheavier.com/<id>/download`).
      - host lain -> streaming `curl_cffi` biasa dengan Referer host.
   d. `fmt_key == "mp3"` -> ekstrak audio ffmpeg `libmp3lame -q:a 2`.
4. Semua langkah MURNI HTTP (curl_cffi). TIDAK memakai yt-dlp.
"""
import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from urllib.parse import urlparse

from handlers.dl.constants import MAX_TG_SIZE, TMP_DIR
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename

from .constants import ANICHIN_HOSTS, PROGRESS_INTERVAL, UA
from . import extractor

log = logging.getLogger(__name__)

_PROBE_CACHE: dict = {}
_CACHE_TTL = 300.0


def is_anichin_url(url: str) -> bool:
    """Halaman episode Anichin: host anichin.moe + slug episode.

    Halaman series (`/<slug>/`) juga diterima (diunduh lewat episode pertama).
    """
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in tuple(h.lstrip("www.") for h in ANICHIN_HOSTS):
        return False
    path = (parsed.path or "").strip("/")
    if not path:
        return False
    # Buang halaman non-video.
    if path.split("/")[0] in ("anime", "ongoing", "completed", "upcoming-donghua", "drop",
                              "genres", "az-lists", "schedule", "bookmark", "blog",
                              "privacy-policy-2", "riwayat-menonton", "anichin-vip", "anichin-cafe"):
        return False
    return True


def _prune_cache() -> None:
    now = time.time()
    for key in [k for k, v in list(_PROBE_CACHE.items()) if now - v.get("ts", 0) > _CACHE_TTL]:
        _PROBE_CACHE.pop(key, None)


def cache_probe(url: str, probe: dict) -> None:
    _prune_cache()
    _PROBE_CACHE[(url or "").strip()] = {"ts": time.time(), "probe": probe}


def peek_probe(url: str) -> dict | None:
    item = _PROBE_CACHE.get((url or "").strip())
    if not item:
        return None
    if time.time() - item.get("ts", 0) > _CACHE_TTL:
        _PROBE_CACHE.pop((url or "").strip(), None)
        return None
    return item.get("probe")


def _scrape_series_latest_episode(url: str) -> dict | None:
    """Halaman series -> episode TERBARU (`div.eplister ul li` pertama).

    Dipakai `probe_anichin` supaya URL series tidak berakhir
    "No valid resolutions available". -> {episode_url, title}
    """
    import re as _re
    from bs4 import BeautifulSoup

    code, raw, _ = extractor._get(url)
    if code != 200 or not raw:
        return None
    soup = BeautifulSoup(raw, "html.parser")
    ul = soup.select_one("div.eplister ul")
    if not ul:
        return None
    li = ul.select_one("li")
    if not li:
        return None
    a = li.select_one("a[href]")
    if not a:
        return None
    href = a["href"].strip()
    if href.startswith("/"):
        href = "https://anichin.moe" + href
    title = li.select_one("div.epl-title")
    return {
        "episode_url": href,
        "title": _re.sub(r"\s+", " ", title.get_text(" ", strip=True)) if title else "",
    }


def probe_anichin(raw_url: str) -> dict:
    """-> {title, thumbnail, mirrors, res_list}"""
    url = (raw_url or "").strip()
    cached = peek_probe(url)
    if cached:
        return cached

    post = extractor.scrape_post(url)
    mirrors = post.get("mirrors") or []

    # Kalau user menempel URL SERIES (`/<slug>/`), halaman itu cuma daftar
    # episode -> tidak ada mirror. Ambil episode TERBARU lalu probe itu,
    # supaya "No valid resolutions available" tidak muncul untuk URL series.
    if not mirrors:
        try:
            detail = _scrape_series_latest_episode(url)
        except Exception as e:
            log.debug("Anichin series fallback gagal | url=%s err=%r", url, e)
            detail = None
        if detail and detail.get("episode_url"):
            ep_url = detail["episode_url"]
            log.info("Anichin series URL -> latest episode | from=%s to=%s", url, ep_url)
            ep_post = extractor.scrape_post(ep_url)
            mirrors = ep_post.get("mirrors") or []
            if mirrors:
                post = ep_post
                url = ep_url

    # Server streaming — jalur cadangan paling andal (OK.ru, lalu VidHide/
    # StreamWish). Di-resolve di sini supaya picker bisa memakai tinggi nyata dan
    # download worker tidak fetch ulang.
    stream = {}
    try:
        stream = extractor.resolve_stream_source(post.get("servers") or [], url) or {}
    except Exception as e:
        log.debug("Anichin server stream probe gagal | url=%s err=%r", url, e)

    # Halaman tanpa mirror resolusi TIDAK langsung dianggap gagal: selama ada
    # server streaming, halaman masih bisa diunduh (kualitas server yang mengisi
    # picker). Ini yang mencegah "No valid resolutions available."
    if not mirrors and not (stream.get("variants") or stream.get("segments")):
        raise RuntimeError(
            "Tidak ada link unduh (mirror) di halaman Anichin ini. "
            "Kalau ini halaman series, buka salah satu episode-nya."
        )

    res_list = []
    seen = set()
    for m in mirrors:
        label = (m.get("label") or "").strip()
        low = label.lower()

        # Label "Multi": shortener-nya langsung menunjuk `mirrored.to/multilinks/<hash>`
        # yang berisi BEBERAPA resolusi (1080p/720p/480p/360p). Expand jadi
        # resolusi nyata supaya picker terisi; kalau gagal, fallback ke label asli.
        if low == "multi":
            try:
                expanded = extractor.list_resolutions(m.get("url") or "", referer=url)
            except Exception as e:
                log.debug("Anichin expand Multi gagal | url=%s err=%r", m.get("url"), e)
                expanded = []
            if expanded:
                for r in expanded:
                    key = (r.get("format_id") or "").lower()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    res_list.append({
                        "height": r.get("height") or 480,
                        "format_id": r.get("format_id") or r.get("label") or "480p",
                        "label": r.get("label") or r.get("format_id") or "480p",
                        "has_audio": True,
                        "filesize": 0,
                        "total_size": 0,
                        "mirror_url": m.get("url"),
                    })
                continue

        # "VIP" bukan resolusi (link Telegram promosi).
        if low in ("vip", ""):
            continue
        # "4K" tidak punya digit tinggi -> map manual.
        if low in ("4k", "2160p"):
            height = 2160
        else:
            hm = re.search(r"(\d{3,4})", label)
            height = int(hm.group(1)) if hm else 0
        if not height:
            continue
        key = low
        if key in seen:
            continue
        seen.add(key)
        res_list.append({
            "height": height,
            "format_id": label,
            "label": label,
            "has_audio": True,
            "filesize": 0,
            "total_size": 0,
            "mirror_url": m.get("url"),
        })

    res_list.sort(key=lambda x: x.get("height") or 0, reverse=True)

    # Kalau halaman tidak punya mirror resolusi sama sekali, tawarkan kualitas
    # server streaming supaya picker tidak kosong ("No valid resolutions
    # available"). Kalau mirror ADA, server TIDAK ditambahkan ke picker (akan
    # membanjiri daftar dengan tinggi "nyata" yang berbeda dari label nominal) —
    # server tetap dipakai otomatis sebagai fallback saat semua host mirror gagal.
    if not res_list and stream.get("variants"):
        for v in reversed(stream["variants"]):
            h = v.get("height") or 0
            if h <= 0:
                continue
            fid = str(h)
            if fid in seen:
                continue
            seen.add(fid)
            res_list.append({
                "height": h,
                "format_id": fid,
                "label": f"{h}p",
                "has_audio": True,
                "filesize": 0,
                "total_size": 0,
                "mirror_url": stream.get("embed"),
                "server": "okru",
            })
        res_list.sort(key=lambda x: x.get("height") or 0, reverse=True)
    elif not res_list and stream.get("segments"):
        # Hanya ada jalur HLS (VidHide/StreamWish): satu opsi di tinggi tertinggi.
        h = stream.get("height") or 0
        res_list.append({
            "height": h or 720,
            "format_id": str(h or 720),
            "label": f"{h}p" if h else "Auto",
            "has_audio": True,
            "filesize": 0,
            "total_size": 0,
            "mirror_url": stream.get("embed"),
            "server": "hls",
        })

    probe = {
        "title": post.get("title") or "Anichin Video",
        "thumbnail": post.get("thumbnail") or "",
        "mirrors": mirrors,
        "servers": post.get("servers") or [],
        "res_list": res_list,
        "stream": stream,
        # Kompatibilitas nama lama.
        "okru": stream if stream.get("kind") == "okru" else {},
    }
    log.info(
        "Anichin probe | title=%r mirrors=%s res=%s stream=%s/%s servers=%d",
        probe["title"], len(mirrors), [r["format_id"] for r in res_list],
        stream.get("kind") or "-", stream.get("label") or "-",
        len(probe["servers"]),
    )
    cache_probe(url, probe)
    # Kalau URL asli adalah halaman series, simpan juga di key URL series supaya
    # probe kedua tidak resolve ulang (download worker memakai `raw_url` asli).
    if (raw_url or "").strip() != url:
        cache_probe((raw_url or "").strip(), probe)
    return probe


def _gdrive_id(url: str) -> str:
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def _download_gdrive(gid: str, out_path: str) -> int:
    """Unduh dari Google Drive (endpoint `drive.usercontent.google.com`)."""
    import curl_cffi.requests as curl_requests

    h = {"User-Agent": UA}
    url = f"https://drive.usercontent.google.com/download?id={gid}&export=download"
    r = curl_requests.get(url, headers=h, impersonate="chrome", timeout=120, stream=True)
    ct = (r.headers.get("content-type") or "").lower()

    if "text/html" in ct:
        text = r.content.decode("utf-8", "ignore")
        if "quota" in text.lower():
            raise RuntimeError("Kuota Google Drive terlampaui.")
        m = re.search(r'name="confirm"\s+value="([^"]+)"', text)
        if not m:
            m = re.search(r'href="(/uc\?export=download[^"]*confirm=[^"]+)"', text)
            if m:
                r = curl_requests.get(
                    "https://drive.google.com" + m.group(1).replace("&amp;", "&"),
                    headers=h, impersonate="chrome", timeout=120, stream=True,
                )
            else:
                raise RuntimeError("Google Drive butuh konfirmasi yang tidak dikenali.")
        else:
            r = curl_requests.get(
                "https://drive.usercontent.google.com/download",
                params={"id": gid, "export": "download", "confirm": m.group(1)},
                headers=h, impersonate="chrome", timeout=120, stream=True,
            )
            ct = (r.headers.get("content-type") or "").lower()

    if r.status_code != 200 or "html" in ct:
        raise RuntimeError(f"Google Drive tidak mengembalikan video ({r.status_code}, {ct or 'no ct'}).")

    total = 0
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                total += len(chunk)
                if total > MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("File melebihi batas 2GB.")
    return total


def _download_stream(url: str, out_path: str, referer: str = "") -> int:
    """Streaming HTTP biasa (host selain Google Drive)."""
    import curl_cffi.requests as curl_requests

    with_headers = {"User-Agent": UA}
    if referer:
        # Nama file mirrored.to bisa memuat karakter Unicode (mis. `Ⅴ` U+2164);
        # header Referer WAJIB ASCII/latin-1 atau curl_cffi melempar
        # UnicodeEncodeError yang menyesatkan.
        with_headers["Referer"] = extractor._header_safe(referer)
    r = curl_requests.get(url, headers=with_headers, impersonate="chrome", timeout=120, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"Host file membalas HTTP {r.status_code}.")
    ct = (r.headers.get("content-type") or "").lower()
    if "html" in ct:
        raise RuntimeError(f"Host file mengembalikan HTML, bukan video ({ct}).")
    total = 0
    try:
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=512 * 1024):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
                    if total > MAX_TG_SIZE:
                        raise FileSizeLimitExceeded("File melebihi batas 2GB.")
    finally:
        try:
            r.close()
        except Exception:
            pass
    return total


def _est_bytes(duration, bandwidth) -> int:
    """Estimasi total byte dari `duration × bandwidth / 8` (aman terhadap tipe)."""
    try:
        dur = float(duration or 0)
        bw = float(bandwidth or 0)
    except (TypeError, ValueError):
        return 0
    if dur <= 0 or bw <= 0:
        return 0
    return int(dur * bw / 8)


async def _download_segments_parallel(
    segments: list[str],
    referer: str,
    out_path: str,
    *,
    bot=None, chat_id=None, status_msg_id=None,
    title: str = "Anichin", notify: bool = False,
    est_total: int = 0,
    concurrency: int = 12,
) -> int:
    """Unduh segmen `.ts` OK.ru paralel lalu concat ke satu MP4.

    Jauh lebih cepat dari ffmpeg serial (~1.1 MB/s): 12 koneksi paralel pada
    CDN OK.ru. Tiap segmen ditulis ke file sendiri (bukan ditahan di RAM) supaya
    episode besar tidak menghabiskan memori; concat byte-level (semua segmen
    MPEG-TS) lalu ffmpeg `-c copy -movflags +faststart` -> MP4 faststart.
    """
    import curl_cffi.requests as curl_requests
    from handlers.dl.progress import TransferStats

    total_segs = len(segments)
    stats = TransferStats(max(0, int(est_total or 0)))
    work_dir = out_path + ".parts"
    os.makedirs(work_dir, exist_ok=True)
    lock = asyncio.Lock()
    done_bytes = 0
    sem = asyncio.Semaphore(max(1, int(concurrency or 12)))

    def _fetch(idx: int, url: str) -> int:
        seg_path = os.path.join(work_dir, f"{idx:06d}.ts")
        headers = {"User-Agent": UA}
        if referer:
            headers["Referer"] = referer
        r = curl_requests.get(url, headers=headers, impersonate="chrome",
                              timeout=120, stream=True)
        try:
            if r.status_code not in (200, 206):
                raise RuntimeError(f"Segmen {idx} balas HTTP {r.status_code}.")
            n = 0
            with open(seg_path, "wb") as f:
                for ch in r.iter_content(chunk_size=256 * 1024):
                    if ch:
                        f.write(ch)
                        n += len(ch)
            return n
        finally:
            try:
                r.close()
            except Exception:
                pass

    async def _worker(idx: int, url: str):
        nonlocal done_bytes
        async with sem:
            size = await asyncio.to_thread(_fetch, idx, url)
        async with lock:
            done_bytes += size
            stats.sample(done_bytes)
            over = done_bytes > MAX_TG_SIZE
        if over:
            raise FileSizeLimitExceeded("File melebihi batas 2GB.")
        if notify and status_msg_id:
            async with lock:
                await stats.emit(
                    bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                    title=title, kind="Anichin download", label="Anichin",
                    log_interval=PROGRESS_INTERVAL, edit_interval=PROGRESS_INTERVAL,
                )

    tasks = [asyncio.create_task(_worker(i, u)) for i, u in enumerate(segments)]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        for t in tasks:
            t.cancel()
        raise

    # Buat daftar segmen untuk concat demuxer.
    concat_list = os.path.join(work_dir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for i in range(total_segs):
            seg_path = os.path.join(work_dir, f"{i:06d}.ts")
            if not os.path.exists(seg_path) or os.path.getsize(seg_path) == 0:
                raise RuntimeError(f"Segmen {i} tidak lengkap.")
            safe = os.path.abspath(seg_path).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    stats.log_done("Anichin download", label="Anichin")

    # Jalur 1: concat demuxer (-f concat -safe 0). Lebih hemat I/O (tidak membuat
    # file perantara 600MB) dan menangani timestamp tanpa desync. `-bsf:a aac_adtstoasc`
    # wajib saat muxing stream AAC dari MPEG-TS ke container MP4.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart", out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    # Jika jalur 1 gagal, fallback ke jalur 2 (byte-concat ke .ts lalu remux).
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        err1 = (stderr or b"").decode("utf-8", "replace").strip()[-300:]
        log.warning("ffmpeg -f concat gagal (exit=%s: %s), coba byte-concat", proc.returncode, err1)
        concat_ts = out_path + ".ts"
        try:
            with open(concat_ts, "wb") as out:
                for i in range(total_segs):
                    seg_path = os.path.join(work_dir, f"{i:06d}.ts")
                    with open(seg_path, "rb") as src:
                        shutil.copyfileobj(src, out, 1024 * 1024)
            proc2 = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "warning", "-i", concat_ts,
                "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr2 = await proc2.communicate()
            if proc2.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                err2 = (stderr2 or b"").decode("utf-8", "replace").strip()[-300:]
                raise RuntimeError(f"ffmpeg concat gagal (exit={proc2.returncode}): {err2 or err1 or 'output kosong'}")
        finally:
            if os.path.exists(concat_ts):
                try:
                    os.remove(concat_ts)
                except OSError:
                    pass

    shutil.rmtree(work_dir, ignore_errors=True)
    return os.path.getsize(out_path)


async def _run_ffmpeg_hls(playlist_url: str, referer: str, out_path: str,
                           bot=None, chat_id=None, status_msg_id=None,
                           title: str = "Anichin", notify: bool = False,
                           est_total: int = 0) -> None:
    """Unduh HLS via ffmpeg (`-c copy` + faststart).

    Progress dipantau dari ukuran file keluaran (ffmpeg menulis inkremental)
    supaya UI Telegram tetap bergerak; `-loglevel error` menjaga stderr bersih
    untuk pesan error. `est_total` = estimasi total byte (`duration × bandwidth/8`)
    supaya bar punya penyebut.
    """
    from handlers.dl.progress import TransferStats

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-user_agent", UA,
        "-headers", f"Referer: {referer or playlist_url}\r\n",
        "-i", playlist_url,
        "-c", "copy", "-movflags", "+faststart",
        out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    stats = TransferStats(max(0, int(est_total or 0)))

    async def _drain():
        # `communicate()` menangani pembacaan stderr sekaligus menunggu proses,
        # sehingga tidak ada race antara `stderr.read()` dan proses yang sudah
        # keluar (yang bisa memblokir selamanya).
        out, err = await proc.communicate()
        return err or b""

    drain_task = asyncio.create_task(_drain())
    while proc.returncode is None:
        if os.path.exists(out_path):
            try:
                cur = os.path.getsize(out_path)
            except OSError:
                cur = 0
            stats.sample(cur)
            if cur > MAX_TG_SIZE:
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            if notify and status_msg_id:
                await stats.emit(
                    bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                    title=title, kind="Anichin download", label="Anichin",
                    log_interval=PROGRESS_INTERVAL, edit_interval=PROGRESS_INTERVAL,
                )
        await asyncio.sleep(0.5)

    err_buf = b""
    try:
        err_buf = await drain_task
    except asyncio.CancelledError:
        pass

    if proc.returncode not in (0, None) or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        msg = (err_buf or b"").decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"ffmpeg HLS gagal: {msg}")
    if os.path.getsize(out_path) > MAX_TG_SIZE:
        raise FileSizeLimitExceeded("File melebihi batas 2GB.")
    stats.log_done("Anichin download", label="Anichin")


async def anichin_server(
    raw_url: str,
    fmt_key: str,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
    engine: str | None = None,
):
    """Unduh dari server streaming Anichin (OK.ru) — jalur paling andal.

    Dipakai `anichin_download` sebagai fallback setelah semua host mirror gagal,
    dan oleh resolution picker (engine `anichin_server`) supaya user bisa memilih
    server streaming secara eksplisit.
    """
    del has_audio, known_size, engine
    url = (raw_url or "").strip()

    probe = peek_probe(url)
    if not probe:
        probe = await asyncio.to_thread(probe_anichin, url)
        cache_probe(url, probe)

    servers = probe.get("servers") or []
    if not servers:
        raise RuntimeError("Tidak ada server streaming di halaman ini.")

    if not metadata_ready and status_msg_id:
        from handlers.dl.progress import edit_status
        await edit_status(bot, chat_id, status_msg_id,
                          "<b>Resolving stream...</b>", label="Anichin")

    info = probe.get("stream") or {}
    if not (info.get("variants") or info.get("segments")):
        info = await asyncio.to_thread(extractor.resolve_stream_source, servers, url)
    if not info:
        raise RuntimeError("Tidak ada server streaming yang bisa diunduh (OK.ru/StreamWish tidak tersedia).")

    kind = info.get("kind") or "okru"
    want = 0
    m = re.search(r"(\d+)", str(format_id or ""))
    if m:
        want = int(m.group(1))

    # Siapkan daftar segmen + tinggi tergantung jenis sumber.
    segments: list[str] = []
    height = 0
    width = 0
    est_total = 0
    referer = info.get("referer") or "https://ok.ru/"
    if kind == "okru":
        variant = extractor.pick_okru_variant(info.get("variants") or [], want)
        if not variant:
            raise RuntimeError("Varian OK.ru tidak ditemukan.")
        height = variant.get("height") or 0
        width = variant.get("width") or 0
        est_total = _est_bytes(info.get("duration"), variant.get("bandwidth"))
        try:
            got = await asyncio.to_thread(
                extractor.okru_segments, variant, info.get("hls_master") or ""
            )
        except Exception as e:
            log.warning("Anichin OK.ru playlist segmen gagal | err=%r", e)
            got = {}
        segments = got.get("segments") or []
        if not segments:
            # Fallback: ffmpeg serial pada master playlist.
            variant_url = variant.get("url") or ""
            if not variant_url:
                raise RuntimeError("URL varian OK.ru tidak ditemukan.")
            info = dict(info, _fallback_url=variant_url)
    else:
        segments = info.get("segments") or []
        height = info.get("height") or 0
        est_total = _est_bytes(info.get("duration"), info.get("bandwidth"))

    title = probe.get("title") or info.get("title") or "Anichin Video"
    work_dir = os.path.join(TMP_DIR, f"ac_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    temp_video = os.path.join(work_dir, f"{uuid.uuid4().hex[:8]}.mp4")

    try:
        log.info(
            "Anichin server start | title=%r kind=%s quality=%s %sx%s segs=%d",
            title, kind, info.get("label"), width, height, len(segments),
        )
        if not metadata_ready and status_msg_id:
            from handlers.dl.progress import edit_status
            await edit_status(bot, chat_id, status_msg_id,
                              "<b>Downloading...</b>", label="Anichin")

        if segments:
            await _download_segments_parallel(
                segments, referer, temp_video,
                bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                title=title, notify=bool(status_msg_id), est_total=est_total,
            )
        elif info.get("_fallback_url"):
            await _run_ffmpeg_hls(
                info["_fallback_url"], referer, temp_video,
                bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                title=title, notify=bool(status_msg_id), est_total=est_total,
            )
        else:
            raise RuntimeError("Tidak ada segmen video yang bisa diunduh.")

        if not os.path.exists(temp_video) or os.path.getsize(temp_video) == 0:
            raise RuntimeError("File hasil unduhan server kosong.")
        log.info("Anichin server sukses | title=%r kind=%s", title, kind)

        if fmt_key == "mp3":
            mp3_path = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp3")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", temp_video,
                "-vn", "-c:a", "libmp3lame", "-q:a", "2", mp3_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0 or not os.path.exists(mp3_path):
                raise RuntimeError("Gagal mengekstrak MP3 dari video server.")
            return {
                "path": mp3_path,
                "title": title,
                "duration": info.get("duration") or 0,
                "thumb": probe.get("thumbnail") or info.get("poster") or "",
                "artist": "Anichin",
            }

        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(temp_video, out_video)
        return {
            "path": out_video,
            "title": title,
            "duration": info.get("duration") or 0,
            "thumb": probe.get("thumbnail") or info.get("poster") or "",
            "width": width or 1280,
            "height": height or 720,
            # ffmpeg sudah menulis MP4 faststart -> lewati remux kedua di pipeline.
            "remux_done": True,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def anichin_download(
    raw_url: str,
    fmt_key: str,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
    engine: str | None = None,
):
    """Unduh episode Anichin. Lihat FLOW DOWNLOADER di docstring modul."""
    del has_audio, known_size
    url = (raw_url or "").strip()

    # Engine eksplisit `anichin_server` -> langsung jalur server streaming (OK.ru).
    if (engine or "") == "anichin_server":
        return await anichin_server(
            raw_url, fmt_key, bot, chat_id, status_msg_id,
            format_id=format_id, metadata_ready=metadata_ready,
        )

    probe = peek_probe(url)
    if not probe:
        probe = await asyncio.to_thread(probe_anichin, url)
        cache_probe(url, probe)

    title = probe.get("title") or "Anichin Video"
    mirrors = probe.get("mirrors") or []
    stream = probe.get("stream") or {}
    if not mirrors and not (stream.get("variants") or stream.get("segments")):
        raise RuntimeError("Tidak ada link unduh di halaman ini.")

    want = (format_id or "").strip().lower()
    # Halaman tanpa mirror tapi punya server streaming: langsung ke jalur server.
    if not mirrors:
        log.info("Anichin tanpa mirror -> langsung server streaming | url=%s", url)
        return await anichin_server(
            raw_url, fmt_key, bot, chat_id, status_msg_id,
            format_id=format_id, metadata_ready=metadata_ready,
        )
    # Ambil SEMUA provider untuk resolusi yang dipilih (mis. `Mirrored` +
    # `Terabox`). Kalau provider pertama butuh captcha, provider kedua dicoba.
    # Cocokkan toleran: format_id bisa "1080p" (label soraurlx) atau "1080"
    # (tinggi OK.ru) sementara label mirror bisa "1080p" / "Multi".
    targets = [m for m in mirrors if (m.get("label") or "").strip().lower() == want]
    if not targets:
        wn = extractor.label_height(want)
        if wn:
            targets = [m for m in mirrors if extractor.label_height(m.get("label")) == wn]
    if not targets:
        targets = [m for m in mirrors if (m.get("label") or "").strip().lower() == "multi"]
    if not targets:
        targets = [mirrors[0]]
    target = targets[0]

    if not metadata_ready and status_msg_id:
        from handlers.dl.progress import edit_status
        await edit_status(
            bot, chat_id, status_msg_id,
            f"<b>Resolving {target.get('label') or 'video'}...</b>", label="Anichin",
        )

    # Kumpulkan kandidat dari SEMUA provider pada resolusi ini (mis. `Mirrored`,
    # `Pixeldrain`, `Terabox`), bukan berhenti di provider pertama yang berhasil
    # di-resolve. Provider pertama bisa resolve tapi gagal saat UNDUH; kandidat
    # dari provider lain jadi fallback.
    candidates = []
    captcha_blocked = []
    seen_cand = set()
    for tgt in targets:
        got = await asyncio.to_thread(
            extractor.resolve_provider, tgt.get("url"), tgt.get("label") or "", url
        )
        if got:
            for c in got:
                key = (c.get("host"), c.get("url"))
                if key in seen_cand:
                    continue
                seen_cand.add(key)
                candidates.append(c)
        elif extractor.is_captcha_shortener(tgt.get("url") or ""):
            captcha_blocked.append(tgt.get("name") or tgt.get("url") or "")

    if not candidates:
        # Fallback: server streaming (OK.ru / VidHide-StreamWish) — tanpa captcha.
        if stream.get("variants") or stream.get("segments"):
            reason = "semua provider captcha" if captcha_blocked else "mirror kosong"
            log.info(
                "Anichin %s -> fallback server streaming | url=%s kind=%s captcha=%s",
                reason, url, stream.get("kind"), sorted(set(captcha_blocked)),
            )
            return await anichin_server(
                raw_url, fmt_key, bot, chat_id, status_msg_id,
                format_id=format_id, metadata_ready=metadata_ready,
            )
        if captcha_blocked:
            raise RuntimeError(
                "Link unduh dilindungi captcha ("
                + ", ".join(sorted(set(captcha_blocked)))
                + "). Coba resolusi lain atau buka di browser."
            )
        raise RuntimeError(
            "Gagal me-resolve link unduh (shortener/mirror mati). "
            "Coba resolusi lain."
        )

    log.info(
        "Anichin candidates | title=%r label=%s providers=%s hosts=%s",
        title, target.get("label"),
        sorted({t.get("name") or "" for t in targets}),
        [c.get("host") for c in candidates],
    )

    work_dir = os.path.join(TMP_DIR, f"ac_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    temp_video = os.path.join(work_dir, f"{uuid.uuid4().hex[:8]}.mp4")

    try:
        last_err = None
        for cand in candidates:
            file_url = cand["url"]
            host = cand.get("host") or ""
            try:
                if "drive.google" in file_url:
                    gid = _gdrive_id(file_url)
                    if not gid:
                        raise RuntimeError("Google Drive id tidak ditemukan.")
                    await asyncio.to_thread(_download_gdrive, gid, temp_video)
                else:
                    direct = await asyncio.to_thread(
                        extractor.resolve_direct_file, file_url, cand.get("files_url") or url
                    )
                    await asyncio.to_thread(_download_stream, direct, temp_video, cand.get("files_url") or url)

                if not os.path.exists(temp_video) or os.path.getsize(temp_video) == 0:
                    raise RuntimeError("File hasil unduhan kosong.")
                log.info("Anichin host sukses | host=%s label=%s", host, cand.get("label"))
                break
            except Exception as e:
                last_err = e
                log.warning("Anichin host gagal | host=%s err=%r", host, e)
                if os.path.exists(temp_video):
                    try:
                        os.remove(temp_video)
                    except OSError:
                        pass
                continue
        else:
            # Semua host mirror gagal -> coba server streaming (OK.ru/VidHide).
            if stream.get("variants") or stream.get("segments"):
                log.info(
                    "Anichin semua host mirror gagal -> fallback server streaming | url=%s kind=%s",
                    url, stream.get("kind"),
                )
                shutil.rmtree(work_dir, ignore_errors=True)
                return await anichin_server(
                    raw_url, fmt_key, bot, chat_id, status_msg_id,
                    format_id=format_id, metadata_ready=metadata_ready,
                )
            raise RuntimeError(f"Semua host gagal. Terakhir: {last_err}")

        label = target.get("label") or "video"
        if fmt_key == "mp3":
            mp3_path = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp3")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", temp_video,
                "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                mp3_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0 or not os.path.exists(mp3_path):
                raise RuntimeError("Gagal mengekstrak MP3 dari video.")
            return {
                "path": mp3_path,
                "title": title,
                "duration": 0,
                "thumb": probe.get("thumbnail"),
                "artist": "Anichin",
            }

        out_video = os.path.join(TMP_DIR, f"{sanitize_filename(title, 80)}.mp4")
        shutil.move(temp_video, out_video)
        hm = re.search(r"(\d+)", label)
        height = int(hm.group(1)) if hm else 480
        width = 1920 if height >= 1080 else (1280 if height >= 720 else (854 if height >= 480 else 640))
        return {
            "path": out_video,
            "title": title,
            "duration": 0,
            "thumb": probe.get("thumbnail"),
            "width": width,
            "height": height,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
