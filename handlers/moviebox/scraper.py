"""HTTP client and metadata parser for MovieBox (officialmoviebox.com).

FLOW SCRAPER
------------
1. Search:
   -> GET `https://officialmoviebox.com/newWeb/searchResult?keyword=<q>`
   -> Parse HTML SSR `a.card` + state JSON -> title, rating, slug, cover.
2. Trending:
   -> GET `https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/trending`
   -> tabId: `ONEROOM_MOVIE` (movies), `ONEROOM_TV` (series), omit for all.
3. Home sections:
   -> GET `https://h5-api.aoneroom.com/wefeed-h5api-bff/home?host=<host>`
   -> Pre-curated `operatingList[].subjects[]` blocks (Popular, Coming Soon,
      K-Drama, C-Drama, Anime, Action, Horror, Hot Short TV, ...).
4. Filter (by country / type / sort):
   -> POST `https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/filter`
   -> body `{page, perPage, country, subjectType, sort}`.
5. Detail:
   -> GET `https://h5-api.aoneroom.com/wefeed-h5api-bff/detail?detailPath=<slug>`
   -> title, synopsis, year, country, genres, IMDb, seasons & episodes.
6. Stream resolver:
   -> GET `.../subject/play?subjectId=<id>&se=<se>&ep=<ep>&detailPath=<slug>`
   -> Header `Referer` MUST match the video page URL.
   -> Direct MP4 streams: 1080p, 720p, 480p, 360p (H.264 + AAC, >200 MB/s CDN).
"""
import json
import logging
import re
import time
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from handlers.dl.moviebox.extractor import fetch_detail

from .constants import (
    API_BASE,
    BASE_URL,
    HTTP_TIMEOUT,
    INDEX_TTL,
    MAX_RESULTS,
    POPULAR_TITLES,
    UA,
)
from .state import HOME_CACHE, TRENDING_CACHE

log = logging.getLogger(__name__)

_session = None


def _http():
    global _session
    if _session is None:
        _session = curl_requests.Session(impersonate="chrome")
    return _session


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def http_get(url: str, referer: str = f"{BASE_URL}/") -> str:
    """Fetch HTML with standard headers."""
    try:
        r = _http().get(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/json",
                "Referer": referer,
            },
            timeout=HTTP_TIMEOUT,
        )
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        log.warning("MovieBox GET failed | url=%s err=%r", url, e)
        return ""


def http_json(url: str, referer: str = f"{BASE_URL}/") -> dict:
    """Fetch a GET JSON endpoint."""
    try:
        r = _http().get(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Referer": referer,
            },
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json() or {}
        return {}
    except Exception as e:
        log.warning("MovieBox JSON failed | url=%s err=%r", url, e)
        return {}


def http_post_json(url: str, body: dict, referer: str = f"{BASE_URL}/") -> dict:
    """Fetch a POST JSON endpoint."""
    try:
        r = _http().post(
            url,
            json=body,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": referer,
            },
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json() or {}
        return {}
    except Exception as e:
        log.warning("MovieBox POST failed | url=%s err=%r", url, e)
        return {}


def _subject_to_item(it: dict) -> dict:
    """Normalise a MovieBox `subject` object into a card item."""
    sid = str(it.get("subjectId") or "")
    stype = int(it.get("subjectType") or 1)  # 1 = Movie, 2 = TV Series
    is_tv = (stype == 2)
    slug = it.get("detailPath") or ""
    title = _clean(it.get("title") or "Unknown")
    year = (it.get("releaseDate") or "")[:4]
    rate = str(it.get("imdbRatingValue") or "")
    genres = _clean(it.get("genre") or "")
    country = _clean(it.get("countryName") or "")
    cover = (it.get("cover") or {}).get("url") or ""

    type_label = "TV Series" if is_tv else "Movie"
    meta_parts = []
    if rate and rate != "0":
        meta_parts.append(f"⭐ {rate}")
    if year:
        meta_parts.append(year)
    meta_parts.append(type_label)

    web_url = f"{BASE_URL}/movies/{slug}?id={sid}&type={'/tv/detail' if is_tv else '/movie/detail'}&lang=en"
    return {
        "title": title,
        "display_title": title,
        "subject_id": sid,
        "slug": slug,
        "url": web_url,
        "rating": rate if rate != "0" else "",
        "year": year,
        "genres": genres,
        "country": country,
        "thumbnail": cover,
        "type": type_label,
        "is_tv": is_tv,
        "meta": " • ".join(meta_parts),
    }


def parse_search_cards(html_text: str) -> list[dict]:
    """Parse search result cards from SSR HTML."""
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    seen = set()

    # Thumbnails live in the Nuxt state blob (HTML has no <img> on search).
    img_map = {}
    for s in soup.find_all("script"):
        if s.string and "ShallowReactive" in s.string:
            txt = s.string
            pairs = re.findall(
                r'detailPath["\']?\s*:\s*["\']([^"\']+)["\'].*?https://(pbcdn[w]?\.aoneroom\.com/image/[^"\']+)',
                txt,
            )
            for dp, im in pairs:
                img_map[dp] = f"https://{im}"
            break

    for a in soup.find_all("a", href=re.compile(r"/moviesDetail/")):
        href = a.get("href", "").strip()
        if not href or href in seen:
            continue
        seen.add(href)

        slug = href.split("/moviesDetail/")[-1].strip("/")
        h2 = a.find(["h2", "h3"], class_=re.compile("card-title"))
        title = _clean(h2.get_text(strip=True)) if h2 else ""
        if not title:
            title = _clean(a.get("title") or "") or slug

        rate_el = a.find("span", class_=re.compile("rate"))
        rate = _clean(rate_el.get_text(strip=True)) if rate_el else "-"

        is_tv = bool(re.search(r"\bS\d+|\bSeason\b|\bSeries\b", title, re.I))
        clean_title = re.sub(r"\s+S\d+(?:-S\d+)?$", "", title).strip()

        thumb = img_map.get(slug, "")
        if not thumb:
            img_tag = a.find("img")
            if img_tag:
                thumb = img_tag.get("src") or img_tag.get("data-src") or ""

        type_label = "TV Series" if is_tv else "Movie"
        meta = f"⭐ {rate} • {type_label}" if rate != "-" else type_label

        out.append({
            "title": clean_title or title,
            "display_title": title,
            "slug": slug,
            "url": f"{BASE_URL}{href}",
            "subject_id": "",
            "rating": rate if rate != "-" else "",
            "thumbnail": thumb,
            "type": type_label,
            "is_tv": is_tv,
            "meta": meta,
        })
    return out


def search(query: str) -> list[dict]:
    """Search MovieBox by title query."""
    q = _clean(query)
    if not q:
        return []
    url = f"{BASE_URL}/newWeb/searchResult?keyword={quote_plus(q)}"
    html = http_get(url)
    return parse_search_cards(html)[:MAX_RESULTS]


def fetch_trending(tab_id: str | None = None, page: int = 0, per_page: int = 30) -> list[dict]:
    """Trending movies / TV shows from the MovieBox API."""
    tab_param = f"tabId={tab_id}&" if tab_id else ""
    url = f"{API_BASE}/subject/trending?{tab_param}page={page}&perPage={per_page}"
    data = http_json(url)
    if not data or data.get("code") != 0:
        return []
    items = (data.get("data") or {}).get("subjectList") or []
    return [_subject_to_item(it) for it in items]


def fetch_home_sections() -> dict:
    """Pre-curated home blocks: {section title: [subject, ...]} (cached)."""
    now = time.time()
    cached = HOME_CACHE.get("by_title") or {}
    if cached and now - HOME_CACHE.get("ts", 0) < INDEX_TTL:
        return cached

    data = http_json(f"{API_BASE}/home?host=officialmoviebox.com")
    by_title: dict[str, list] = {}
    for op in (data.get("data") or {}).get("operatingList") or []:
        title = op.get("title") or ""
        subs = op.get("subjects") or []
        if subs and title and title not in by_title:
            by_title[title] = subs

    HOME_CACHE["ts"] = now
    HOME_CACHE["by_title"] = by_title
    return by_title


def fetch_section_items(section_key: str, page: int = 1, per_page: int = 30) -> list[dict]:
    """Fetch items for one section key from `constants.SECTIONS`."""
    from .constants import SECTIONS

    spec = SECTIONS.get(section_key)
    if not spec:
        return []
    _label, kind, params = spec

    if kind == "trending":
        tab = params.get("tab")
        # `subject/trending` ignores perPage > 18 for tabbed requests and
        # returns ~10 items, so page through and merge until `per_page`.
        out: list[dict] = []
        seen: set[str] = set()
        api_page = 0
        while len(out) < per_page and api_page < 6:
            chunk = fetch_trending(tab, page=api_page, per_page=18)
            if not chunk:
                break
            for it in chunk:
                key = it.get("subject_id") or it.get("slug") or it.get("title")
                if key in seen:
                    continue
                seen.add(key)
                out.append(it)
            api_page += 1
        return out[:per_page]

    if kind == "home":
        by_title = fetch_home_sections()
        subs = by_title.get(params.get("title") or "") or []
        return [_subject_to_item(s) for s in subs]

    if kind == "filter":
        body = {"page": page, "perPage": per_page}
        body.update(params)
        data = http_post_json(f"{API_BASE}/subject/filter", body)
        items = (data.get("data") or {}).get("items") or []
        return [_subject_to_item(it) for it in items]

    return []


def fetch_popular_titles() -> list[str]:
    """Curated quick-search titles."""
    return list(POPULAR_TITLES)
