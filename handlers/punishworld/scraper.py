"""HTTP client + parser + fetcher untuk explorer PunishWorld (`/punish`).

FLOW SCRAPER
------------
1. `http_get(url)` -> HTML mentah (curl_cffi impersonate Chrome).
2. `parse_cards(html)` -> kartu listing `div.row.no-gutters div.video-block`
   (judul, url, thumb, durasi, views, rating).
3. `_collect_pages(first_url, page_tmpl, max_items)` -> gabungkan beberapa
   halaman server sampai `MAX_RESULTS` (48 kartu/halaman).
4. `search(query)` -> `/?s=<q>` dengan paginasi `/page/<N>/?s=<q>`.
5. `fetch_latest()` -> beranda `/` dengan paginasi `/page/<N>/`.
6. `fetch_category(cat_id, slug)` -> `/<id>-<slug>/page/N/`.
7. `fetch_tag(slug)` -> `/tag/<slug>/page/N/`.
8. `fetch_categories()` -> `/categories/` (126 kategori, cache 1 jam).
9. `fetch_tags()` -> `/10-video-tags/` (900+ tag, cache 1 jam).
10. `fetch_detail(url)` -> metadata halaman post (judul, thumb, statistik, kategori, tag).
"""
import logging
import math
import re
import time

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .constants import (
    BASE_URL,
    FALLBACK_CATEGORIES,
    HTTP_TIMEOUT,
    INDEX_TTL,
    MAX_RESULTS,
    UA,
)
from .state import get_categories_cache, get_tags_cache

log = logging.getLogger(__name__)

_sessions: dict = {}

# Kategori bisa bertingkat: `/<id>-<slug>/` ATAU `/<id>-<slug>/<sub_id>-<sub_slug>/`.
# `path` menyimpan jalur lengkap (tanpa origin) supaya listing & paginasi benar,
# termasuk untuk subkategori (mis. `7995-femdom/18757-ballbusting`).
_CAT_RE = re.compile(r"^(?:https?://punishworld\.com)?/((?:[a-z0-9-]+/)*\d+-[a-z0-9-]+)/?$", re.I)
_TAG_RE = re.compile(r"/tag/([^/]+)/?")
_POST_RE = re.compile(r"^https?://(?:www\.)?punishworld\.com/\d+-[a-z0-9-]+/?$")


def _http(host_key: str = "default"):
    sess = _sessions.get(host_key)
    if sess is None:
        sess = curl_requests.Session(impersonate="chrome")
        _sessions[host_key] = sess
    return sess


def http_get(url: str, referer: str = "") -> str:
    """Ambil HTML halaman; kosong kalau gagal."""
    try:
        headers = {"User-Agent": UA}
        if referer:
            headers["Referer"] = referer
        r = _http().get(url, headers=headers, timeout=HTTP_TIMEOUT)
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        log.warning("PunishWorld explorer HTTP request failed | url=%s err=%r", url, e)
        return ""


def is_post_url(url: str) -> bool:
    return bool(_POST_RE.match((url or "").strip()))


def _clean_views(text: str) -> str:
    txt = re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()
    txt = re.sub(r"^[^\d]*", "", txt).strip()
    return txt


def parse_cards(html_text: str) -> list[dict]:
    """Kartu listing dari `div.row.no-gutters div.video-block`.

    -> [{title, url, thumbnail, duration, views, rating, meta}]
    """
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    seen = set()
    for block in soup.select("div.row.no-gutters div.video-block"):
        link = block.select_one("a.thumb[href]")
        if not link:
            continue
        url = (link.get("href") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)

        title = (link.get("aria-label") or "").strip()
        if not title:
            span = block.select_one("a.infos span.title") or block.select_one("span.title")
            title = span.get_text(" ", strip=True) if span else "Unknown title"
        title = " ".join(title.split())

        img = link.select_one("img") or block.find("img")
        thumb = ""
        if img:
            thumb = (img.get("src") or img.get("data-src") or "").strip()

        duration = ""
        dur = block.select_one(".duration")
        if dur:
            duration = " ".join(dur.get_text(" ", strip=True).split())

        views = ""
        vw = block.select_one(".views-number")
        if vw:
            views = _clean_views(vw.get_text(" ", strip=True))

        rating = ""
        rt = block.select_one(".rating-nolike")
        if rt:
            rating = _clean_views(rt.get_text(" ", strip=True))

        parts = []
        if duration:
            parts.append(duration)
        if views:
            parts.append(f"{views} views")
        if rating:
            parts.append(f"👍 {rating}" if rating.endswith("%") else rating)

        out.append({
            "title": title,
            "url": url,
            "thumbnail": thumb,
            "duration": duration,
            "views": views,
            "rating": rating,
            "meta": "  ·  ".join(parts),
        })
    return out


def _has_results(html_text: str) -> bool:
    """Halaman search menaruh jumlah video di h1: `<query> Porn (N Videos)`.

    N == 0 -> site menampilkan video fallback (bukan hasil search) -> buang.
    """
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html_text or "", re.S | re.I)
    if not m:
        return True
    plain = re.sub(r"<[^>]+>", "", m.group(1))
    cnt = re.search(r"\((\d+)\s*Videos?\)", plain, re.I)
    if not cnt:
        return True
    return int(cnt.group(1)) > 0


def _collect_pages(first_url: str, page_tmpl: str, max_items: int) -> list[dict]:
    """Ambil halaman 1..N sampai `max_items`; `page_tmpl` memakai `{page}`."""
    first = http_get(first_url)
    results = parse_cards(first)
    if not results:
        return []
    seen = {c["url"] for c in results}
    if len(results) >= max_items:
        return results[:max_items]

    max_pages = max(2, math.ceil(max_items / 48) + 1)
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
    """Cari di `/?s=<q>`; halaman berikutnya `/page/<N>/?s=<q>`."""
    from urllib.parse import quote_plus
    slug = quote_plus((query or "").strip())
    first = http_get(f"{BASE_URL}/?s={slug}")
    if not _has_results(first):
        return []
    results = parse_cards(first)
    if not results:
        return []
    seen = {c["url"] for c in results}
    if len(results) >= max_items:
        return results[:max_items]
    max_pages = max(2, math.ceil(max_items / 48) + 1)
    for page in range(2, max_pages + 1):
        more = parse_cards(http_get(f"{BASE_URL}/page/{page}/?s={slug}"))
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


def fetch_latest(max_items: int = MAX_RESULTS) -> list[dict]:
    """Listing beranda (`/`) dengan paginasi `/page/<N>/`."""
    return _collect_pages(f"{BASE_URL}/", f"{BASE_URL}/page/{{page}}/", max_items)


def fetch_category(path: str, max_items: int = MAX_RESULTS) -> list[dict]:
    """Listing kategori `/<path>/page/N/`.

    `path` = jalur kategori tanpa origin, mis. `22-anal` atau
    `7995-femdom/18757-ballbusting` (subkategori bertingkat).
    """
    path = (path or "").strip().strip("/")
    if not path:
        return []
    base = f"{BASE_URL}/{path}"
    return _collect_pages(f"{base}/", f"{base}/page/{{page}}/", max_items)


def fetch_tag(slug: str, max_items: int = MAX_RESULTS) -> list[dict]:
    """Listing tag `/tag/<slug>/page/N/`."""
    slug = (slug or "").strip()
    if not slug:
        return []
    base = f"{BASE_URL}/tag/{slug}"
    return _collect_pages(f"{base}/", f"{base}/page/{{page}}/", max_items)


def fetch_categories() -> list[dict]:
    """Kategori dari `/categories/` (cache 1 jam). -> [{id, slug, name, count, thumb, url}]

    Hasil apa pun (termasuk `FALLBACK_CATEGORIES` statis) SELALU disimpan ke
    cache supaya `build_categories_content` ikut terisi.
    """
    slot = get_categories_cache()
    now = time.time()
    if slot.get("list") and now - slot.get("ts", 0) < INDEX_TTL:
        return slot["list"]

    cats = []
    seen = set()
    raw = http_get(f"{BASE_URL}/categories/")
    if raw:
        soup = BeautifulSoup(raw, "html.parser")
        for block in soup.select("div.video-block.video-block-cat"):
            a = block.select_one("a.thumb[href]") or block.select_one("a.infos[href]")
            if not a:
                continue
            m = _CAT_RE.match(a["href"].strip())
            if not m:
                continue
            cat_path = m.group(1).strip("/")
            slug = cat_path.rsplit("-", 1)[-1] if "-" in cat_path else cat_path
            if cat_path in seen:
                continue
            seen.add(cat_path)
            span = block.select_one("span.title")
            name = (span.get_text(" ", strip=True) if span else a.get("title") or "") or slug.replace("-", " ").title()
            cnt_el = block.select_one("div.video-datas-category-count")
            count = cnt_el.get_text(" ", strip=True).strip("() ") if cnt_el else ""
            count = re.sub(r"\s*videos?\s*$", "", count, flags=re.I).strip()
            img = block.find("img")
            thumb = (img.get("src") or img.get("data-src") or "").strip() if img else ""
            cats.append({
                "path": cat_path,
                "slug": slug,
                "name": " ".join(name.split()),
                "count": count,
                "thumb": thumb,
                "url": f"{BASE_URL}/{cat_path}/",
            })

    if not cats:
        cats = [dict(c) for c in FALLBACK_CATEGORIES]

    if cats:
        slot["list"] = cats
        slot["ts"] = now
    return cats


def fetch_tags() -> list[dict]:
    """Tag dari `/10-video-tags/` (cache 1 jam). -> [{slug, name, url}]"""
    slot = get_tags_cache()
    now = time.time()
    if slot.get("list") and now - slot.get("ts", 0) < INDEX_TTL:
        return slot["list"]

    tags = []
    seen = set()
    raw = http_get(f"{BASE_URL}/10-video-tags/")
    if raw:
        soup = BeautifulSoup(raw, "html.parser")
        for a in soup.select("a[href*='/tag/']"):
            m = _TAG_RE.search(a.get("href") or "")
            if not m:
                continue
            slug = m.group(1).strip()
            if slug in seen:
                continue
            name = " ".join(a.get_text(" ", strip=True).split())
            if not name or name.lower() in ("tags",):
                continue
            seen.add(slug)
            tags.append({"slug": slug, "name": name, "url": f"{BASE_URL}/tag/{slug}/"})

    if tags:
        slot["list"] = tags
        slot["ts"] = now
    return tags or slot.get("list") or []


def fetch_detail(url: str) -> dict:
    """Metadata halaman post.

    -> {kind, title, thumbnail, duration, views, rating, categories, tags}
    `kind="video"` kalau ada `<video>`/source; `"unknown"` kalau tidak.
    """
    raw = http_get(url, referer=f"{BASE_URL}/")
    if not raw:
        return {"kind": "unknown", "title": "", "url": url}

    soup = BeautifulSoup(raw, "html.parser")

    title = ""
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
    title = re.sub(r"\s*[-|]\s*Punishworld.*$", "", title, flags=re.I).strip()

    thumb = ""
    ogi = soup.select_one('meta[property="og:image"]')
    if ogi and ogi.get("content"):
        thumb = ogi["content"].strip()

    duration = ""
    dur = soup.select_one(".duration")
    if dur:
        duration = " ".join(dur.get_text(" ", strip=True).split())

    views = ""
    vw = soup.select_one(".views-number")
    if vw:
        views = _clean_views(vw.get_text(" ", strip=True))

    rating = ""
    rt = soup.select_one(".rating-nolike")
    if rt:
        rating = _clean_views(rt.get_text(" ", strip=True))

    cats, tags = [], []
    scope = soup.select_one("div.tags-list") or soup.select_one("div.category-menu.tag") or soup
    for a in scope.find_all("a", href=True):
        href = a["href"] or ""
        name = " ".join(a.get_text(" ", strip=True).split())
        if not name:
            continue
        if _CAT_RE.match(href):
            if name not in cats:
                cats.append(name)
        elif "/tag/" in href:
            if name not in tags:
                tags.append(name)

    has_video = bool(soup.find("video") or soup.select("source[src]"))

    return {
        "kind": "video" if has_video else "unknown",
        "url": url,
        "title": " ".join(title.split()),
        "thumbnail": thumb,
        "duration": duration,
        "views": views,
        "rating": rating,
        "categories": cats,
        "tags": tags,
    }
