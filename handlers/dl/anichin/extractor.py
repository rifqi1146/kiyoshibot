"""Extractor Anichin: scrape halaman + resolve chain mirror -> URL file.

FLOW EXTRACTOR
--------------
1. `scrape_post(url)` -> HTML halaman episode, ambil `title` + `div.soraurlx`
   (mirror per resolusi) + `select.mirror` (server streaming).
2. `resolve_shortener(url)` -> ikuti shortener (`bit.ly`, `lifehei.my.id`) sampai
   dapat halaman `mirrored.to/multilinks/<hash>`.
3. `resolve_multilinks(url)` -> halaman multilinks memuat satu link
   `mirrored.to/files/<uid>/<nama>.mp4_links` per resolusi. Ambil yang cocok
   dengan label (360p/480p/720p/1080p) atau semuanya.
4. `resolve_host_links(files_url)` -> GET halaman files -> baca endpoint
   `/mirstats.php?...` dari `<script>` -> poll sampai `<div id="done">1</div>` ->
   kumpulkan `form[action=/out_url]` + host (`img[alt]`).
5. `resolve_out_url(fields)` -> POST `/out_url` -> halaman redirect memuat
   `URL=<safelinkearn>...&url=<file_host>`. Ambil parameter `url=` = link file asli.
6. `pick_best_host(hosts)` -> pilih host sesuai `HOST_PRIORITY`.

Catatan: semua langkah murni HTTP (curl_cffi impersonate Chrome), TANPA yt-dlp.
"""
import html as html_mod
import logging
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .constants import (
    HOST_PRIORITY,
    HTTP_TIMEOUT,
    MIRSTATS_MAX_POLLS,
    MIRSTATS_POLL_DELAY,
    UA,
)

log = logging.getLogger(__name__)

_session = None


def _http():
    global _session
    if _session is None:
        _session = curl_requests.Session(impersonate="chrome")
    return _session


def _get(url: str, referer: str = "") -> tuple[int, str, str]:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        r = _http().get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
        return r.status_code, (r.text or ""), str(r.url)
    except Exception as e:
        log.debug("Anichin GET failed | url=%s err=%r", url, e)
        return 0, "", url


def _clean(text: str) -> str:
    return " ".join((text or "").split())


# --------------------------------------------------------------------------- #
# 1. Halaman episode
# --------------------------------------------------------------------------- #

def scrape_post(url: str) -> dict:
    """-> {title, thumbnail, mirrors:[{label,name,url}], servers:[{label,url}], raw_html}"""
    import base64

    code, raw, _ = _get(url)
    if code != 200 or not raw:
        raise RuntimeError(f"HTTP {code} saat mengambil halaman Anichin")

    soup = BeautifulSoup(raw, "html.parser")
    h1 = soup.select_one("h1.entry-title") or soup.find("h1")
    title = _clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        title = og["content"].strip() if og and og.get("content") else "Anichin Video"

    thumb = ""
    ogi = soup.select_one('meta[property="og:image"]')
    if ogi and ogi.get("content"):
        thumb = ogi["content"].strip()

    mirrors = []
    seen = set()
    for row in soup.select("div.soraurlx"):
        strong = row.find("strong")
        a = row.find("a", href=True)
        if not a:
            continue
        label = _clean(strong.get_text(" ", strip=True)) if strong else ""
        u = (a.get("href") or "").strip()
        # Dedup per (label, url): dua resolusi berbeda boleh berbagi URL yang
        # sama (mis. 360p & 1080p sama-sama `inshorturl.in/6OM29W`).
        key = (label.lower(), u)
        if not u or key in seen:
            continue
        seen.add(key)
        mirrors.append({
            "label": label,
            "name": _clean(a.get_text(" ", strip=True)) or "Mirror",
            "url": u,
        })

    servers = []
    sel = soup.select_one("select.mirror")
    if sel:
        s_seen = set()
        for opt in sel.select("option"):
            val = (opt.get("value") or "").strip()
            if not val:
                continue
            try:
                dec = base64.b64decode(val).decode("utf-8", "replace")
            except Exception:
                continue
            m = re.search(r'(?:SRC|src)=["\']?([^"\'\s>]+)', dec)
            if not m:
                continue
            su = m.group(1).strip()
            if su in s_seen:
                continue
            s_seen.add(su)
            servers.append({
                "label": _clean(opt.get_text(" ", strip=True)) or (urlsplit(su).hostname or "server"),
                "url": su,
            })

    return {"title": title, "thumbnail": thumb, "mirrors": mirrors, "servers": servers, "raw_html": raw}


# --------------------------------------------------------------------------- #
# 2. Shortener
# --------------------------------------------------------------------------- #

def resolve_shortener(url: str, referer: str = "") -> str:
    """Ikuti shortener sampai dapat URL `mirrored.to` (atau URL final).

    `bit.ly` -> halaman preview dengan anchor "Continue to destination".
    `lifehei.my.id` -> parameter `o=` base64 (URL tujuan langsung).
    Fallback: ikuti redirect apa pun / ambil link eksternal pertama.
    """
    import base64

    u = (url or "").strip()
    if not u:
        return ""

    # lifehei.my.id: tujuan ada di parameter `o=` (base64, bisa di-encode ganda).
    if "lifehei" in u and "o=" in u:
        m = re.search(r"[?&]o=([^&]+)", u)
        if m:
            raw_param = m.group(1)
            for _ in range(2):
                try:
                    decoded = base64.b64decode(raw_param + "=" * (-len(raw_param) % 4)).decode("utf-8", "replace")
                except Exception:
                    break
                if decoded.startswith("http") and decoded != raw_param:
                    return decoded
                raw_param = decoded

    code, raw, final = _get(u, referer=referer)
    if code != 200 or not raw:
        return final or ""

    # Redirect sudah ke host tujuan (bukan shortener lagi).
    host = (urlsplit(final).hostname or "").lower()
    if host and not any(s in host for s in ("bit.ly", "bitly", "inshorturl", "lifehei")):
        return final

    soup = BeautifulSoup(raw, "html.parser")
    # bit.ly preview: anchor "Continue to destination" / link ke host tujuan.
    for a in soup.select("a[href]"):
        href = html_mod.unescape(a.get("href") or "")
        txt = _clean(a.get_text(" ", strip=True)).lower()
        if "continue to destination" in txt and href.startswith("http"):
            return href
    for a in soup.select("a[href]"):
        href = html_mod.unescape(a.get("href") or "")
        h = (urlsplit(href).hostname or "").lower()
        if href.startswith("http") and "mirrored.to" in h:
            return href
    # meta refresh
    m = re.search(r'http-equiv=["\']?refresh["\']?[^>]*content=["\'][^;]+;\s*url=([^"\']+)', raw, re.I)
    if m:
        return html_mod.unescape(m.group(1).strip())
    return final or ""


# --------------------------------------------------------------------------- #
# 3. mirrored.to multilinks
# --------------------------------------------------------------------------- #

def resolve_multilinks(url: str) -> list[dict]:
    """Halaman multilinks -> daftar `mirrored.to/files/<uid>/<nama>.mp4_links`.

    -> [{label, name, files_url}]
    """
    code, raw, final = _get(url)
    if code != 200 or not raw:
        return []

    text = html_mod.unescape(raw)
    found = []
    seen = set()
    for m in re.finditer(r'(https?://www\.mirrored\.to/files/[^"\'\s<>]+_links)', text):
        files_url = m.group(1)
        if files_url in seen:
            continue
        seen.add(files_url)
        # Nama file memuat label resolusi, mis. ...[720p].mp4_links
        name = files_url.rsplit("/", 1)[-1]
        lm = re.search(r"\[(\d{3,4}p|4K)\]", name, re.I)
        label = lm.group(1) if lm else ""
        found.append({"label": label, "name": name, "files_url": files_url})

    # Urutkan resolusi tertinggi dulu.
    def _h(item):
        m = re.search(r"(\d+)", item.get("label") or "")
        return int(m.group(1)) if m else 0

    found.sort(key=_h, reverse=True)
    return found


# --------------------------------------------------------------------------- #
# 4. mirrored.to files -> host links
# --------------------------------------------------------------------------- #

def resolve_host_links(files_url: str) -> list[dict]:
    """GET halaman files -> poll `/mirstats.php` -> daftar host.

    -> [{host, fields}]
    """
    code, raw, final = _get(files_url)
    if code != 200 or not raw:
        return []

    # Halaman `_links` belum punya endpoint mirstats: ada anchor
    # "Click to view the download links" ke `/files/<uid>/?hash=...&dl=0`.
    # Endpoint `/mirstats.php?...` baru muncul di halaman hash itu.
    if "/mirstats.php?" not in raw:
        hash_url = ""
        soup0 = BeautifulSoup(raw, "html.parser")
        for a in soup0.select("a[href]"):
            href = html_mod.unescape(a.get("href") or "")
            if "/files/" in href and "hash=" in href:
                hash_url = href
                break
        if hash_url:
            code0, raw0, _ = _get(hash_url, referer=files_url)
            if code0 == 200 and raw0:
                raw = raw0
                files_url = hash_url

    m = re.search(r"/mirstats\.php\?([^\"']+)", raw)
    if not m:
        log.debug("Anichin mirstats endpoint tidak ditemukan | %s", files_url)
        return []

    api = "https://www.mirrored.to/mirstats.php?" + html_mod.unescape(m.group(1))
    body = ""
    for attempt in range(max(1, MIRSTATS_MAX_POLLS)):
        code2, body, _ = _get(api, referer=files_url)
        if code2 == 200 and body and 'id="done"' in body:
            break
        time.sleep(MIRSTATS_POLL_DELAY)

    if not body:
        return []

    soup = BeautifulSoup(body, "html.parser")
    hosts = []
    for tr in soup.select("tr"):
        form = tr.select_one("form")
        if not form:
            continue
        img = tr.select_one("img")
        host = (img.get("alt") if img else "") or ""
        fields = {}
        for i in form.select("input[type=hidden]"):
            fields[i.get("name")] = i.get("value")
        action = form.get("action") or "/out_url"
        if fields:
            hosts.append({"host": host, "action": action, "fields": fields})
    return hosts


# --------------------------------------------------------------------------- #
# 5. POST /out_url -> URL file asli
# --------------------------------------------------------------------------- #

def resolve_out_url(host_entry: dict, referer: str = "") -> str:
    """POST `/out_url` -> ambil parameter `url=` (link file host asli)."""
    action = host_entry.get("action") or "/out_url"
    if action.startswith("/"):
        action = "https://www.mirrored.to" + action
    try:
        r = _http().post(
            action,
            data=host_entry.get("fields") or {},
            headers={
                "User-Agent": UA,
                "Referer": referer or "https://www.mirrored.to/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
        )
    except Exception as e:
        log.debug("Anichin /out_url gagal | host=%s err=%r", host_entry.get("host"), e)
        return ""

    body = html_mod.unescape(r.text or "")
    m = re.search(r"URL=([^\s\"'>]+)", body)
    target = m.group(1) if m else ""
    if not target:
        loc = r.headers.get("Location") or ""
        if loc:
            target = html_mod.unescape(loc)
    if not target:
        return ""
    if "url=" in target:
        real = target.split("url=", 1)[1].split("&")[0]
        return html_mod.unescape(real)
    return target


def pick_best_host(hosts: list[dict]) -> dict | None:
    """Pilih host sesuai `HOST_PRIORITY` (GoogleDrive didahulukan)."""
    if not hosts:
        return None
    for wanted in HOST_PRIORITY:
        for h in hosts:
            if (h.get("host") or "").lower() == wanted.lower():
                return h
    return hosts[0]


def resolve_download_url(mirror_url: str, label: str = "", referer: str = "") -> dict:
    """Chain lengkap: shortener -> multilinks -> files -> host -> URL file.

    -> {url, host, label, name} atau {} kalau gagal.
    """
    target = resolve_shortener(mirror_url, referer=referer)
    if not target:
        return {}

    files_list = []
    if "mirrored.to/multilinks/" in target:
        files_list = resolve_multilinks(target)
    elif "mirrored.to/files/" in target:
        files_list = [{"label": label, "name": target.rsplit("/", 1)[-1], "files_url": target}]
    if not files_list:
        return {}

    chosen_files = None
    if label:
        for f in files_list:
            if (f.get("label") or "").lower() == label.lower():
                chosen_files = f
                break
    if not chosen_files:
        chosen_files = files_list[0]

    hosts = resolve_host_links(chosen_files["files_url"])
    if not hosts:
        return {}

    entry = pick_best_host(hosts)
    if not entry:
        return {}

    real = resolve_out_url(entry, referer=chosen_files["files_url"])
    if not real:
        return {}

    return {
        "url": real,
        "host": entry.get("host") or "",
        "label": chosen_files.get("label") or label,
        "name": chosen_files.get("name") or "",
    }


def resolve_all_candidates(mirror_url: str, label: str = "", referer: str = "") -> list[dict]:
    """Semua kandidat host untuk satu resolusi, urut sesuai `HOST_PRIORITY`.

    Dipakai downloader supaya bisa mencoba host berikutnya kalau host pertama
    gagal (mis. GoogleDrive kena "quota exceeded"). -> [{host, url, label, name}]
    """
    target = resolve_shortener(mirror_url, referer=referer)
    if not target:
        return []

    files_list = []
    if "mirrored.to/multilinks/" in target:
        files_list = resolve_multilinks(target)
    elif "mirrored.to/files/" in target:
        files_list = [{"label": label, "name": target.rsplit("/", 1)[-1], "files_url": target}]
    if not files_list:
        return []

    chosen_files = None
    if label:
        for f in files_list:
            if (f.get("label") or "").lower() == label.lower():
                chosen_files = f
                break
    if not chosen_files:
        chosen_files = files_list[0]

    hosts = resolve_host_links(chosen_files["files_url"])
    if not hosts:
        return []

    # Urutkan sesuai preferensi lalu resolve satu per satu.
    ordered = []
    used = set()
    for wanted in HOST_PRIORITY:
        for h in hosts:
            if (h.get("host") or "").lower() == wanted.lower() and id(h) not in used:
                used.add(id(h))
                ordered.append(h)
    for h in hosts:
        if id(h) not in used:
            used.add(id(h))
            ordered.append(h)

    out = []
    for entry in ordered:
        real = resolve_out_url(entry, referer=chosen_files["files_url"])
        if not real:
            continue
        out.append({
            "host": entry.get("host") or "",
            "url": real,
            "label": chosen_files.get("label") or label,
            "name": chosen_files.get("name") or "",
            "files_url": chosen_files["files_url"],
        })
    return out


# --------------------------------------------------------------------------- #
# 6. Resolver per-host (file page -> direct URL)
# --------------------------------------------------------------------------- #

def _buzzheavier_direct(url: str) -> str:
    """Buzzheavier: token ada di `copyDownloadLink('...')` (JS), bukan di HTML link.

    -> `https://buzzheavier.com/<id>/download?t=<token>` (video/mp4 langsung).
    """
    code, raw, _ = _get(url)
    if code != 200 or not raw:
        return ""
    m = re.search(r"copyDownloadLink\('([^']+)'\)", raw)
    if not m:
        return ""
    path = m.group(1).replace("\\/", "/")
    if path.startswith("/"):
        return "https://buzzheavier.com" + path
    return "https://buzzheavier.com/" + path


def _gofile_direct(url: str) -> str:
    """Gofile: butuh token guest + API contents -> `link` (CDN langsung)."""
    code, raw, _ = _get(url)
    if code != 200 or not raw:
        return ""
    m = re.search(r"/d/([A-Za-z0-9]+)", url)
    content_id = m.group(1) if m else ""
    if not content_id:
        m = re.search(r"contentId\s*[=:]\s*['\"]([A-Za-z0-9]+)", raw)
        content_id = m.group(1) if m else ""
    if not content_id:
        return ""
    try:
        tok_r = _http().post("https://api.gofile.io/accounts", timeout=HTTP_TIMEOUT)
        token = ((tok_r.json() or {}).get("data") or {}).get("token") or ""
        if not token:
            return ""
        api = f"https://api.gofile.io/contents/{content_id}?wt=4fd6sg89d7s6"
        r = _http().get(api, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}, timeout=HTTP_TIMEOUT)
        data = (r.json() or {}).get("data") or {}
        for child in (data.get("children") or {}).values():
            link = child.get("link") or ""
            if link:
                return link
        return data.get("link") or ""
    except Exception as e:
        log.debug("Gofile resolve failed | url=%s err=%r", url, e)
        return ""


def _streamwish_direct(url: str) -> str:
    """StreamWish/strwish: cari `sources`/m3u8 di HTML atau via `/dl?op=view`."""
    code, raw, _ = _get(url)
    if code != 200 or not raw:
        return ""
    m = re.search(r'file\s*:\s*["\']([^"\']+)["\']', raw)
    if m and ".m3u8" in m.group(1):
        return m.group(1)
    m = re.search(r'https?://[^"\'\s<>]+\.m3u8[^"\'\s<>]*', raw)
    if m:
        return m.group(0)
    return ""


def resolve_direct_file(file_url: str) -> str:
    """Ubah halaman file host -> URL file langsung (per-host).

    -> URL langsung, atau `file_url` apa adanya kalau host belum didukung
    (GoogleDrive ditangani di `main._download_gdrive`).
    """
    host = (urlsplit(file_url).hostname or "").lower()
    if "buzzheavier" in host:
        return _buzzheavier_direct(file_url) or file_url
    if "gofile" in host:
        return _gofile_direct(file_url) or file_url
    if "strwish" in host or "streamwish" in host or "streamhg" in host:
        return _streamwish_direct(file_url) or file_url
    return file_url


def list_resolutions(mirror_url: str, referer: str = "") -> list[dict]:
    """Daftar resolusi yang tersedia dari satu link shortener (multilinks).

    -> [{height, format_id, label, files_url}]
    """
    target = resolve_shortener(mirror_url, referer=referer)
    if not target:
        return []
    files_list = resolve_multilinks(target) if "multilinks" in target else []
    out = []
    for f in files_list:
        m = re.search(r"(\d+)", f.get("label") or "")
        height = int(m.group(1)) if m else 0
        out.append({
            "height": height,
            "format_id": str(height) if height else (f.get("label") or ""),
            "label": f.get("label") or "",
            "files_url": f["files_url"],
        })
    return out
