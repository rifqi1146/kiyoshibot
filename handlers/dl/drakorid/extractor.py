"""Scraper Drakor.id (drakorid.co) — metadata drama + daftar episode + varian video.

FLOW SCRAPER
------------
1. `scrape_post(url)`:
   - Parse `slug` + nomor episode dari URL (`/nonton/<slug>/`, `/nonton/<slug>/<ep>`,
     `/download-streaming/<slug>/<ep>`, `/watch-streaming/<slug>/<ep>`).
   - GET `/nonton/<slug>/` -> metadata: judul, poster, jumlah episode, genres,
     network, release date, synopsis.
   - Baca daftar episode dari `[data-episode]` / `.episode-pick-num`
     (mis. 7 episode untuk `a-love-other-than-yours-2026`).
2. `resolve_episode(slug, episode)`:
   - GET `/download-streaming/<slug>/<ep>` -> bila tersedia, keruk anchor
     `href="https://vod<NN>.drakor.cc/download/.../drakor.id.<N>p.<slug>-episode-<N>.mp4"`
     (varian 360p/480p/720p sekaligus, tanpa login).
   - Kalau halaman membalas blokir guest ("belum login ... hanya bisa download 1x"),
     fallback API: baca `var token` + `var mId` -> `POST /myapi/episode_detail.php`
     -> `streaming` = id file -> `GET http://admin.drakor.la/go/files/<id>`
     (302) -> Location `http://<node>.drakor.cc/files/<hash>.mp4`.
   - Node yang sama juga melayani `/480p/` dan `/360p/` dari nama file identik;
     varian yang benar-benar ada diprobe lewat `Range: bytes=0-0`.
3. Setiap varian diprobe `Content-Range` untuk ukuran total, lalu di-cache
   (`_VARIANT_CACHE`, TTL 300s). URL unduh selalu di-resolve ulang saat unduh
   karena token/id stream bertanggal.

FLOW DOWNLOADER
---------------
1. `download_video(url, format_id, out_path, ...)` -> resolve ulang episode,
   pilih varian sesuai `format_id`, lalu aria2c 16-koneksi (fallback streaming
   `curl_cffi` chunk 512KB).
2. `TransferStats` untuk progress Telegram + speed/ETA + batas `MAX_TG_SIZE`.
3. Return dict `{"path", "title"}`; `main.py` menangani konversi mp3.
"""
import os
import re
import json
import time
import logging
import asyncio
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from handlers.dl.constants import MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded
from handlers.dl.progress import TransferStats

from .constants import (
    UA, HTTP_TIMEOUT, BASE_URL, CDN_RESOLVER, CDN_QUALITY_DIRS,
    DRAKORID_PROGRESS_INTERVAL,
)

log = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"drakorid\.co/(?:nonton|download-streaming|watch-streaming)/([^/?#]+)(?:/(\d+))?",
    re.I,
)
_DIRECT_LINK_RE = re.compile(
    r'href="(https?://vod\d+\.drakor\.cc/download/[^"]+?\.mp4)"'
)
_QUALITY_RE = re.compile(r"drakor\.id\.(\d{3,4})p\.", re.I)
_TITLE_TRIM = ("Sub Indo", "Drakor.id", "Nonton", " - ")

_VARIANT_CACHE: dict = {}
_CACHE_TTL = 300.0


def _new_session() -> curl_requests.Session:
    sess = curl_requests.Session(impersonate="chrome", headers={"User-Agent": UA})
    try:
        from handlers.drakor.auth import get_auth_cookies
        for name, value in (get_auth_cookies() or {}).items():
            sess.cookies.set(name, value, domain="drakorid.co")
    except Exception as e:
        log.debug("Drakor.id auth cookie attach failed | err=%r", e)
    return sess


def parse_url(url: str) -> tuple[str, int]:
    """`(slug, episode)` dari URL Drakor.id mana pun. Default episode = 1."""
    m = _URL_RE.search(url or "")
    if not m:
        return "", 1
    slug = (m.group(1) or "").strip()
    try:
        ep = int(m.group(2)) if m.group(2) else 1
    except (TypeError, ValueError):
        ep = 1
    return slug, max(1, ep)


def _prune_cache():
    now = time.time()
    for key in [
        k for k, v in list(_VARIANT_CACHE.items())
        if now - v.get("ts", 0) > _CACHE_TTL
    ]:
        _VARIANT_CACHE.pop(key, None)


def cache_variants(key: str, data: dict) -> None:
    _prune_cache()
    _VARIANT_CACHE[key] = {"ts": time.time(), "data": data}


def peek_variants(key: str) -> dict | None:
    item = _VARIANT_CACHE.get(key)
    if not item:
        return None
    if time.time() - item.get("ts", 0) > _CACHE_TTL:
        _VARIANT_CACHE.pop(key, None)
        return None
    return item.get("data")


def _clean_title(raw: str, fallback: str) -> str:
    title = raw or ""
    for token in _TITLE_TRIM:
        title = title.replace(token, "")
    title = " ".join(title.split()).strip()
    return title or fallback


def _parse_episodes(soup: BeautifulSoup) -> list[int]:
    nums = set()
    for el in soup.select("[data-episode], .episode-pick-num"):
        val = el.get("data-episode")
        if val is None:
            val = el.get_text(strip=True)
        m = re.search(r"\d+", str(val or ""))
        if m:
            nums.add(int(m.group(0)))
    return sorted(n for n in nums if n > 0)


def _probe_size(url: str, session: curl_requests.Session) -> int:
    """Ukuran total via `Range: bytes=0-0` (`Content-Range`). 0 kalau gagal."""
    try:
        resp = session.get(
            url,
            headers={"User-Agent": UA, "Referer": f"{BASE_URL}/", "Range": "bytes=0-0"},
            timeout=HTTP_TIMEOUT,
        )
        cr = resp.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        if resp.status_code in (200, 206):
            ct = (resp.headers.get("Content-Type") or "").lower()
            if "video" in ct or "octet-stream" in ct:
                return int(resp.headers.get("Content-Length") or 0)
    except Exception as e:
        log.debug("Drakor.id probe size failed | url=%s err=%r", url, e)
    return 0


def scrape_post(url: str) -> dict:
    """Metadata drama + daftar episode + (opsional) varian episode dari URL."""
    slug, ep = parse_url(url)
    if not slug:
        raise RuntimeError("Invalid Drakor.id URL.")

    session = _new_session()
    detail_url = f"{BASE_URL}/nonton/{slug}/"
    resp = session.get(detail_url, timeout=HTTP_TIMEOUT, headers={"Referer": f"{BASE_URL}/"})
    if resp.status_code == 404:
        raise FileNotFoundError(f"Drama not found (HTTP 404): {detail_url}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} saat mengakses Drakor.id")

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_title = soup.title.text if soup.title else ""
    fallback_title = slug.replace("-", " ").title()
    title = _clean_title(raw_title, fallback_title)

    poster = ""
    poster_img = soup.select_one("img.poster-img")
    if poster_img and poster_img.get("src"):
        poster = poster_img["src"]
    else:
        for img in soup.find_all("img"):
            src = img.get("src") or ""
            if "assets.d-cdn.me" in src and any(sz in src for sz in ("256x", "180x")):
                poster = src
                break

    synopsis = ""
    for p in soup.find_all("p"):
        t = p.get_text(" ", strip=True)
        if t.startswith("Sinopsis"):
            synopsis = t.replace("Sinopsis", "", 1).strip()
            break
    if not synopsis:
        for p in soup.find_all("p"):
            t = p.get_text(" ", strip=True)
            if len(t) > 120 and "Drama:" not in t and "Director:" not in t:
                synopsis = t
                break

    episodes = _parse_episodes(soup)
    raw_text = soup.get_text("\n")

    def _field(pattern: str) -> str:
        m = re.search(pattern, raw_text, re.I)
        if not m:
            return ""
        return " ".join(m.group(1).replace("-", " ").split()).strip()

    is_restricted = bool(
        "Welcome" in raw_title
        and not soup.select(".episode-pick-num, [data-episode], .poster-img")
    )
    if is_restricted:
        title = fallback_title
        synopsis = "This drama requires login or is temporarily restricted on Drakor.id."

    return {
        "slug": slug,
        "episode": ep,
        "title": title,
        "poster": poster,
        "synopsis": synopsis,
        "episodes": episodes,
        "episode_count": len(episodes),
        "genres": _field(r"Genres?\s*:\s*([^\n]+)"),
        "network": _field(r"Network\s*:\s*([^\n]+)"),
        "release": _field(r"Release Date\s*:\s*([^\n]+)"),
        "url": detail_url,
        "restricted": is_restricted,
    }


def _episode_from_direct_links(html_text: str) -> list[dict]:
    """Varian dari anchor direct MP4 di halaman `/download-streaming/`."""
    variants = []
    seen = set()
    for link in _DIRECT_LINK_RE.findall(html_text or ""):
        link = link.strip()
        if link in seen:
            continue
        seen.add(link)
        m = _QUALITY_RE.search(link)
        height = int(m.group(1)) if m else 0
        if not height:
            height = 720
        variants.append({
            "format_id": f"{height}p",
            "label": f"{height}p",
            "height": height,
            "url": link,
            "source": "direct",
        })
    variants.sort(key=lambda v: v["height"], reverse=True)
    return variants


def _token_and_id(html_text: str, session: curl_requests.Session, slug: str) -> tuple[str, str]:
    """`(token, mId)` dari halaman mana pun; fallback ambil halaman `/nonton/`."""
    tok = re.search(r'var token\s*=\s*"([^"]+)"', html_text or "")
    mid = re.search(r'var mId\s*=\s*"?(\d+)"?', html_text or "")
    if tok and mid:
        return tok.group(1), mid.group(1)
    try:
        rn = session.get(f"{BASE_URL}/nonton/{slug}/", timeout=HTTP_TIMEOUT)
        tok = re.search(r'var token\s*=\s*"([^"]+)"', rn.text)
        mid = re.search(r'var mId\s*=\s*"?(\d+)"?', rn.text)
        if tok and mid:
            return tok.group(1), mid.group(1)
    except Exception as e:
        log.debug("Drakor.id token fetch failed | slug=%s err=%r", slug, e)
    return "", ""


def _resolve_via_api(slug: str, episode: int, session: curl_requests.Session) -> list[dict]:
    """Fallback API saat halaman unduh memblokir guest (batas 1x/hari)."""
    dl_page = f"{BASE_URL}/download-streaming/{slug}/{episode}"
    resp = session.get(dl_page, timeout=HTTP_TIMEOUT, headers={"Referer": f"{BASE_URL}/nonton/{slug}/"})
    token, mid = _token_and_id(resp.text, session, slug)
    if not token or not mid:
        return []

    try:
        api = session.post(
            f"{BASE_URL}/myapi/episode_detail.php",
            data={"token": token, "id": mid, "episode": episode},
            headers={
                "Referer": dl_page,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=HTTP_TIMEOUT,
        )
        data = json.loads(api.text)
    except Exception as e:
        log.debug("Drakor.id episode_detail failed | slug=%s ep=%s err=%r", slug, episode, e)
        return []

    stream_id = str(data.get("streaming") or "").strip()
    if not stream_id:
        return []

    try:
        r = session.get(
            f"{CDN_RESOLVER}/{stream_id}",
            headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"},
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
        )
        location = r.headers.get("location") or ""
    except Exception as e:
        log.debug("Drakor.id CDN resolve failed | sid=%s err=%r", stream_id, e)
        return []

    if not location:
        return []

    # location: http://<node>.drakor.cc/files/<hash>.mp4
    parsed = urlparse(location)
    node = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or ""
    m = re.match(r"/([^/]+)/(.+)$", path)
    if not m:
        return []
    filename = m.group(2)

    variants = []
    for quality in CDN_QUALITY_DIRS:
        cand = f"{node}/{quality}/{filename}"
        size = _probe_size(cand, session)
        if size <= 0:
            continue
        # `files/` = master 720p (diverifikasi ffprobe 1280x720). Folder `720p`
        # terpisah kadang tidak ada; `files/` selalu ada sebagai varian tertinggi.
        if quality == "files":
            height = 720
            label = "720p"
        else:
            try:
                height = int(quality.rstrip("p"))
            except ValueError:
                height = 480
            label = quality
        variants.append({
            "format_id": f"{height}p",
            "label": label,
            "height": height,
            "url": cand,
            "source": "cdn",
            "filesize": size,
            "total_size": size,
        })

    # Buang duplikat tinggi (mis. `720p` folder + `files/` sama-sama 720p);
    # ambil ukuran terbesar untuk tinggi yang sama.
    dedup: dict[int, dict] = {}
    for v in variants:
        prev = dedup.get(v["height"])
        if not prev or v["total_size"] > prev["total_size"]:
            dedup[v["height"]] = v
    variants = list(dedup.values())

    variants.sort(key=lambda v: v["height"], reverse=True)
    return variants


def resolve_episode(slug: str, episode: int, session: curl_requests.Session | None = None) -> list[dict]:
    """Varian unduh untuk satu episode (halaman direct dulu, fallback API)."""
    key = f"{slug}:{episode}"
    cached = peek_variants(key)
    if cached:
        return cached.get("variants") or []

    own = session is None
    sess = session or _new_session()
    try:
        dl_page = f"{BASE_URL}/download-streaming/{slug}/{episode}"
        try:
            resp = sess.get(
                dl_page, timeout=HTTP_TIMEOUT,
                headers={"Referer": f"{BASE_URL}/nonton/{slug}/"},
            )
            variants = _episode_from_direct_links(resp.text)
        except Exception as e:
            log.debug("Drakor.id download page failed | slug=%s ep=%s err=%r", slug, episode, e)
            variants = []

        if not variants:
            variants = _resolve_via_api(slug, episode, sess)

        for v in variants:
            if not v.get("filesize"):
                v["filesize"] = _probe_size(v["url"], sess)
            v["total_size"] = v.get("filesize") or v.get("total_size") or 0
            v["ext"] = "mp4"
            v["has_audio"] = True
            v["fps"] = "-"

        if variants:
            cache_variants(key, {"variants": variants, "ts": time.time()})
        return variants
    finally:
        if own:
            try:
                sess.close()
            except Exception:
                pass


async def download_video(
    url: str, format_id: str, out_path: str, bot, chat_id,
    status_msg_id, title: str, episode: int | None = None, notify: bool = True,
):
    slug, ep = parse_url(url)
    if episode:
        ep = int(episode)

    variants = await asyncio.to_thread(resolve_episode, slug, ep)
    if not variants:
        raise RuntimeError(
            f"Tidak ada varian unduh untuk {slug} episode {ep}. "
            "Kemungkinan dibatasi login atau episode belum tersedia."
        )

    fid = (format_id or "").strip().lower()
    target = next((v for v in variants if v["format_id"].lower() == fid), None)
    if not target:
        target = variants[0]

    dl_url = target["url"]
    headers = {"User-Agent": UA, "Referer": f"{BASE_URL}/nonton/{slug}/"}
    total_hint = int(target.get("total_size") or target.get("filesize") or 0)
    if total_hint > MAX_TG_SIZE:
        raise FileSizeLimitExceeded(
            f"File exceeds 2GB limit ({total_hint / 1024 ** 3:.2f} GB). Download canceled."
        )

    label = f"Drakor.id {target.get('label')}"

    try:
        from handlers.dl.aria2 import download_aria2
        ok = await download_aria2(
            dl_url, out_path,
            headers=headers, total_size=total_hint,
            kind="Drakor.id download", label=label,
            title=title, bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            notify=notify, edit_interval=DRAKORID_PROGRESS_INTERVAL,
            timeout=HTTP_TIMEOUT * 12,
        )
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return {"path": out_path, "title": title}
    except FileSizeLimitExceeded:
        raise
    except Exception as e:
        log.warning("Drakor.id aria2c exception, fallback streaming | err=%r", e)

    def _get():
        return curl_requests.get(
            dl_url, headers=headers, impersonate="chrome",
            stream=True, timeout=HTTP_TIMEOUT,
        )

    resp = await asyncio.to_thread(_get)
    if resp.status_code not in (200, 206):
        raise RuntimeError(f"HTTP {resp.status_code} saat mengunduh video Drakor.id.")

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
            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            title=title, kind="Drakor.id download", label=label,
            log_interval=DRAKORID_PROGRESS_INTERVAL,
            edit_interval=DRAKORID_PROGRESS_INTERVAL,
        )

    write_task = asyncio.ensure_future(asyncio.to_thread(_write))
    while not write_task.done():
        if notify and status_msg_id:
            await stats.emit(
                bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                title=title, kind="Drakor.id download", label=label,
                log_interval=DRAKORID_PROGRESS_INTERVAL,
                edit_interval=DRAKORID_PROGRESS_INTERVAL,
            )
        await asyncio.sleep(0.5)

    await write_task
    stats.log_done("Drakor.id download", label=label)
    return {"path": out_path, "title": title}
