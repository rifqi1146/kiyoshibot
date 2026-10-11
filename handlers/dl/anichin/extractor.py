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
import json
import logging
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .constants import (
    CAPTCHA_SHORTENER_HOSTS,
    HOST_PRIORITY,
    HTTP_TIMEOUT,
    MIRSTATS_MAX_POLLS,
    MIRSTATS_POLL_DELAY,
    UA,
    VIDHIDE_HOSTS,
    VIDHIDE_TIMEOUT,
)

log = logging.getLogger(__name__)

_session = None


def _http():
    global _session
    if _session is None:
        # `verify=False` WAJIB:信任 chain TLS beberapa host embed Anichin
        # (ahvsh.com, sbrapid.com, keluarga vidhide/streamwish) tidak lengkap dan
        # curl_cffi melempar `CertificateVerifyError` (curl 60: unable to get local
        # issuer certificate) -> resolve bisu mengembalikan {} dan picker kosong.
        # Host tujuan cuma dibaca HTML-nya, tidak ada kredensial yang dikirim.
        _session = curl_requests.Session(impersonate="chrome", verify=False)
    return _session


def _header_safe(value: str) -> str:
    """Buat nilai header aman (ASCII/latin-1).

    Nama file mirrored.to bisa memuat karakter Unicode seperti `Ⅴ` (U+2164)
    di path URL. Kalau string itu dipakai sebagai header `Referer`, curl_cffi
    melempar `UnicodeEncodeError: 'latin-1' codec can't encode character`.
    Exception itu ditelan `_get` sehingga hasilnya 0 host tanpa jejak yang jelas.
    Persen-encode karakter non-ASCII supaya header tetap valid.
    """
    if not value:
        return ""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        from urllib.parse import quote
        # `safe` dipertahankan seluas mungkin supaya struktur URL tidak rusak.
        return quote(value, safe=":/?#[]@!$&'()*+,;=%~.-_")


def _get(url: str, referer: str = "", timeout: float | None = None) -> tuple[int, str, str]:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = _header_safe(referer)
    try:
        r = _http().get(url, headers=headers, timeout=timeout or HTTP_TIMEOUT, allow_redirects=True)
        return r.status_code, (r.text or ""), str(r.url)
    except Exception as e:
        # Jangan telan diam-diam: exception di sini (bukan HTTP non-200) hampir
        # selalu masalah nyata (mis. header unicode, DNS, TLS).
        log.warning("Anichin GET failed | url=%s err=%r", url, e)
        return 0, "", url


def _clean(text: str) -> str:
    return " ".join((text or "").split())


# --------------------------------------------------------------------------- #
# 1. Halaman episode
# --------------------------------------------------------------------------- #

def scrape_post(url: str) -> dict:
    """-> {title, thumbnail, mirrors:[{label,name,url}], servers:[{label,url}], raw_html}

    Tiap baris `div.soraurlx` bisa memuat LEBIH DARI SATU anchor (mis. `Mirrored`
    + `Terabox`) untuk resolusi yang sama; semuanya dikumpulkan supaya downloader
    bisa mencoba link berikutnya kalau yang pertama butuh captcha.
    """
    import base64

    code, raw, _ = _get(url)
    if code != 200 or not raw:
        raise RuntimeError(f"HTTP {code} while fetching Anichin page")

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
        label = _clean(strong.get_text(" ", strip=True)) if strong else ""
        for a in row.find_all("a", href=True):
            u = (a.get("href") or "").strip()
            # Dedup per (label, url, nama): dua resolusi berbeda boleh berbagi URL
            # yang sama, dan satu resolusi boleh punya beberapa penyedia (Mirrored/Terabox).
            key = (label.lower(), u, _clean(a.get_text(" ", strip=True)).lower())
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

    - `bit.ly` -> halaman preview dengan anchor "Continue to destination".
    - `lifehei.my.id` -> parameter `o=` base64 (URL tujuan langsung).
    - `clk.asia`/`dekhe.click` (Clicksfly) -> **butuh hCaptcha**, tujuan TIDAK
      ada di HTML. Dikembalikan "" + log jelas (lihat `is_captcha_shortener`).
    - Fallback: ikuti redirect / meta refresh / link `mirrored.to` pertama.
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
        if not is_captcha_shortener(final):
            return final

    soup = BeautifulSoup(raw, "html.parser")

    # Captcha (Clicksfly/hCaptcha): tujuan tidak ada di HTML. Deteksi SEBELUM
    # mencoba apa pun supaya pesan errornya jujur dan tidak menyesatkan.
    if is_captcha_shortener(u) or is_captcha_shortener(final) or "h-captcha" in raw or "g-recaptcha" in raw:
        log.warning(
            "Anichin shortener requires captcha (cannot be bypassed via HTTP) | url=%s host=%s",
            u, host or urlsplit(u).hostname,
        )
        return ""

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


def is_captcha_shortener(url: str) -> bool:
    """True kalau URL shortener-nya butuh captcha (tujuan tak bisa di-resolve HTTP)."""
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in CAPTCHA_SHORTENER_HOSTS)


def label_height(label) -> int:
    """Tinggi (int) dari label resolusi apa pun: `1080p`, `1080`, `4K`, `Multi`->0."""
    text = str(label or "").strip().lower()
    if text in ("4k", "2160p"):
        return 2160
    m = re.search(r"(\d{3,4})", text)
    return int(m.group(1)) if m else 0


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
        log.warning("Anichin mirstats endpoint not found | %s", files_url)
        return []

    api = "https://www.mirrored.to/mirstats.php?" + html_mod.unescape(m.group(1))
    body = ""
    for attempt in range(max(1, MIRSTATS_MAX_POLLS)):
        code2, body, _ = _get(api, referer=files_url)
        if code2 == 200 and body and 'id="done"' in body:
            break
        time.sleep(MIRSTATS_POLL_DELAY)

    if not body:
        log.warning("Anichin mirstats empty | api=%s", api[:110])
        return []

    soup = BeautifulSoup(body, "html.parser")
    hosts = []
    for tr in soup.select("tr"):
        form = tr.select_one("form")
        if not form:
            continue
        img = tr.select_one("img")
        host = (img.get("alt") if img else "") or ""
        # FilesDLi (`filesdl.ink`) adalah host mati (cuma mengembalikan HTML
        # landing page, bukan file). Abaikan supaya tidak memicu error palsu.
        if host.lower() in ("filesdli", "filesdl"):
            continue
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
    """POST `/out_url` -> ambil parameter `url=` (link file host asli).

    Beberapa host (mis. FilesDLi) tidak memakai `/out_url` melainkan halaman
    perantara berisi form `POST /dl_go.php?fileid=<uid>`; halaman itu ditangani
    di sini juga supaya tidak balik kosong.
    """
    action = host_entry.get("action") or "/out_url"
    if action.startswith("/"):
        action = "https://www.mirrored.to" + action
    try:
        r = _http().post(
            action,
            data=host_entry.get("fields") or {},
            headers={
                "User-Agent": UA,
                "Referer": _header_safe(referer) or "https://www.mirrored.to/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except Exception as e:
        log.warning("Anichin /out_url failed | host=%s err=%r", host_entry.get("host"), e)
        return ""

    body = html_mod.unescape(r.text or "")
    m = re.search(r"URL=([^\s\"'>]+)", body)
    target = m.group(1) if m else ""
    if not target:
        loc = r.headers.get("Location") or ""
        if loc:
            target = html_mod.unescape(loc)
    if target:
        if "url=" in target:
            real = target.split("url=", 1)[1].split("&")[0]
            return html_mod.unescape(real)
        return target

    # Halaman perantara (FilesDLi): form `POST /dl_go.php?fileid=<uid>`.
    soup = BeautifulSoup(body, "html.parser")
    for form in soup.select("form[action]"):
        fa = html_mod.unescape(form.get("action") or "")
        if "dl_go.php" not in fa:
            continue
        if fa.startswith("//"):
            fa = "https:" + fa
        elif fa.startswith("/"):
            fa = "https://www.mirrored.to" + fa
        data = {}
        for i in form.select("input[name]"):
            if (i.get("type") or "text").lower() == "hidden":
                data[i.get("name")] = i.get("value") or ""
        try:
            r2 = _http().post(
                fa, data=data,
                headers={
                    "User-Agent": UA,
                    "Referer": _header_safe(action),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=HTTP_TIMEOUT, allow_redirects=False,
            )
        except Exception as e:
            log.warning("Anichin dl_go failed | host=%s err=%r", host_entry.get("host"), e)
            return ""
        loc = html_mod.unescape(r2.headers.get("Location") or "")
        if loc:
            return loc
        b2 = html_mod.unescape(r2.text or "")
        m2 = re.search(r"URL=([^\s\"'>]+)", b2)
        if m2:
            t2 = m2.group(1)
            if "url=" in t2:
                return html_mod.unescape(t2.split("url=", 1)[1].split("&")[0])
            return t2
        # Rantai kedua: halaman `dl_go` mengembalikan form ke host akhir
        # (mis. `//filesdl.ink/download.php?fileid=<uid>`). Ikuti sekali lagi.
        soup2 = BeautifulSoup(b2, "html.parser")
        for form2 in soup2.select("form[action]"):
            fa2 = html_mod.unescape(form2.get("action") or "")
            if not fa2:
                continue
            if fa2.startswith("//"):
                fa2 = "https:" + fa2
            elif fa2.startswith("/"):
                fa2 = "https://www.mirrored.to" + fa2
            data2 = {
                i.get("name"): (i.get("value") or "")
                for i in form2.select("input[name]")
                if (i.get("type") or "text").lower() == "hidden"
            }
            try:
                r3 = _http().post(
                    fa2, data=data2,
                    headers={
                        "User-Agent": UA,
                        "Referer": _header_safe(action),
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=HTTP_TIMEOUT, allow_redirects=True,
                )
            except Exception as e:
                log.warning("Anichin dl_go stage2 failed | host=%s err=%r", host_entry.get("host"), e)
                return fa2
            # Host akhir biasanya mengembalikan halaman unduh; URL-nya sendiri
            # sudah cukup untuk diunduh (server menerima POST/GET form-nya).
            return str(r3.url or fa2)
    return ""


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

    # Cocokkan label dengan toleransi (resolusi "1080" vs "1080p" vs "1080").
    def _num(x):
        m = re.search(r"(\d+)", str(x or ""))
        return int(m.group(1)) if m else 0

    want = _num(label)
    chosen_files = None
    if want:
        for f in files_list:
            if _num(f.get("label")) == want:
                chosen_files = f
                break
    if not chosen_files and label:
        for f in files_list:
            if (f.get("label") or "").lower() == str(label).lower():
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


def resolve_provider(provider_url: str, label: str = "", referer: str = "") -> list[dict]:
    """Satu provider mirror -> kandidat file.

    Mendukung dua bentuk:
    - shortener `mirrored.to` (bit.ly/inshorturl/lifehei) -> rantai `resolve_all_candidates`.
    - link file host langsung (mis. `pixeldrain.com/u/<id>`) -> kandidat tunggal.
    """
    u = (provider_url or "").strip()
    if not u:
        return []

    host = (urlsplit(u).hostname or "").lower()

    # Shortener captcha (Clicksfly/hCaptcha): tujuan tidak ada di HTML.
    # Jangan dianggap file langsung — kalau tidak, downloader mengunduh halaman
    # captcha dan melaporkan "host mengembalikan HTML" yang menyesatkan.
    if is_captcha_shortener(u):
        return []

    # Pixeldrain: URL-nya sudah file langsung (tanpa shortener).
    m = re.search(r"pixeldrain\.com/u/([A-Za-z0-9]+)", u)
    if m:
        pid = m.group(1)
        return [{
            "host": "Pixeldrain",
            "url": f"https://pixeldrain.com/api/file/{pid}",
            "label": label,
            "name": "Pixeldrain",
            "files_url": u,
        }]

    if any(h in host for h in ("mirrored.to", "bit.ly", "bitly.com", "inshorturl", "lifehei")):
        return resolve_all_candidates(u, label, referer=referer)

    # Shortener safelink (pndk.to/SafelinkEarn), link Telegram promosi, serta
    # host aplikasi/mati (Terabox/1024tera butuh login/app, FilesDLi mati) tidak
    # menuju file langsung: jangan dianggap file (kalau tidak, downloader akan
    # mengunduh HTML-nya lalu gagal "mengembalikan HTML, bukan video").
    if any(h in host for h in (
        "pndk.to", "safelinkearn", "t.me", "telegram.me", "telegram.dog",
        "terabox", "1024tera", "teraboxapp.com", "filesdl",
    )):
        return []

    # Host file yang SUDAH dikenal -> URL langsung (tidak lewat rantai shortener).
    if any(h in host for h in (
        "pixeldrain.com", "buzzheavier.com", "gofile.io", "strwish.com", "streamwish",
        "streamhg", "drive.google", "megaup.net",
        "turbobit.net", "send.now", "dsvplay", "dood.", "d.tube", "odysee.com",
        "ok.ru", "rumble.com", "dailymotion",
    )):
        return [{
            "host": host or "Direct",
            "url": u,
            "label": label,
            "name": "Direct",
            "files_url": u,
        }]

    # Sisanya: anggap SHORTENER generik (`tinyurl.com`, `shrinkme.click`, dst.)
    # dan coba rantai resolusi yang sama dengan `mirrored.to`. Ini penting karena
    # Anichin memakai shortener yang terus berganti.
    got = resolve_all_candidates(u, label, referer=referer)
    if got:
        return got
    # Shortener mati / tidak menghasilkan apa pun -> URL apa adanya sebagai
    # percobaan terakhir (host lain mungkin menerimanya langsung).
    return [{
        "host": host or "Direct",
        "url": u,
        "label": label,
        "name": "Direct",
        "files_url": u,
    }]


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


def _filesdl_direct(url: str, referer: str = "") -> str:
    """FilesDLi (`filesdl.ink/download.php?fileid=…`).

    Butuh POST form `fn`+`cn`, balasannya meta-refresh ke host file akhir
    (`https://new-file-*.sbs/?…&file=…`). -> URL file, atau "" kalau gagal.
    """
    import re as _re

    body = ""
    for payload in ({"fn": "File", "cn": "SG"}, None):
        try:
            if payload:
                r = _http().post(url, data=payload, headers={
                    "User-Agent": UA,
                    "Referer": _header_safe(referer) or "https://www.mirrored.to/",
                    "Content-Type": "application/x-www-form-urlencoded",
                }, timeout=HTTP_TIMEOUT, allow_redirects=True)
            else:
                r = _http().get(url, headers={
                    "User-Agent": UA,
                    "Referer": _header_safe(referer) or "https://www.mirrored.to/",
                }, timeout=HTTP_TIMEOUT, allow_redirects=True)
        except Exception as e:
            log.warning("FilesDLi fetch failed | url=%s err=%r", url, e)
            continue
        body = html_mod.unescape(r.text or "")
        if body:
            break
    if not body:
        return ""
    m = _re.search(r'URL=([^"\'>\s]+)', body, _re.I)
    if m:
        target = m.group(1).strip()
        if target.startswith("//"):
            target = "https:" + target
        return target
    return ""


def parse_stream_servers(html: str) -> list[dict]:
    """`select.mirror` -> daftar server streaming.

    Tiap `<option>` berisi base64 yang mendekode ke `<iframe ... SRC="...">`.
    -> [{label, url}]
    """
    import base64

    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.select_one("select.mirror")
    if not sel:
        return []
    out = []
    seen = set()
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
        u = m.group(1).strip()
        if not u or u.startswith("data:") or u in seen:
            continue
        # Embed kadang memakai URL protocol-relative (`//listeamed.net/e/...`);
        # tanpa skema, curl_cffi menolak dengan `InvalidURL: No host part`.
        if u.startswith("//"):
            u = "https:" + u
        seen.add(u)
        out.append({"label": _clean(opt.get_text(" ", strip=True)) or "server", "url": u})
    return out


def resolve_rumble(embed_url: str, target_height: int = 720) -> dict:
    """Rumble embed -> {kind: 'hls', segments, height, bandwidth, duration, title, poster, referer, label}.

    Rumble menyediakan endpoint JSON `embedJS/u3/?request=video&ver=2&v=<vid>`
    tanpa API key dan tanpa yt-dlp. Master playlist HLS-nya memuat segmen `.ts`
    standar yang bisa diunduh paralel via `_download_segments_parallel`.
    """
    m = re.search(r"rumble\.com/embed/([a-zA-Z0-9_-]+)", embed_url or "")
    if not m:
        return {}
    vid = m.group(1)
    api = f"https://rumble.com/embedJS/u3/?request=video&ver=2&v={vid}"
    code, raw, _ = _get(api, referer="https://rumble.com/")
    if code != 200 or not raw:
        return {}
    try:
        data = json.loads(raw) or {}
    except Exception:
        return {}
    u_obj = data.get("u") or {}
    hls_info = u_obj.get("hls") or {}
    hls_url = hls_info.get("url")
    if not hls_url:
        # Sebagian video Rumble tidak punya HLS, cuma MP4 progresif (`u.mp4`).
        # Kembalikan sebagai `direct_url` supaya downloader memakai jalur
        # unduhan langsung (ffmpeg `-i <url>`).
        mp4_info = u_obj.get("mp4") or {}
        mp4_url = mp4_info.get("url")
        if not mp4_url:
            return {}
        meta = mp4_info.get("meta") or {}
        return {
            "kind": "hls",
            "direct_url": mp4_url,
            "segments": [],
            "height": meta.get("h") or 0,
            "bandwidth": (meta.get("bitrate") or 0) * 1000,
            "duration": int(float(data.get("duration") or 0)),
            "title": data.get("title") or "",
            "poster": data.get("i") or "",
            "referer": "https://rumble.com/",
            "label": f"Rumble {meta.get('h') or 0}p",
        }

    code_hls, raw_hls, _ = _get(hls_url, referer="https://rumble.com/")
    if code_hls != 200 or not raw_hls:
        return {}

    from urllib.parse import urljoin
    variants = []
    lines = [l.strip() for l in raw_hls.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            hm = re.search(r"RESOLUTION=\d+x(\d+)", line)
            h = int(hm.group(1)) if hm else 0
            bm = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(bm.group(1)) if bm else 0
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                v_url = urljoin(hls_url, lines[i + 1])
                variants.append({"height": h, "bandwidth": bw, "url": v_url})

    if not variants:
        return {}

    variants.sort(key=lambda v: abs(v["height"] - (target_height or 720)))
    chosen = variants[0]

    code_media, raw_media, _ = _get(chosen["url"], referer="https://rumble.com/")
    if code_media != 200 or not raw_media:
        return {}

    init, segs = _hls_track(raw_media, chosen["url"])

    if not segs:
        return {}

    return {
        "kind": "hls",
        "segments": segs,
        "init": init,
        "height": chosen["height"],
        "bandwidth": chosen["bandwidth"],
        "duration": int(float(data.get("duration") or 0)),
        "title": data.get("title") or "",
        "poster": data.get("i") or "",
        "referer": "https://rumble.com/",
        "label": f"Rumble {chosen['height']}p",
    }


def resolve_dailymotion(embed_url: str, target_height: int = 720) -> dict:
    """Dailymotion embed/player -> {kind:'hls', segments, height, bandwidth, duration, title, poster, referer, label}.

    Halaman `player.nunadrama.sbs` (label "Dailymotion [ADS]") menyematkan
    `geo.dailymotion.com/player/<hash>.html?video=<id>`. Metadata video diambil
    dari `https://www.dailymotion.com/player/metadata/video/<id>` (JSON tanpa
    API key) yang memuat URL master HLS (`qualities.auto[0].url`). Master diikuti
    ke varian resolusi terdekat (H.264 diprioritaskan) lalu diurai jadi segmen
    (TANPA enkripsi, tanpa yt-dlp).

    PENTING: Dailymotion kini menyajikan **fMP4** (`#EXT-X-MAP:URI="init.mp4"`
    + fragmen `.m4s`) dengan **audio sebagai rendition terpisah**
    (`#EXT-X-MEDIA:TYPE=AUDIO`). Hasilnya memuat `init`, `segments`,
    `audio_init`, dan `audio_segments` supaya downloader bisa menyatukan track
    video + audio jadi MP4 yang valid.
    """
    m = re.search(
        r"(?:dailymotion\.com/(?:video|embed/video)/|[?&]video=)([a-zA-Z0-9]+)",
        embed_url or "",
    )
    if not m:
        return {}
    vid = m.group(1)
    api = f"https://www.dailymotion.com/player/metadata/video/{vid}"
    code, raw, _ = _get(api, referer="https://geo.dailymotion.com/")
    if code != 200 or not raw:
        return {}
    try:
        data = json.loads(raw) or {}
    except Exception:
        return {}

    master = ""
    qualities = data.get("qualities") or {}
    auto = qualities.get("auto") or []
    if auto and isinstance(auto, list) and auto[0].get("url"):
        master = auto[0]["url"]
    if not master:
        return {}

    referer = "https://geo.dailymotion.com/"
    from urllib.parse import urljoin
    code_m, raw_m, _ = _get(master, referer=referer)
    if code_m != 200 or not raw_m:
        return {}

    # Master bisa langsung berisi segmen, atau memuat varian (#EXT-X-STREAM-INF).
    # Varian video + grup audio terpisah (`#EXT-X-MEDIA:TYPE=AUDIO`) seperti pada
    # HLS fMP4 Dailymotion: video `.m4s` tidak memuat audio sama sekali.
    variants = []
    audio_groups: dict[str, str] = {}
    lines = [l.strip() for l in raw_m.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line:
            gm = re.search(r'GROUP-ID="([^"]+)"', line)
            um = re.search(r'URI="([^"]+)"', line)
            if gm and um:
                audio_groups[gm.group(1)] = urljoin(master, um.group(1))
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            hm = re.search(r"RESOLUTION=\d+x(\d+)", line)
            h = int(hm.group(1)) if hm else 0
            bm = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(bm.group(1)) if bm else 0
            am = re.search(r'AUDIO="([^"]+)"', line)
            cm = re.search(r'CODECS="([^"]+)"', line)
            codecs = (cm.group(1) if cm else "").lower()
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                variants.append({
                    "height": h,
                    "bandwidth": bw,
                    "url": urljoin(master, lines[i + 1]),
                    "audio": am.group(1) if am else "",
                    # Utamakan H.264 (avc1): AV1 (av01) tidak diputar di semua
                    # klien Telegram. Dipakai sebagai tie-breaker saat tinggi sama.
                    "h264": 1 if "avc1" in codecs else 0,
                })

    if variants:
        # Pilih varian terdekat target; kalau seri, prioritaskan H.264.
        variants.sort(key=lambda v: (abs(v["height"] - (target_height or 720)), -v["h264"]))
        chosen = variants[0]
        media_url = chosen["url"]
        height = chosen["height"]
        bandwidth = chosen["bandwidth"]
        audio_url = audio_groups.get(chosen.get("audio") or "") or ""
        code_media, raw_media, _ = _get(media_url, referer=referer)
        if code_media != 200 or not raw_media:
            return {}
    else:
        media_url = master
        raw_media = raw_m
        height = 0
        bandwidth = 0
        audio_url = ""

    init, segs = _hls_track(raw_media, media_url)

    # Track audio terpisah (fMP4 Dailymotion). Tanpa ini video bisu.
    audio_init, audio_segs = "", []
    if audio_url:
        code_a, raw_a, _ = _get(audio_url, referer=referer)
        if code_a == 200 and raw_a:
            audio_init, audio_segs = _hls_track(raw_a, audio_url)

    if not segs:
        return {}

    return {
        "kind": "hls",
        "segments": segs,
        "init": init,
        "audio_segments": audio_segs,
        "audio_init": audio_init,
        "height": height,
        "bandwidth": bandwidth,
        "duration": int(float(data.get("duration") or 0)),
        "title": data.get("title") or "",
        "poster": data.get("thumbnail_720_url") or "",
        "referer": referer,
        "label": f"Dailymotion {height}p" if height else "Dailymotion",
    }


def resolve_stream_source(servers: list[dict], episode_url: str = "") -> dict:
    """Cari sumber streaming yang bisa diunduh langsung dari daftar server.

    Urutan: OK.ru (paling andal, ~93% episode) -> Rumble (HLS tanpa yt-dlp) ->
    Dailymotion (metadata JSON -> master HLS) -> keluarga VidHide/StreamWish
    (unpack packer Dean Edwards -> `master.m3u8`). Server lain (RPMShare/Odysee/
    D-Tube/...) butuh yt-dlp atau punya proteksi — sengaja dilewati (repo
    melarang yt-dlp).

    -> {kind: "okru"|"hls", host, embed, ...} atau {}
    """
    # 1. OK.ru.
    for s in servers or []:
        u = (s.get("url") or "").strip()
        if not u:
            continue
        if re.search(r"ok\.ru/(?:videoembed|video|live)/\d+", u):
            try:
                got = resolve_okru(u)
            except Exception as e:
                log.warning("OK.ru resolve failed | url=%s err=%r", u, e)
                continue
            if got and got.get("variants"):
                got["kind"] = "okru"
                got["embed"] = u
                got["label"] = s.get("label") or "OK.ru"
                return got

    # 2. Rumble (HLS manifest langsung tanpa yt-dlp).
    for s in servers or []:
        u = (s.get("url") or "").strip()
        if not u or "rumble.com" not in u:
            continue
        try:
            got = resolve_rumble(u)
        except Exception as e:
            log.warning("Rumble resolve failed | url=%s err=%r", u, e)
            continue
        if got and (got.get("segments") or got.get("direct_url")):
            got["embed"] = u
            return got

    # 3. Dailymotion (label "Dailymotion [ADS]" -> player.nunadrama.sbs ->
    #    geo.dailymotion.com; metadata JSON memuat master HLS).
    for s in servers or []:
        u = (s.get("url") or "").strip()
        if not u:
            continue
        low = u.lower()
        if "dailymotion" not in low and "nunadrama" not in low:
            continue
        try:
            got = resolve_dailymotion(u)
        except Exception as e:
            log.warning("Dailymotion resolve failed | url=%s err=%r", u, e)
            continue
        if got and got.get("segments"):
            got["embed"] = u
            return got

    # 4. Keluarga VidHide/StreamWish (packer -> m3u8, segmen .ts tanpa enkripsi).
    for s in servers or []:
        u = (s.get("url") or "").strip()
        if not u:
            continue
        host = (urlsplit(u).hostname or "").lower()
        if not any(h in host for h in VIDHIDE_HOSTS):
            continue
        try:
            got = resolve_embed_hls(u)
        except Exception as e:
            log.warning("VidHide resolve failed | url=%s err=%r", u, e)
            continue
        if got and got.get("segments"):
            got["kind"] = "hls"
            got["embed"] = u
            got["label"] = s.get("label") or "StreamWish"
            return got
    return {}


def resolve_embed_hls(embed_url: str) -> dict:
    """Embed keluarga VidHide/StreamWish -> daftar segmen `.ts`.

    Halaman embed memuat Dean Edwards packer; setelah di-unpack muncul URL
    `master.m3u8`. Master diikuti ke varian resolusi TERTINGGI, lalu playlist
    media diurai jadi segmen absolut (umumnya TANPA `#EXT-X-KEY`).

    -> {host, media_url, segments, height, bandwidth, duration, referer}
    """
    code, raw, final = _get(embed_url)
    if code != 200 or not raw:
        return {}

    text = raw
    if "eval(function(p,a,c,k,e,d" in raw:
        try:
            from handlers.dl.retrotube.packer import _PACKER_RE, _unpack_eval
            m = _PACKER_RE.search(raw)
            if m:
                text = _unpack_eval(m.group(1), int(m.group(2)), int(m.group(3)),
                                    m.group(4).split("|"))
        except Exception as e:
            log.debug("VidHide unpack failed | url=%s err=%r", embed_url, e)

    m3 = re.search(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*', text)
    if not m3:
        return {}
    master = m3.group(0)
    referer = "https://" + (urlsplit(final).hostname or urlsplit(embed_url).hostname or "")

    media, height, bandwidth = _media_playlist(master, referer, timeout=VIDHIDE_TIMEOUT)
    segs = (media or {}).get("segments") or []
    if not segs:
        return {}
    return {
        "host": urlsplit(master).hostname or "cdn",
        "media_url": media.get("media_url") or "",
        "init": (media or {}).get("init") or "",
        "segments": segs,
        "height": height,
        "bandwidth": bandwidth,
        "duration": media.get("duration") or 0,
        "referer": referer,
    }


def _hls_track(manifest: str, base_url: str) -> tuple[str, list[str]]:
    """Playlist media -> (init_segment, [segment absolut]).

    Menangani HLS MPEG-TS (tanpa init) maupun fMP4 (`#EXT-X-MAP:URI="init.mp4"`
    lalu fragmen `.m4s`). `init` WAJIB dikembalikan untuk fMP4 — tanpa itu file
    hasil concat tidak punya `moov`/`tfhd` dan ffmpeg menolaknya.
    """
    from urllib.parse import urljoin

    init = ""
    segs = []
    for line in (manifest or "").splitlines():
        line = line.strip()
        if not line:
            continue
        mm = re.match(r'#EXT-X-MAP:URI="([^"]+)"', line)
        if mm:
            init = urljoin(base_url, mm.group(1))
            continue
        if line.startswith("#"):
            continue
        segs.append(urljoin(base_url, line))
    return init, segs


def _media_playlist(m3u8_url: str, referer: str = "", timeout: float | None = None) -> tuple[dict, int, int]:
    """Master/media playlist -> (media_dict, height, bandwidth).

    Master (`#EXT-X-STREAM-INF`) -> ikuti varian resolusi TERTINGGI (rekursif).
    Playlist media -> segmen absolut + total durasi.
    """
    from urllib.parse import urljoin

    code, raw, final = _get(m3u8_url, referer=referer, timeout=timeout)
    if code != 200 or not raw:
        return {}, 0, 0
    lines = [l.strip() for l in raw.splitlines()]

    best = None
    for idx, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        rm = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bm = re.search(r"BANDWIDTH=(\d+)", line)
        path = ""
        for nxt in lines[idx + 1:]:
            if nxt and not nxt.startswith("#"):
                path = nxt
                break
        if not path:
            continue
        h = int(rm.group(2)) if rm else 0
        bw = int(bm.group(1)) if bm else 0
        if best is None or h > best[1]:
            best = (path, h, bw)
    if best:
        sub, _, _ = _media_playlist(urljoin(m3u8_url, best[0]), referer, timeout=timeout)
        return sub, best[1], best[2]

    segs = []
    durs = []
    init = ""
    for line in lines:
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r"#EXTINF\s*:\s*([\d.]+)", line)
            if m:
                durs.append(float(m.group(1)))
            continue
        mm = re.match(r'#EXT-X-MAP:URI="([^"]+)"', line)
        if mm:
            init = urljoin(final or m3u8_url, mm.group(1))
            continue
        if line.startswith("#"):
            continue
        segs.append(urljoin(final or m3u8_url, line))
    if not segs:
        return {}, 0, 0
    return (
        {
            "media_url": final or m3u8_url,
            "init": init,
            "segments": segs,
            "duration": int(round(sum(durs))) if durs else 0,
        },
        0,
        0,
    )


def _okru_dims(hls_url: str) -> dict:
    """`hlsManifestUrl` OK.ru -> {quality_name: {width, height, bandwidth, path}}.

    Manifest memuat `#EXT-X-STREAM-INF:...,BANDWIDTH=5482080,QUALITY=full,RESOLUTION=1920x818`
    diikuti baris path playlist media. Tanpa ini, API `videos[]` cuma memberi nama
    kualitas (mobile/lowest/low/sd/hd/full) tanpa dimensi, sehingga picker tidak
    bisa memetakan ke tinggi nyata. `bandwidth` dipakai untuk mengestimasi total
    byte (`duration * bandwidth / 8`) supaya progress bar Telegram punya penyebut.
    """
    if not hls_url:
        return {}
    code, raw, _ = _get(hls_url)
    if code != 200 or not raw:
        return {}
    dims = {}
    lines = raw.splitlines()
    for idx, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = line
        q = re.search(r"QUALITY=([A-Za-z0-9_]+)", attrs)
        r = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
        b = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", attrs)
        if not (q and r):
            continue
        path = ""
        for nxt in lines[idx + 1:]:
            if nxt.strip() and not nxt.strip().startswith("#"):
                path = nxt.strip()
                break
        dims[q.group(1).lower()] = {
            "width": int(r.group(1)),
            "height": int(r.group(2)),
            "bandwidth": int(b.group(1)) if b else 0,
            "path": path,
        }
    return dims


def resolve_okru(embed_url: str) -> dict:
    """OK.ru embed -> varian video progresif (tanpa yt-dlp).

    `https://ok.ru/videoembed/<mid>` -> POST `dk?cmd=videoPlayerMetadata&mid=<mid>`
    -> JSON memuat `videos[]` (URL MP4 progresif per kualitas) + `hlsManifestUrl`
    (untuk dimensi nyata tiap kualitas). CDN OK.ru (`ok*.vkuser.net` /
    `vd*.okcdn.ru`) melayani `video/mp4` tanpa captcha.

    -> {host, variants:[{name,url,width,height}], title, duration, poster, referer}
    """
    m = re.search(r"ok\.ru/(?:videoembed|video|live)/(\d+)", embed_url or "")
    if not m:
        return {}
    mid = m.group(1)
    try:
        r = _http().post(
            f"https://ok.ru/dk?cmd=videoPlayerMetadata&mid={mid}",
            data={},
            headers={"User-Agent": UA, "Referer": "https://ok.ru/"},
            timeout=HTTP_TIMEOUT,
        )
        data = r.json() or {}
    except Exception as e:
        log.warning("OK.ru metadata failed | mid=%s err=%r", mid, e)
        return {}

    dims = _okru_dims(data.get("hlsManifestUrl") or "")
    hls_master = (data.get("hlsManifestUrl") or "").strip()
    variants = []
    for v in data.get("videos") or []:
        name = (v.get("name") or "").strip().lower()
        u = (v.get("url") or "").strip()
        if not u or v.get("disallowed"):
            continue
        d = dims.get(name) or {}
        variants.append({
            "name": name,
            "url": u,
            "width": d.get("width") or 0,
            "height": d.get("height") or 0,
            "bandwidth": d.get("bandwidth") or 0,
            "hls_path": d.get("path") or "",
        })
    # Yang dimensinya tak diketahui ditaruh paling belakang supaya tidak menang
    # saat pemilihan "kualitas tertinggi".
    variants.sort(key=lambda x: (x.get("height") or 0))
    if not variants:
        return {}

    movie = data.get("movie") or {}
    try:
        duration = int(float(movie.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    return {
        "host": "OK.ru",
        "variants": variants,
        "hls_master": hls_master,
        "title": (movie.get("title") or "").strip(),
        "duration": duration,
        "poster": (movie.get("poster") or "").strip(),
        "referer": "https://ok.ru/",
    }


def okru_segments(variant: dict, hls_master: str) -> dict:
    """Playlist media OK.ru -> {media_url, segments:[url absolut], duration, bytes}.

    Segmen `.ts` bisa diunduh paralel (jauh lebih cepat daripada ffmpeg serial
    ~1.1 MB/s). `bytes` = jumlah `Content-Length` bila ada, dipakai progress.
    """
    from urllib.parse import urljoin

    path = (variant or {}).get("hls_path") or ""
    if not path or not hls_master:
        return {}
    media_url = urljoin(hls_master, path)
    code, raw, _ = _get(media_url)
    if code != 200 or not raw:
        return {}
    segs = []
    durs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r"#EXTINF\s*:\s*([\d.]+)", line)
            if m:
                durs.append(float(m.group(1)))
            continue
        if line.startswith("#"):
            continue
        segs.append(urljoin(media_url, line))
    if not segs:
        return {}
    return {
        "media_url": media_url,
        "segments": segs,
        "duration": int(round(sum(durs))) if durs else 0,
    }


def pick_okru_variant(variants: list[dict], want_height: int = 0) -> dict | None:
    """Pilih varian OK.ru paling dekat dengan tinggi yang diminta.

    Kalau `want_height` 0 -> ambil kualitas tertinggi yang diketahui dimensinya.
    """
    usable = [v for v in variants if (v.get("height") or 0) > 0]
    if not usable:
        return variants[-1] if variants else None
    if not want_height:
        return usable[-1]
    return min(usable, key=lambda v: abs((v.get("height") or 0) - want_height))


def resolve_direct_file(file_url: str, referer: str = "") -> str:
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
    if "filesdl" in host:
        return _filesdl_direct(file_url, referer=referer) or file_url
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
