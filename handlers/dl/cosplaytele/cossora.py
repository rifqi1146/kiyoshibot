"""FLOW RESOLVER Cossora (cossora.stream)
1. Post Cosplaytele memuat video di <iframe src="https://cossora.stream/embed/<uuid>">.
2. Halaman embed berisi dua string base64 terenkripsi (videoURL, portadaURL) dan
   satu kunci AES heksadesimal 32 karakter di dalam script.
3. Dekripsi AES-256-CBC (PKCS7, IV = 16 byte pertama) menghasilkan URL master
   playlist `index.m3u8?token=...` plus poster.
4. Playlist master -> varian resolusi tertinggi -> playlist media -> daftar segmen
   `.ts` + total durasi dari `#EXTINF`.
"""
import base64
import logging
import re
import time
from urllib.parse import urljoin

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad
from curl_cffi import requests as curl_requests

from .constants import (
    COSSORA_HTTP_TIMEOUT,
    COSSORA_SEG_TIMEOUT,
    UA,
)

log = logging.getLogger(__name__)

EMBED_RE = re.compile(r"https?://cossora\.stream/embed/[0-9a-f-]+", re.I)
_VIDEO_RE = re.compile(r"const\s+videoURL\s*=\s*'([^']+)'")
_PORTADA_RE = re.compile(r"const\s+portadaURL\s*=\s*'([^']+)'")
_KEY_RE = re.compile(r"decryptLink\(videoURL,\s*'([0-9a-fA-F]+)'\)")
_EXTINF_RE = re.compile(r"#EXTINF\s*:\s*([\d.]+)", re.I)
_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:.*?RESOLUTION=(\d+)x(\d+).*?\n(.*?)\n", re.I)


def find_embed(html: str) -> str:
    """Ambil iframe embed cossora.stream dari HTML post Cosplaytele."""
    m = EMBED_RE.search(html or "")
    return m.group(0) if m else ""


def _decrypt(enc: str, key: str) -> str:
    raw = base64.b64decode(enc)
    if len(raw) <= 16:
        raise RuntimeError("ciphertext Cossora terlalu pendek")
    iv, ciphertext = raw[:16], raw[16:]
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv=iv)
    try:
        plain = unpad(cipher.decrypt(ciphertext), AES.block_size)
    except ValueError:
        plain = cipher.decrypt(ciphertext)
    return plain.decode("utf-8", "replace")


def _best_variant(playlist: str, base_url: str) -> str:
    """Pilih varian resolusi tertinggi dari master playlist."""
    lines = [line.strip() for line in playlist.splitlines() if line.strip()]
    best = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF"):
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
            height = int(res.group(2)) if res else 0
            uri = ""
            for j in range(i + 1, len(lines)):
                if not lines[j].startswith("#"):
                    uri = lines[j]
                    break
            if uri:
                url = "https:" + uri if uri.startswith("//") else urljoin(base_url, uri)
                if best is None or height > best[0]:
                    best = (height, url)
        i += 1
    if best:
        return best[1]
    # Playlist media langsung (tanpa STREAM-INF): pakai URL apa adanya.
    return base_url


def _parse_media(playlist: str, base_url: str) -> dict:
    if "#EXT-X-KEY" in playlist:
        raise RuntimeError("Playlist HLS Cossora terenkripsi (#EXT-X-KEY) — belum didukung")
    duration = sum(float(d) for d in _EXTINF_RE.findall(playlist))
    base = base_url.rsplit("/", 1)[0] + "/"
    segments = []
    for raw in playlist.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("//"):
            line = "https:" + line
        elif not line.startswith("http"):
            line = urljoin(base, line)
        segments.append(line)
    if not segments:
        raise RuntimeError("Tidak ada segmen di playlist Cossora")
    return {"segments": segments, "duration": int(duration)}


def resolve_embed(embed_url: str) -> dict:
    """Buka halaman embed -> URL playlist master Cossora + poster.

    Blocking (~1s, 2 request HTTP). Panggil lewat `asyncio.to_thread`.
    """
    last_err = None
    for attempt in range(2):
        try:
            r = curl_requests.get(
                embed_url,
                headers={"User-Agent": UA, "Referer": "https://cosplaytele.com/"},
                impersonate="chrome",
                timeout=COSSORA_HTTP_TIMEOUT,
            )
            if r.status_code != 200:
                raise RuntimeError(f"GET embed Cossora HTTP {r.status_code}")
            html = r.text

            m_video = _VIDEO_RE.search(html)
            m_key = _KEY_RE.search(html)
            if not m_video or not m_key:
                raise RuntimeError("videoURL/kunci Cossora tidak ditemukan di halaman embed")

            playlist = _decrypt(m_video.group(1), m_key.group(1))

            m_portada = _PORTADA_RE.search(html)
            poster = ""
            if m_portada:
                try:
                    poster = _decrypt(m_portada.group(1), m_key.group(1))
                except Exception as e:
                    log.debug("Decrypt poster Cossora gagal | err=%r", e)

            return {"playlist_url": playlist, "poster": poster, "referer": embed_url}
        except Exception as e:
            last_err = e
            log.debug("resolve_embed Cossora attempt=%s gagal | err=%r", attempt + 1, e)
            time.sleep(1.0)
    raise RuntimeError(f"Gagal membuka video Cossora ({last_err})")


def probe_stream(playlist_url: str, referer: str) -> dict:
    """Master playlist -> playlist media (varian terbaik) -> segmen + durasi."""
    r = curl_requests.get(
        playlist_url,
        headers={"User-Agent": UA, "Referer": referer,
                 "Accept": "application/vnd.apple.mpegurl,*/*"},
        impersonate="chrome",
        timeout=COSSORA_HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"GET playlist Cossora HTTP {r.status_code}")
    text = r.text

    media_url = playlist_url
    if "#EXT-X-STREAM-INF" in text:
        media_url = _best_variant(text, playlist_url)
        r = curl_requests.get(
            media_url,
            headers={"User-Agent": UA, "Referer": referer,
                     "Accept": "application/vnd.apple.mpegurl,*/*"},
            impersonate="chrome",
            timeout=COSSORA_HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"GET media playlist Cossora HTTP {r.status_code}")
        text = r.text

    info = _parse_media(text, media_url)
    info["media_url"] = media_url
    return info


def download_segment(url: str, referer: str, out_path: str, retries: int = 3) -> int:
    """Unduh satu segmen .ts dengan retry. Blocking, panggil via to_thread."""
    headers = {"User-Agent": UA, "Referer": referer}
    last_err = None
    for attempt in range(retries):
        try:
            r = curl_requests.get(
                url, headers=headers, impersonate="chrome", timeout=COSSORA_SEG_TIMEOUT,
            )
            if r.status_code == 200 and r.content:
                with open(out_path, "wb") as fh:
                    fh.write(r.content)
                return len(r.content)
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"Gagal mengunduh segmen Cossora ({last_err})")
