import re
import time
import logging
from urllib.parse import urlsplit, urlunsplit, urljoin
from curl_cffi import requests as curl_requests

from .constants import (
    UA,
    _HTTP_TIMEOUT,
    _PREFERRED_HOSTS,
    _DECOY_HOSTS,
    _MIRROR_GROUPS,
    _MIRROR_EXTRA_HOSTS,
    DEBUG_RETROTUBE,
    mirror_domains_for,
)
from .packer import _PACKER_RE, _unpack_eval, _deobfuscate_voe_json

log = logging.getLogger(__name__)


def _dbg(msg, *args):
    if DEBUG_RETROTUBE:
        log.warning("RTDBG | " + msg, *args)


def _host(url: str) -> str:
    try:
        return (url or "").split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    except Exception:
        return ""


def _collect_embed_candidates(html_text: str) -> list:
    """Kumpulkan semua kandidat URL embed, host lulustream diprioritaskan."""
    seen = []
    order = []

    def add(u):
        u = (u or "").strip()
        if not u or not u.lower().startswith(("http://", "https://")):
            return
        if u in seen:
            return
        seen.append(u)
        order.append(u)

    for m in re.finditer(r'itemprop="embedUrl"\s+content="([^"]+)"', html_text, re.I):
        add(m.group(1))
    for m in re.finditer(r'<iframe[^>]+(?:src|data-src)="(https?://[^"]+)"', html_text, re.I):
        add(m.group(1))

    pref = [u for u in order if any(_host(u) == h or _host(u).endswith("." + h) for h in _PREFERRED_HOSTS)]
    rest = [u for u in order if u not in pref]
    return pref + rest


def _scrape_post(url: str, _depth: int = 0) -> tuple:
    """-> (title, thumb_url, [embed_candidates]) dari halaman post.
    Jika ini ternyata halaman kategori/listing, otomatis loncat ke post video pertama."""
    r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=_HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} saat mengambil halaman")
    html_text = r.text

    title = None
    for pat in (
        r'<meta\s+property="og:title"\s+content="([^"]+)"',
        r'<meta\s+content="([^"]+)"\s+property="og:title"',
        r"<title>([^<]+)</title>",
    ):
        m = re.search(pat, html_text, re.I)
        if m:
            title = m.group(1).strip()
            break
    title = title or "Video"

    thumb = None
    for pat in (
        r'<meta\s+property="og:image"\s+content="([^"]+)"',
        r'<meta\s+content="([^"]+)"\s+property="og:image"',
    ):
        m = re.search(pat, html_text, re.I)
        if m:
            thumb = m.group(1).strip()
            break

    candidates = _collect_embed_candidates(html_text)

    # Auto-resolve listing/category page:
    # Jika tidak ada embed (0 kandidat) dan ini panggilan pertama, 
    # periksa apakah ada daftar <article> (halaman kategori)
    if not candidates and _depth == 0:
        articles = re.findall(r'<article\b.*?</article>', html_text, re.S | re.I)
        if articles:
            # Ambil link video pertama
            for art in articles:
                m_href = re.search(r'href="([^"]+)"', art, re.I)
                if m_href:
                    first_post_url = m_href.group(1).strip()
                    # Pastikan ia ada di host yang sama untuk mencegah jebakan iklan
                    if _host(first_post_url) == _host(url):
                        _dbg("Kategori terdeteksi. Loncat ke post pertama: %s", first_post_url)
                        return _scrape_post(first_post_url, _depth=1)

    _dbg("post scraped | title=%r candidates=%s", title, candidates)
    return title, thumb, candidates


def _mirror_urls(raw_url: str) -> list:
    """Kembalikan URL post yang sama di domain mirror (path identik), tanpa domain asal.

    Domain wildcard (mis. "lendirqu.*") tidak bisa dipakai sebagai host, jadi
    untuk grup tersebut hanya dipakai domain yang terdaftar eksplisit di
    _MIRROR_EXTRA_HOSTS + domain asal.
    """
    try:
        parts = urlsplit(raw_url)
    except Exception:
        return []
    host = (parts.netloc or "").lower().split(":", 1)[0]
    if not host:
        return []

    group = mirror_domains_for(host)
    if not group:
        return []

    hosts = []
    for g in group:
        if g.endswith(".*"):
            # Wildcard: pakai host yang sudah terverifikasi aktif untuk prefix ini.
            hosts.extend(_MIRROR_EXTRA_HOSTS.get(g, ()))
        else:
            hosts.append(g)

    out = []
    seen = {host}
    if host.startswith("www."):
        seen.add(host[4:])
    for g in hosts:
        if g in seen:
            continue
        seen.add(g)
        out.append(urlunsplit((parts.scheme, g, parts.path, parts.query, parts.fragment)))
    return out


def _resolve_embed(embed_url: str, referer: str, prefer_gdrive: bool = True):
    """-> (kind, media_url, thumb) dengan kind di {'hls','mp4'}, atau (None,None,None)."""
    
    # VOE rotates its player domain via a literal JavaScript redirect.
    # Do not pin an obsolete clone before fetching the canonical embed.
    is_voe = _host(embed_url) in ('voe.sx', 'www.voe.sx')

    text = None
    for attempt in range(2):
        try:
            r = curl_requests.get(
                embed_url,
                headers={"User-Agent": UA, "Referer": referer or embed_url},
                impersonate="chrome",
                timeout=_HTTP_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 200:
                text = r.text
                # voe.sx first returns a tiny JS page that redirects to a current
                # player mirror (the host rotates; e.g. teresapoliticallearn.com).
                if is_voe:
                    redirect = re.search(
                        r"window\.location\.href\s*=\s*['\"](https?://[^'\"]+/e/[^'\"?#]+)",
                        text,
                        re.I,
                    )
                    if redirect:
                        player_url = redirect.group(1)
                        player = curl_requests.get(
                            player_url,
                            headers={"User-Agent": UA, "Referer": referer or embed_url},
                            impersonate="chrome",
                            timeout=_HTTP_TIMEOUT,
                            allow_redirects=True,
                        )
                        if player.status_code == 200:
                            text = player.text
                            embed_url = player_url
                        else:
                            _dbg("VOE mirror non-200 | %s %s", player_url, player.status_code)
                break
            _dbg("resolve non-200 | %s %s", embed_url, r.status_code)
        except Exception as e:
            _dbg("resolve fetch gagal (attempt %s) | %s %r", attempt + 1, embed_url, e)
            time.sleep(1.0)
    if not text:
        return None, None, None

    m = _PACKER_RE.search(text)
    if m:
        try:
            text = _unpack_eval(m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|"))
        except Exception as e:
            _dbg("unpack gagal | %s %r", embed_url, e)

    # VOE / kloningnya (miaw.lol): config JSON ter-obfuscate -> m3u8 (source) / mp4.
    voe_hls, voe_mp4 = _deobfuscate_voe_json(text)
    if voe_hls:
        return "hls", voe_hls, None
    if voe_mp4:
        return "mp4", voe_mp4, None

    # mumu.watch: MASTER_URL disimpan plaintext di JS dengan slash ter-escape (\/).
    if "mumu.watch" in embed_url or "m-cdn.video" in text:
        mm = re.search(r'MASTER_URL\s*=\s*"([^"]+)"', text)
        if mm:
            return "hls", mm.group(1).replace("\\/", "/"), None
        mm2 = re.search(r'https?:\\/\\/[^"\']+?master\.m3u8', text)
        if mm2:
            return "hls", mm2.group(0).replace("\\/", "/"), None

    # HTML5 <video>/<source> (mis. lordfile.site): mp4 langsung, URL bisa berisi spasi
    for m in re.finditer(r'<(?:source|video)\b[^>]*\bsrc=["\']([^"\']+)["\']', text, re.I):
        u = m.group(1).strip()
        if re.search(r"\.(?:mp4|m3u8|webm)(?:\?|#|$)", u, re.I):
            u = u.replace(" ", "%20")
            return ("hls" if ".m3u8" in u.lower() else "mp4"), u, None

    # db.fbplay.vip: Google Drive ID diproxy jadi HLS (dengan segmen PNG 1x1 dari tiktokcdn).
    # Bypass langsung ke Google Drive aslinya untuk unduhan cepat dan utuh.
    if prefer_gdrive:
        m_gid = re.search(r'db\.fbplay\.vip/embed/video/([a-zA-Z0-9_-]{20,})', embed_url, re.I)
        if m_gid:
            return "gdrive", m_gid.group(1), None

    # nontonvideo.xyz / JWPlayer path relatif: /stream/...
    # (bisa ter-escape quotes: \'file\':\'/stream/...\')
    stream_m = re.search(r'''['"]?file['"]?\s*:\s*['"](/stream/[^'"]+)['"]''', text)
    if not stream_m:
        stream_m = re.search(r'''\\['"]file\\['"]\s*:\s*\\['"](/stream/[^\\'"]+)\\['"]''', text)
    if stream_m:
        stream_path = stream_m.group(1)
        full_stream_url = urljoin(embed_url, stream_path)
        return "mp4", full_stream_url, None

    # JWPlayer m3u8
    m3u8 = re.findall(r'file\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', text)
    if not m3u8:
        m3u8 = re.findall(r'https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*', text)
    if m3u8:
        thumbs = re.findall(r'image\s*:\s*["\'](https?://[^"\']+)["\']', text)
        return "hls", m3u8[0], (thumbs[0] if thumbs else None)

    # JWPlayer mp4
    mp4 = re.findall(r'file\s*:\s*["\'](https?://[^"\']+\.mp4[^"\']*)["\']', text)
    if not mp4:
        mp4 = re.findall(r'https?://[^"\'\s\\]+\.mp4[^"\'\s\\]*', text)
    for u in mp4:
        if any(_host(u) == d or _host(u).endswith("." + d) for d in _DECOY_HOSTS):
            continue
        return "mp4", u, None

    _dbg("no media | %s", embed_url)
    return None, None, None
