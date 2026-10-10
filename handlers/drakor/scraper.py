"""HTTP client + parser + fetcher untuk Drakor.id (drakorid.co).

FLOW SCRAPER
------------
1. `http_get(url)` -> HTML mentah (curl_cffi impersonate Chrome).
2. `parse_cards(html)` -> daftar kartu `.movie-list-card` (judul, url, thumb).
3. `fetch_paginated(url_template)` -> gabungkan beberapa halaman server sampai
   `MAX_RESULTS` (search / latest / kategori).
4. `fetch_categories()` -> 63 kategori dari `/kategori.html` (cache 1 jam).
5. `fetch_detail(url)` -> metadata drama dari `/nonton/<slug>/` (judul, poster,
   episode count, genres, network, release, sinopsis).
"""
import logging
import math
import re
import time
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .auth import get_auth_cookies
from .constants import BASE_URL, MAX_RESULTS, UA
from .state import CATEGORIES_CACHE

log = logging.getLogger(__name__)

_CARD_TITLE_RE = re.compile(r"title")
_TITLE_TRIM = ("Sub Indo", "Drakor.id", "Nonton", " - ")


def make_session() -> curl_requests.Session:
    """Session `curl_cffi` dengan kuki login Drakor.id bila tersedia."""
    sess = curl_requests.Session(impersonate="chrome")
    try:
        for name, value in (get_auth_cookies() or {}).items():
            sess.cookies.set(name, value, domain="drakorid.co")
    except Exception as e:
        log.debug("Drakor.id auth cookie attach failed | err=%r", e)
    return sess


def http_get(url: str) -> str:
    sess = make_session()
    try:
        resp = sess.get(url, headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"}, timeout=20)
        return resp.text if resp.status_code == 200 else ""
    except Exception as e:
        log.warning("Drakor.id HTTP request failed | url=%s err=%r", url, e)
        return ""
    finally:
        try:
            sess.close()
        except Exception:
            pass


def slug_from_url(url: str) -> str:
    m = re.search(r"drakorid\.co/(?:nonton|download-streaming|watch-streaming)/([^/?#]+)", url or "")
    return m.group(1).strip() if m else ""


def parse_cards(html_text: str) -> list[dict]:
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    cards = []
    seen = set()
    for art in soup.select(".movie-list-card"):
        a = art.find("a", href=True)
        if not a or not a.get("href"):
            continue
        href = a.get("href").strip()
        if href in seen or "/nonton/" not in href:
            continue
        seen.add(href)

        img = art.find("img")
        title_el = art.find(class_=_CARD_TITLE_RE)
        title = (
            (title_el.get("data-original-title") if title_el else None)
            or (img.get("alt") if img else None)
            or (title_el.get_text(strip=True) if title_el else "")
        )
        title = " ".join((title or "Unknown title").split()).strip()
        thumb = (img.get("src") or "") if img else ""

        cards.append({"title": title, "url": href, "thumbnail": thumb.strip()})
    return cards


def fetch_paginated(url_template: str, max_items: int = MAX_RESULTS) -> list[dict]:
    """Kumpulkan kartu dari beberapa halaman server sampai `max_items`.

    `url_template` memakai `{page}`; berhenti saat halaman kosong, semua
    duplikat, atau target tercapai. Batas keras `max_items` (default 100).
    """
    results: list[dict] = []
    seen: set[str] = set()
    max_pages = max(1, math.ceil(max_items / 30) + 1)
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


def fetch_categories() -> list[dict]:
    now = time.time()
    if CATEGORIES_CACHE["list"] and now - CATEGORIES_CACHE["ts"] < 3600:
        return CATEGORIES_CACHE["list"]

    raw = http_get(f"{BASE_URL}/kategori.html")
    if not raw:
        return CATEGORIES_CACHE["list"]

    soup = BeautifulSoup(raw, "html.parser")
    cats = []
    seen = set()
    for a in soup.select("a[href*='/kategori/']"):
        href = a.get("href") or ""
        m = re.search(r"/kategori/([^/]+)", href)
        if not m:
            continue
        slug = m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)

        raw_text = a.get_text(" ", strip=True)
        name = re.sub(r"\d+$", "", raw_text).strip() or slug.replace("-", " ").title()
        count_m = re.search(r"(\d+)$", a.get_text(strip=True))
        count = count_m.group(1) if count_m else ""
        cats.append({"slug": slug, "name": name, "count": count})

    if cats:
        CATEGORIES_CACHE["list"] = cats
        CATEGORIES_CACHE["ts"] = now
    return cats


def _clean_title(raw: str, fallback: str) -> str:
    title = raw or ""
    for token in _TITLE_TRIM:
        title = title.replace(token, "")
    return " ".join(title.split()).strip() or fallback


def fetch_detail(url: str) -> dict:
    raw = http_get(url)
    if not raw:
        return {}

    soup = BeautifulSoup(raw, "html.parser")
    raw_title = soup.title.text if soup.title else ""
    m_slug = re.search(r"/nonton/([^/]+)", url)
    fallback_title = m_slug.group(1).replace("-", " ").title() if m_slug else "Drama"

    # Halaman promo "Welcome" (butuh login / dibatasi) bukan detail drama.
    if "Welcome" in raw_title and not soup.select(".episode-pick-num, [data-episode], .poster-img"):
        return {
            "title": fallback_title,
            "poster": "",
            "synopsis": "This drama requires login or is temporarily restricted on Drakor.id.",
            "episodes": 0,
            "genres": "",
            "network": "",
            "release": "",
            "url": url,
            "restricted": True,
        }

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

    ep_count = len(soup.select(".episode-pick-num, [data-episode]"))
    raw_text = soup.get_text("\n")
    genres = re.search(r"Genres?\s*:\s*([^\n]+)", raw_text, re.I)
    network = re.search(r"Network\s*:\s*([^\n]+)", raw_text, re.I)
    release = re.search(r"Release Date\s*:\s*([^\n]+)", raw_text, re.I)

    def _field(m) -> str:
        return " ".join((m.group(1) if m else "").replace("-", " ").split()).strip()

    return {
        "title": title,
        "poster": poster,
        "synopsis": synopsis,
        "episodes": ep_count,
        "genres": _field(genres),
        "network": _field(network),
        "release": _field(release),
        "url": url,
        "restricted": False,
    }
