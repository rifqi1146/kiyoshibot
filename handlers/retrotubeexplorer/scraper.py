"""HTTP client + parser + fetcher untuk explorer RetroTube (`/becekku`, `/lendirqu`).

FLOW SCRAPER
------------
1. `http_get(url)` -> HTML mentah (curl_cffi impersonate Chrome).
2. `parse_cards(html)` -> kartu listing `main.site-main article.loop-video`
   (judul, url, thumb, durasi, views, rating). Kartu sidebar ("Vidio Random")
   dan blok "Related videos" di halaman post SENGAJA dibuang dengan membatasi
   scope ke `main.site-main` + membuang `div.under-video-block`.
3. `fetch_paginated(site, url_template)` -> gabungkan beberapa halaman server
   sampai `MAX_RESULTS` (kategori / tag).
4. `fetch_latest(site, filter_key)` -> `?filter=latest` / `?filter=most-viewed`
   dengan paginasi `/page/<N>/?filter=<key>`.
5. `search(site, query)` -> `/?s=<q>` dengan paginasi `/page/<N>/?s=<q>`.
6. `fetch_categories(site)` -> `/categories/` (cache 1 jam, fallback statis).
7. `fetch_tags(site)` -> `/tags/` (tag cloud, cache 1 jam).
8. `fetch_detail(site, url)` -> metadata halaman post (judul, thumb, embed
   iframe, views, tanggal, kategori, tag).
"""
import logging
import math
import re
import time
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .constants import HTTP_TIMEOUT, INDEX_TTL, MAX_RESULTS, UA
from .state import get_categories_cache, get_tags_cache

log = logging.getLogger(__name__)

_sessions: dict = {}
# Cache origin kanonik per prefix (mis. lendirqu.stream -> lendirqu.club).
_CANONICAL_BASE: dict = {}


def canonical_base(site: dict) -> str:
    """Origin efektif setelah redirect (mis. `lendirqu.stream` -> `lendirqu.club`).

    Beberapa path (mis. `post_tag-sitemap.xml`) hilang saat redirect lintas
    domain, jadi base kanonik di-resolve sekali lalu dipakai untuk semua fetch.
    """
    key = site["prefix"]
    cached = _CANONICAL_BASE.get(key)
    if cached:
        return cached
    base = site["base"]
    try:
        r = _http().get(base, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT, allow_redirects=True)
        final = str(getattr(r, "url", "") or "")
        if final:
            from urllib.parse import urlsplit
            parts = urlsplit(final)
            if parts.scheme and parts.netloc:
                base = f"{parts.scheme}://{parts.netloc}"
    except Exception as e:
        log.debug("canonical base resolve failed | site=%s err=%r", key, e)
    _CANONICAL_BASE[key] = base
    return base


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
        log.warning("Explorer HTTP request failed | url=%s err=%r", url, e)
        return ""


def _card_thumb(art) -> str:
    """Thumbnail: `data-main-thumb`, lalu img `data-lazy-src`/`src`/`data-src`."""
    t = art.get("data-main-thumb")
    if t:
        return t.strip()
    img = art.find("img")
    if img:
        for key in ("data-lazy-src", "src", "data-src"):
            v = img.get(key)
            if v and not str(v).startswith("data:"):
                return str(v).strip()
    return ""


def _stat(art, class_name: str) -> str:
    el = art.find("span", class_=class_name)
    if not el:
        return ""
    return " ".join(el.get_text(" ", strip=True).split())


def parse_cards(html_text: str) -> list[dict]:
    """Kartu listing dari `main.site-main article.loop-video`.

    -> [{title, url, thumbnail, duration, views, rating}]
    Sidebar & related-video dibuang dengan scope ke `main.site-main`.
    """
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    scope = soup.select_one("main.site-main") or soup
    out = []
    seen = set()
    for art in scope.find_all("article"):
        # Related videos di halaman post berada di div.under-video-block.
        if art.find_parent("div", class_="under-video-block"):
            continue
        a = art.find("a", href=True)
        if not a or not a.get("href"):
            continue
        url = a["href"].strip()
        if url in seen:
            continue
        seen.add(url)

        title = (a.get("title") or a.get_text(" ", strip=True) or "Unknown title")
        rating_el = art.select_one("div.rating-bar span")
        out.append({
            "title": " ".join(title.split()),
            "url": url,
            "thumbnail": _card_thumb(art),
            "duration": _stat(art, "duration"),
            "views": _stat(art, "views"),
            "rating": " ".join(rating_el.get_text(" ", strip=True).split()) if rating_el else "",
        })
    return out


def _collect_pages(first_url: str, page_tmpl: str, max_items: int) -> list[dict]:
    """Ambil halaman 1..N sampai `max_items`; `page_tmpl` memakai `{page}`."""
    results = parse_cards(http_get(first_url))
    if not results:
        return []
    seen = {c["url"] for c in results}
    if len(results) >= max_items:
        return results[:max_items]

    max_pages = max(2, math.ceil(max_items / 20) + 1)
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


def fetch_paginated(site: dict, url_template: str, max_items: int = MAX_RESULTS) -> list[dict]:
    """Kumpulkan kartu dari beberapa halaman server sampai `max_items`.

    `url_template` memakai `{page}`. Berhenti saat halaman kosong, semua
    duplikat, atau target tercapai.
    """
    return _collect_pages(url_template.format(page=1), url_template, max_items)


def search(site: dict, query: str, max_items: int = MAX_RESULTS) -> list[dict]:
    """Cari di `/?s=<q>`; halaman berikutnya `/page/<N>/?s=<q>`."""
    base = canonical_base(site)
    slug = quote_plus((query or "").strip())
    return _collect_pages(
        f"{base}/?s={slug}",
        f"{base}/page/{{page}}/?s={slug}",
        max_items,
    )


def fetch_latest(site: dict, filter_key: str = "latest", max_items: int = MAX_RESULTS) -> list[dict]:
    """Listing `?filter=latest` / `?filter=most-viewed` sampai `max_items`."""
    base = canonical_base(site)
    return _collect_pages(
        f"{base}/?filter={filter_key}",
        f"{base}/page/{{page}}/?filter={filter_key}",
        max_items,
    )


def _parse_category_links(raw: str) -> list[dict]:
    """Ambil kategori dari link `/category/<slug>/` (dedup, urut kemunculan)."""
    if not raw:
        return []
    soup = BeautifulSoup(raw, "html.parser")
    cats = []
    seen = set()
    for a in soup.select("a[href*='/category/']"):
        m = re.search(r"/category/([^/]+)/", a.get("href") or "")
        if not m:
            continue
        slug = m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)
        name = " ".join(a.get_text(" ", strip=True).split()) or slug.replace("-", " ").title()
        cats.append({"slug": slug, "name": name})
    return cats


def _fetch_with_fallback_paths(site: dict, path: str) -> str:
    """GET `path` di origin kanonik lalu origin mentah (path bisa beda host).

    LendirQu menyajikan `/categories/` di `lendirqu.stream` sementara origin
    kanoniknya `lendirqu.club` (404), sedangkan `post_tag-sitemap.xml` justru
    sebaliknya. Jadi kedua origin wajib dicoba, bukan cuma kanonik.
    """
    canon = canonical_base(site)
    raw_base = (site.get("base") or "").rstrip("/")
    for base in (canon, raw_base):
        if not base:
            continue
        raw = http_get(f"{base}{path}")
        if raw:
            return raw
    return ""


def fetch_categories(site: dict) -> list[dict]:
    """Kategori dari `/categories/` (cache 1 jam). -> [{slug, name}]

    Urutan sumber: `/categories/` (kanonik → origin mentah) → nav menu beranda
    → `fallback_categories` statis. Hasil apa pun (termasuk fallback statis)
    SELALU disimpan ke cache supaya `build_categories_content` ikut terisi.
    """
    site_key = site["prefix"]
    slot = get_categories_cache(site_key)
    now = time.time()
    if slot.get("list") and now - slot.get("ts", 0) < INDEX_TTL:
        return slot["list"]

    cats = _parse_category_links(_fetch_with_fallback_paths(site, "/categories/"))
    if not cats:
        # Terakhir sebelum statis: nav menu di beranda (kedua origin).
        canon = canonical_base(site)
        raw_base = (site.get("base") or "").rstrip("/")
        for base in (canon, raw_base):
            if base:
                cats = _parse_category_links(http_get(f"{base}/"))
                if cats:
                    break
    if not cats:
        cats = list(site.get("fallback_categories") or [])

    if cats:
        slot["list"] = cats
        slot["ts"] = now
    return cats


def fetch_tags(site: dict) -> list[dict]:
    """Tag cloud dari `/tags/` (cache 1 jam). -> [{slug, name}]

    Urutan sumber: `/tags/` (kanonik → origin mentah) → `post_tag-sitemap.xml`
    (kanonik → origin mentah). LendirQu tidak punya halaman tag cloud (`/tags/`
    redirect ke beranda) dan sitemap-nya cuma ada di origin kanonik, jadi kedua
    origin wajib dicoba. Hasil apa pun SELALU disimpan ke cache supaya
    `build_tags_content` ikut terisi.
    """
    site_key = site["prefix"]
    slot = get_tags_cache(site_key)
    now = time.time()
    if slot.get("list") and now - slot.get("ts", 0) < INDEX_TTL:
        return slot["list"]

    tags = _parse_tag_cloud(_fetch_with_fallback_paths(site, "/tags/"))
    if not tags:
        tags = _parse_tag_sitemap(_fetch_with_fallback_paths(site, "/post_tag-sitemap.xml"))

    if tags:
        slot["list"] = tags
        slot["ts"] = now
    return tags or slot.get("list") or []


def _parse_tag_cloud(raw: str) -> list[dict]:
    if not raw:
        return []
    soup = BeautifulSoup(raw, "html.parser")
    tags = []
    seen = set()
    for a in soup.select("a.tag-cloud-link, a[href*='/tag/']"):
        m = re.search(r"/tag/([^/]+)/", a.get("href") or "")
        if not m:
            continue
        slug = m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)
        tags.append({"slug": slug, "name": a.get_text(" ", strip=True) or slug.replace("-", " ")})
    return tags


def _parse_tag_sitemap(raw: str) -> list[dict]:
    """Fallback: daftar tag dari `post_tag-sitemap.xml`."""
    if not raw or "<loc>" not in raw:
        return []
    tags = []
    seen = set()
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", raw):
        m = re.search(r"/tag/([^/]+)/?", loc)
        if not m:
            continue
        slug = m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)
        tags.append({"slug": slug, "name": slug.replace("-", " ")})
    return tags


def fetch_detail(site: dict, url: str) -> dict:
    """Metadata halaman post.

    -> {kind, title, thumbnail, embed, views, date, categories, tags}
    `kind="video"` kalau ada iframe embed; `"unknown"` kalau tidak.
    """
    raw = http_get(url, referer=f"{canonical_base(site)}/")
    if not raw:
        return {"kind": "unknown", "title": "", "url": url}

    soup = BeautifulSoup(raw, "html.parser")

    title = ""
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else site["label"]

    thumb = ""
    ogi = soup.select_one('meta[property="og:image"]')
    if ogi and ogi.get("content"):
        thumb = ogi["content"].strip()

    embed = ""
    for iframe in soup.select("header.entry-header iframe, div.video-player iframe, iframe"):
        src = (iframe.get("src") or "").strip()
        if src:
            embed = src
            break

    views = ""
    vv = soup.select_one("div#video-views span")
    if vv:
        views = vv.get_text(strip=True)

    date = ""
    vd = soup.select_one("div#video-date")
    if vd:
        date = " ".join(vd.get_text(" ", strip=True).replace("Date:", "").split())

    cats, tags = [], []
    for a in soup.select("div.tags-list a.label, div.entry-content a[href*='/category/'], div.entry-content a[href*='/tag/']"):
        href = a.get("href") or ""
        name = a.get_text(" ", strip=True)
        if not name:
            continue
        if "/category/" in href and name not in cats:
            cats.append(name)
        elif "/tag/" in href and name not in tags:
            tags.append(name)

    return {
        "kind": "video" if embed else "unknown",
        "url": url,
        "title": " ".join(title.split()),
        "thumbnail": thumb,
        "embed": embed,
        "views": views,
        "date": date,
        "categories": cats,
        "tags": tags,
    }
