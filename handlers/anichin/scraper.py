import base64
import logging
import math
import re
import time
from urllib.parse import quote_plus, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .constants import (
    BASE_URL,
    DAY_EN_MAP,
    HTTP_TIMEOUT,
    INDEX_TTL,
    MAX_RESULTS,
    UA,
)
from .state import AZ_CACHE, GENRES_CACHE, SCHEDULE_CACHE

log = logging.getLogger(__name__)

_session = None


def _http():
    global _session
    if _session is None:
        _session = curl_requests.Session(impersonate="chrome")
    return _session


def http_get(url: str) -> str:
    try:
        r = _http().get(url, headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"}, timeout=HTTP_TIMEOUT)
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        log.warning("Anichin HTTP request failed | url=%s err=%r", url, e)
        return ""


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def parse_cards(html_text: str) -> list[dict]:
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    seen = set()
    for art in soup.select("article.bs"):
        a = art.select_one("a.tip") or art.find("a", href=True)
        if not a or not a.get("href"):
            continue
        url = a["href"].strip()
        if url.startswith("/"):
            url = BASE_URL + url
        if url in seen:
            continue
        seen.add(url)

        tt = art.select_one("div.tt")
        h2 = art.select_one("div.tt h2")
        title = _clean(tt.get_text(" ", strip=True)) if tt else ""
        if h2:
            full = _clean(h2.get_text(" ", strip=True))
            if full:
                title = full
        if not title:
            title = _clean(a.get("title") or "") or "Unknown title"

        status_el = art.select_one("div.status")
        type_el = art.select_one("div.typez")
        ep_el = art.select_one("span.epx")
        sub_el = art.select_one("span.sb")
        img = art.select_one("img")

        out.append({
            "title": title,
            "url": url,
            "thumbnail": (img.get("src") or "").strip() if img else "",
            "status": _clean(status_el.get_text(" ", strip=True)) if status_el else "",
            "type": _clean(type_el.get_text(" ", strip=True)) if type_el else "",
            "ep": _clean(ep_el.get_text(" ", strip=True)) if ep_el else "",
            "sub": _clean(sub_el.get_text(" ", strip=True)) if sub_el else "",
        })
    return out


def _collect(first_url: str, page_tmpl: str, max_items: int) -> list[dict]:
    results = parse_cards(http_get(first_url))
    if not results:
        return []
    seen = {c["url"] for c in results}
    if len(results) >= max_items or not page_tmpl:
        return results[:max_items]

    max_pages = max(2, math.ceil(max_items / 25) + 1)
    for page in range(2, max_pages + 1):
        more = parse_cards(http_get(page_tmpl.format(page=page)))
        if not more:
            break
        fresh = 0
        for card in more:
            if card["url"] in seen:
                continue
            seen.add(card["url"])
            results.append(card)
            fresh += 1
            if len(results) >= max_items:
                break
        if fresh == 0 or len(results) >= max_items:
            break
    return results[:max_items]


def search(query: str, max_items: int = MAX_RESULTS) -> list[dict]:
    slug = quote_plus((query or "").strip())
    return _collect(f"{BASE_URL}/?s={slug}", "", max_items)


def fetch_listing(path: str, paginated: bool = True, max_items: int = MAX_RESULTS) -> list[dict]:
    path = path.rstrip("/")
    tmpl = f"{BASE_URL}{path}/page/{{page}}/" if paginated else ""
    return _collect(f"{BASE_URL}{path}/", tmpl, max_items)


def fetch_genres() -> list[dict]:
    now = time.time()
    if GENRES_CACHE.get("list") and now - GENRES_CACHE.get("ts", 0) < INDEX_TTL:
        return GENRES_CACHE["list"]

    raw = http_get(f"{BASE_URL}/genres/")
    if not raw:
        return GENRES_CACHE.get("list") or []

    soup = BeautifulSoup(raw, "html.parser")
    genres = []
    seen = set()
    for a in soup.select("a[href*='/genres/']"):
        m = re.search(r"/genres/([^/]+)/?", a.get("href") or "")
        if not m:
            continue
        slug = m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)
        label = _clean(a.get_text(" ", strip=True))
        cm = re.search(r"\s(\d+)$", label)
        count = cm.group(1) if cm else ""
        name = re.sub(r"\s\d+$", "", label).strip() or slug.replace("-", " ").title()
        genres.append({"slug": slug, "name": name, "count": count})

    if genres:
        GENRES_CACHE["list"] = genres
        GENRES_CACHE["ts"] = now
    return genres


def fetch_az() -> list[str]:
    now = time.time()
    if AZ_CACHE.get("letters") and now - AZ_CACHE.get("ts", 0) < INDEX_TTL:
        return AZ_CACHE["letters"]

    raw = http_get(f"{BASE_URL}/az-lists/")
    letters = []
    if raw:
        soup = BeautifulSoup(raw, "html.parser")
        seen = set()
        for a in soup.select("a[href*='az-lists/?show=']"):
            m = re.search(r"show=([^&]+)", a.get("href") or "")
            if not m:
                continue
            val = m.group(1).strip()
            if val in seen or val == ".":
                continue
            seen.add(val)
            letters.append(val)
    if letters:
        AZ_CACHE["letters"] = letters
        AZ_CACHE["ts"] = now
    return letters


def fetch_az_letter(letter: str, max_items: int = MAX_RESULTS) -> list[dict]:
    from urllib.parse import quote
    first = f"{BASE_URL}/az-lists/?show={quote(letter, safe='')}"
    return _collect(first, f"{BASE_URL}/az-lists/page/{{page}}/", max_items)


def fetch_schedule() -> list[dict]:
    now = time.time()
    if SCHEDULE_CACHE.get("days") and now - SCHEDULE_CACHE.get("ts", 0) < INDEX_TTL:
        return SCHEDULE_CACHE["days"]

    raw = http_get(f"{BASE_URL}/schedule/")
    if not raw:
        return SCHEDULE_CACHE.get("days") or []

    soup = BeautifulSoup(raw, "html.parser")

    labels = []
    for l in soup.select("div.listSchh h2"):
        t = _clean(l.get_text(" ", strip=True))
        if t:
            labels.append(DAY_EN_MAP.get(t.lower(), t))

    def _parse_items(block) -> list[dict]:
        items = []
        for bx in block.select("div.bsx"):
            a = bx.select_one("a[href]")
            if not a:
                continue
            tt = bx.select_one("div.tt")
            img = bx.select_one("img")
            ep_el = bx.select_one("span.epx")
            txt = _clean(bx.get_text(" ", strip=True))
            jm = re.search(r"\bJam\s+(\d{1,2}:\d{2})", txt)
            href = a["href"].strip()
            if href.startswith("/"):
                href = BASE_URL + href
            ep_raw = _clean(ep_el.get_text(" ", strip=True)) if ep_el else ""
            low_ep = ep_raw.lower()
            if "sudah rilis" in low_ep or "sudah tayang" in low_ep or "tayang" in low_ep:
                ep_txt = "Aired"
            elif re.match(r"^Jam\s+\d{1,2}:\d{2}", ep_raw):
                ep_txt = "Scheduled"
            else:
                ep_txt = ep_raw
            items.append({
                "title": _clean(tt.get_text(" ", strip=True)) if tt else txt[:80],
                "url": href,
                "thumbnail": (img.get("src") or "").strip() if img else "",
                "time": jm.group(1) if jm else "",
                "jam": jm.group(1) if jm else "",
                "ep": ep_txt,
            })
        return items

    days = []
    blocks = soup.select("div.bixbox.schedulepage")
    for i, block in enumerate(blocks):
        items = _parse_items(block)
        label = labels[i] if i < len(labels) else f"Day {i + 1}"
        if items:
            days.append({"day": label, "items": items})

    if days:
        SCHEDULE_CACHE["days"] = days
        SCHEDULE_CACHE["ts"] = now
    return days


def _parse_stream_servers(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    sel = soup.select_one("select.mirror")
    if not sel:
        return []
    servers = []
    seen = set()
    for opt in sel.select("option"):
        val = (opt.get("value") or "").strip()
        label = _clean(opt.get_text(" ", strip=True))
        if not val:
            continue
        try:
            decoded = base64.b64decode(val).decode("utf-8", "replace")
        except Exception:
            continue
        m = re.search(r'(?:SRC|src)=["\']?([^"\'\s>]+)', decoded)
        if not m:
            continue
        url = m.group(1).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        servers.append({"label": label or (urlsplit(url).hostname or "server"), "url": url})
    return servers


def _parse_download_mirrors(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    mirrors = []
    seen = set()
    for row in soup.select("div.soraurlx"):
        strong = row.find("strong")
        a = row.find("a", href=True)
        if not a:
            continue
        label = _clean(strong.get_text(" ", strip=True)) if strong else ""
        url = (a.get("href") or "").strip()
        key = (label.lower(), url)
        if not url or key in seen:
            continue
        seen.add(key)
        mirrors.append({
            "label": label,
            "name": _clean(a.get_text(" ", strip=True)) or "Mirror",
            "url": url,
        })
    return mirrors


def fetch_detail(url: str) -> dict:
    raw = http_get(url)
    if not raw:
        return {"kind": "unknown", "title": "", "url": url}

    soup = BeautifulSoup(raw, "html.parser")

    og = soup.select_one('meta[property="og:title"]')
    og_title = og.get("content").strip() if og and og.get("content") else ""

    eplister = soup.select_one("div.eplister ul")
    if eplister:
        h1 = soup.select_one("h1.entry-title") or soup.find("h1")
        title = _clean(h1.get_text(" ", strip=True)) if h1 else og_title

        poster = ""
        img = soup.select_one("div.thumb img, div.tb img")
        if img:
            poster = (img.get("src") or "").strip()

        synopsis = ""
        for sel in ("div.entry-content p", "div.synp p", "div.info-content p", "p"):
            for p in soup.select(sel):
                t = _clean(p.get_text(" ", strip=True))
                if len(t) > 80:
                    synopsis = t
                    break
            if synopsis:
                break

        meta = {}
        spe = soup.select_one("div.spe")
        if spe:
            parts = re.split(
                r"(Status|Network|Studio|Tanggal rilis|Durasi|Season|Negara|Tipe|Episode|Subber|Diposting oleh|Ditambahkan|Diperbarui pada)\s*:",
                _clean(spe.get_text(" ", strip=True)),
            )
            for i in range(1, len(parts) - 1, 2):
                key = parts[i].strip()
                val = _clean(parts[i + 1])
                val = re.split(
                    r"\s(?:Status|Network|Studio|Tanggal rilis|Durasi|Season|Negara|Tipe|Episode|Subber|Diposting oleh|Ditambahkan|Diperbarui pada)\s*:",
                    val,
                )[0].strip()
                if val:
                    meta[key] = val

        genres = []
        gx = soup.select_one("div.genxed")
        if gx:
            genres = [_clean(a.get_text(" ", strip=True)) for a in gx.select("a") if a.get_text(strip=True)]

        episodes = []
        seen_eps = set()
        for li in eplister.select("li"):
            a = li.select_one("a[href]")
            if not a:
                continue
            num = li.select_one("div.epl-num")
            ttl = li.select_one("div.epl-title")
            dt = li.select_one("div.epl-date")
            href = a["href"].strip()
            if href.startswith("/"):
                href = BASE_URL + href
            ep_txt = _clean(num.get_text(" ", strip=True)) if num else ""
            m = re.search(r"(\d+)", ep_txt)
            ep_num = int(m.group(1)) if m else 0
            if ep_num and ep_num in seen_eps:
                continue
            if ep_num:
                seen_eps.add(ep_num)
            episodes.append({
                "ep": ep_num,
                "title": _clean(ttl.get_text(" ", strip=True)) if ttl else "",
                "url": href,
                "date": _clean(dt.get_text(" ", strip=True)) if dt else "",
            })
        episodes.sort(key=lambda e: e["ep"] or 0, reverse=True)

        return {
            "kind": "series",
            "url": url,
            "title": title or og_title or "Anichin",
            "poster": poster,
            "synopsis": synopsis,
            "meta": meta,
            "genres": genres,
            "episodes": episodes,
        }

    servers = _parse_stream_servers(raw)
    mirrors = _parse_download_mirrors(raw)
    if servers or mirrors:
        h1 = soup.select_one("h1.entry-title") or soup.find("h1")
        title = _clean(h1.get_text(" ", strip=True)) if h1 else og_title
        thumb = ""
        ogi = soup.select_one('meta[property="og:image"]')
        if ogi and ogi.get("content"):
            thumb = ogi["content"].strip()
        return {
            "kind": "episode",
            "url": url,
            "title": title or og_title or "Anichin",
            "thumbnail": thumb,
            "servers": servers,
            "mirrors": mirrors,
        }

    return {"kind": "unknown", "url": url, "title": og_title}
