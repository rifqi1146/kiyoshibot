import logging
import math
import re
import time
from urllib.parse import quote

from bs4 import BeautifulSoup
from curl_cffi import CurlOpt, requests as curl_requests

from handlers.dl.nekopoi.constants import FORCE_IPV4

from .constants import (
    AZ_TTL,
    BASE_URL,
    HTTP_TIMEOUT,
    MAX_RESULTS,
    UA,
)
from .state import AZ_CACHE, GENRES_CACHE, SCHEDULE_CACHE

log = logging.getLogger(__name__)

_session = None


def _http():
    global _session
    if _session is None:
        if FORCE_IPV4:
            _session = curl_requests.Session(
                impersonate="chrome",
                curl_options={CurlOpt.IPRESOLVE: 1},
            )
        else:
            _session = curl_requests.Session(impersonate="chrome")
    return _session


def http_get(url: str) -> str:
    try:
        r = _http().get(url, headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"}, timeout=HTTP_TIMEOUT)
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        log.warning("Nekopoi HTTP request failed | url=%s err=%r", url, e)
        return ""


def _bg(style: str) -> str:
    m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style or "")
    return m.group(1) if m else ""


def parse_cards(html_text: str) -> list[dict]:
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    seen = set()
    for li in soup.select("div.nk-search-results ul li"):
        a = li.find("a", class_="nk-search-item")
        if not a or not a.get("href"):
            continue
        href = a["href"].strip()
        if href in seen:
            continue
        seen.add(href)

        h2 = li.find("h2")
        desc = li.find("p", class_="nk-search-desc")
        gspan = li.find("span", class_="nk-search-genres")
        thumb = li.find("div", class_="nk-search-thumb")

        title = " ".join((h2.get_text(" ", strip=True) if h2 else "Unknown title").split())
        path = a["href"].split("nekopoi.care", 1)[-1].split("?")[0]
        is_series = bool(re.match(r"^/(?:hentai|jav)/[^/]+/?$", path))
        out.append({
            "title": title or "Unknown title",
            "url": href,
            "thumbnail": _bg(thumb.get("style") if thumb else ""),
            "genres": " ".join((gspan.get_text(" ", strip=True) if gspan else "").split()),
            "desc": " ".join((desc.get_text(" ", strip=True) if desc else "").split()),
            "type": "series" if is_series else "post",
        })
    return out


def fetch_paginated(url_template: str, max_items: int = MAX_RESULTS) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    max_pages = max(1, math.ceil(max_items / 10) + 1)
    for page in range(1, max_pages + 1):
        cards = parse_cards(http_get(url_template.format(page=page)))
        if not cards:
            break
        fresh = 0
        for card in cards:
            url = card.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(card)
            fresh += 1
            if len(results) >= max_items:
                break
        if fresh == 0 or len(results) >= max_items:
            break
    return results[:max_items]


def search(query: str, max_items: int = MAX_RESULTS) -> list[dict]:
    slug = quote((query or "").strip(), safe="")
    first = f"{BASE_URL}/search/{slug}"
    raw = http_get(first)
    if not raw:
        return []
    cards = parse_cards(raw)
    if not cards:
        return []
    if len(cards) >= max_items:
        return cards[:max_items]

    results = list(cards)
    seen = {c["url"] for c in results}
    max_pages = max(2, math.ceil(max_items / 10) + 1)
    for page in range(2, max_pages + 1):
        more = parse_cards(http_get(f"{first}/page/{page}/"))
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


def fetch_genres() -> list[dict]:
    now = time.time()
    if GENRES_CACHE.get("list") and now - GENRES_CACHE.get("ts", 0) < 3600:
        return GENRES_CACHE["list"]

    raw = http_get(f"{BASE_URL}/genre-list/")
    if not raw:
        return GENRES_CACHE.get("list") or []

    soup = BeautifulSoup(raw, "html.parser")
    genres = []
    seen = set()
    for a in soup.select("a[href*='/genres/']"):
        m = re.search(r"/genres/([^/]+)/", a.get("href") or "")
        if not m:
            continue
        slug = m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)
        genres.append({"slug": slug, "name": a.get_text(" ", strip=True) or slug.replace("-", " ").title()})

    if genres:
        GENRES_CACHE["list"] = genres
        GENRES_CACHE["ts"] = now
    return genres


def fetch_az(kind: str) -> dict:
    now = time.time()
    cached = AZ_CACHE.get(kind)
    if cached and now - cached.get("ts", 0) < AZ_TTL:
        return cached

    path = "/hentai-list/" if kind == "hentai" else "/jav-list/"
    raw = http_get(f"{BASE_URL}{path}")
    if not raw:
        return cached or {"letters": {}, "flat": [], "ts": now}

    soup = BeautifulSoup(raw, "html.parser")
    letters: dict[str, list] = {}
    flat: list[dict] = []
    seen = set()

    for group in soup.select("div.nk-az-group"):
        letter_el = group.select_one("div.nk-az-letter")
        letter = (letter_el.get_text(strip=True) if letter_el else "").strip() or "#"
        items = []
        for item in group.select("div.nk-az-item"):
            a = item.select_one("a.nk-series-link") or item.find("a", href=True)
            if not a or not a.get("href"):
                continue
            url = a["href"].strip()
            if url in seen:
                continue
            seen.add(url)
            title = " ".join(a.get_text(" ", strip=True).split()) or "Unknown"
            items.append({"title": title, "url": url})
            flat.append({"title": title, "url": url, "letter": letter})
        if items:
            letters.setdefault(letter, []).extend(items)

    data = {"letters": letters, "flat": flat, "ts": now}
    if flat:
        AZ_CACHE[kind] = data
    return data


def fetch_schedule() -> list[dict]:
    now = time.time()
    if SCHEDULE_CACHE.get("months") and now - SCHEDULE_CACHE.get("ts", 0) < 3600:
        return SCHEDULE_CACHE["months"]

    raw = http_get(f"{BASE_URL}/jadwal-new-hentai/")
    if not raw:
        return SCHEDULE_CACHE.get("months") or []

    soup = BeautifulSoup(raw, "html.parser")
    months = []
    for sw in soup.select("div.spoiler-wrap"):
        head = sw.select_one("div.spoiler-head")
        month = " ".join((head.get_text(" ", strip=True) if head else "").split())
        items = []
        for cs in sw.select("div.coming_soon"):
            t = cs.select_one("a.title")
            ep = cs.select_one("span.episode")
            rd = cs.select_one("span.release_date")
            title = " ".join((t.get_text(" ", strip=True) if t else "").split())
            if not title:
                continue
            items.append({
                "title": title,
                "episode": (ep.get_text(" ", strip=True) if ep else "").strip(),
                "release": (rd.get_text(" ", strip=True) if rd else "").strip(),
            })
        if items:
            months.append({"month": month, "items": items})

    if months:
        SCHEDULE_CACHE["months"] = months
        SCHEDULE_CACHE["ts"] = now
    return months


def fetch_latest() -> list[dict]:
    raw = http_get(f"{BASE_URL}/")
    if not raw:
        return []
    soup = BeautifulSoup(raw, "html.parser")
    area = soup.select_one("div.nk-episodes-area")
    if not area:
        return []
    out = []
    seen = set()
    for card in area.select("div.nk-post-card"):
        a = card.select_one("h2 a") or card.find("a", href=True)
        if not a or not a.get("href"):
            continue
        url = a["href"].strip()
        if url in seen:
            continue
        seen.add(url)
        thumb = card.select_one("div.nk-thumb-crop")
        title = " ".join(a.get_text(" ", strip=True).split()) or "Unknown"
        path = url.split("nekopoi.care", 1)[-1].split("?")[0]
        is_series = bool(re.match(r"^/(?:hentai|jav)/[^/]+/?$", path))
        out.append({
            "title": title,
            "url": url,
            "thumbnail": _bg(thumb.get("style") if thumb else ""),
            "genres": "",
            "desc": "",
            "type": "series" if is_series else "post",
        })
    return out


def _clean_series_title(raw: str) -> str:
    t = raw or ""
    m = re.search(r'Unduh\s+"([^"]+)"', t)
    if m:
        return " ".join(m.group(1).split())
    return " ".join(t.split())


def _ep_number(text: str) -> int:
    m = re.search(r"(?:episode|ep)\s*(\d+)", text or "", re.I)
    return int(m.group(1)) if m else 0


def fetch_detail(url: str) -> dict:
    raw = http_get(url)
    if not raw:
        return {"kind": "unknown", "title": "", "url": url}

    soup = BeautifulSoup(raw, "html.parser")
    og_title = ""
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        og_title = og["content"].strip()

    info = soup.select_one("div.nk-series-info")
    if info:
        title = _clean_series_title(
            info.select_one("h2").get_text(" ", strip=True) if info.select_one("h2") else og_title
        )
        poster = ""
        p = info.select_one("div.nk-series-poster")
        if p:
            poster = _bg(p.get("style"))
        if not poster:
            img = info.select_one("img")
            poster = (img.get("src") if img and img.get("src") else "")

        synopsis = ""
        sp = info.select_one("span.nk-series-synopsis p")
        if sp:
            synopsis = " ".join(sp.get_text(" ", strip=True).split())

        meta = {}
        ml = info.select_one("div.nk-series-meta-list")
        for li in (ml.select("li") if ml else []):
            b = li.find("b")
            if not b:
                continue
            key = b.get_text(strip=True).rstrip(":").strip()
            val = li.get_text(" ", strip=True)
            if ":" in val:
                val = val.split(":", 1)[1]
            meta[key] = " ".join(val.split())

        episodes = []
        seen_eps = set()
        for a in soup.select("a.nk-episode-card"):
            href = (a.get("href") or "").strip()
            badge = a.select_one("span.nk-episode-badge")
            card_title = a.select_one("span.nk-episode-card-title")
            date = a.select_one("span.nk-episode-card-date")
            ep_num = _ep_number(badge.get_text(strip=True) if badge else "")
            if ep_num and ep_num in seen_eps:
                continue
            if ep_num:
                seen_eps.add(ep_num)
            episodes.append({
                "ep": ep_num,
                "title": " ".join((card_title.get_text(" ", strip=True) if card_title else "").split()),
                "url": href,
                "date": " ".join((date.get_text(" ", strip=True) if date else "").split()),
            })
        episodes.sort(key=lambda e: e["ep"] or 0)

        return {
            "kind": "series",
            "url": url,
            "title": title or og_title or "Nekopoi",
            "poster": poster,
            "synopsis": synopsis,
            "meta": meta,
            "episodes": episodes,
        }

    rows = soup.select("div.nk-download-row")
    thumb = ""
    ogi = soup.select_one('meta[property="og:image"]')
    if ogi and ogi.get("content"):
        thumb = ogi["content"].strip()
    title = og_title
    h1 = soup.find("h1")
    if not title and h1:
        title = h1.get_text(" ", strip=True)

    download_rows = []
    for row in rows:
        name_div = row.select_one("div.nk-download-name")
        links = []
        for a in row.select("div.nk-download-links a"):
            href = (a.get("href") or "").strip()
            if href:
                links.append({"label": a.get_text(" ", strip=True), "url": href})
        download_rows.append({
            "name": " ".join((name_div.get_text(" ", strip=True) if name_div else "").split()),
            "links": links,
        })

    return {
        "kind": "episode" if download_rows else "unknown",
        "url": url,
        "title": " ".join((title or "Nekopoi").split()),
        "thumbnail": thumb,
        "download_rows": download_rows,
    }
