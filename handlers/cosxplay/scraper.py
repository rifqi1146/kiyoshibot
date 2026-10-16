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
from .state import get_actors_cache, get_categories_cache, get_tags_cache

log = logging.getLogger(__name__)

_sessions: dict = {}

_CAT_RE = re.compile(r"^(?:https?://cosxplay\.com)?/((?:[a-z0-9-]+/)*\d+-[a-z0-9-]+)/?$", re.I)
_TAG_RE = re.compile(r"/tag/([^/]+)/?")
_ACTOR_RE = re.compile(r"[?&]actors=([^&/]+)")
_POST_RE = re.compile(r"^https?://(?:www\.)?cosxplay\.com/\d+-[a-z0-9-]+/?$")


def _http(host_key: str = "default"):
    sess = _sessions.get(host_key)
    if sess is None:
        sess = curl_requests.Session(impersonate="chrome")
        _sessions[host_key] = sess
    return sess


def http_get(url: str, referer: str = "") -> str:
    try:
        headers = {"User-Agent": UA}
        if referer:
            headers["Referer"] = referer
        r = _http().get(url, headers=headers, timeout=HTTP_TIMEOUT)
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        log.warning("CosXplay explorer HTTP request failed | url=%s err=%r", url, e)
        return ""


def is_post_url(url: str) -> bool:
    return bool(_POST_RE.match((url or "").strip()))


def _clean_views(text: str) -> str:
    txt = re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()
    txt = re.sub(r"^[^\d]*", "", txt).strip()
    return txt


def parse_cards(html_text: str) -> list[dict]:
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
        rt = block.select_one(".rating-nolike") or block.select_one(".rating")
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
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html_text or "", re.S | re.I)
    if not m:
        return True
    plain = re.sub(r"<[^>]+>", "", m.group(1))
    cnt = re.search(r"\((\d+)\s*Videos?\)", plain, re.I)
    if not cnt:
        return True
    return int(cnt.group(1)) > 0


def _collect_pages(first_url: str, page_tmpl: str, max_items: int) -> list[dict]:
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
    return _collect_pages(f"{BASE_URL}/", f"{BASE_URL}/page/{{page}}/", max_items)


def fetch_category(path: str, max_items: int = MAX_RESULTS) -> list[dict]:
    path = (path or "").strip().strip("/")
    if not path:
        return []
    base = f"{BASE_URL}/{path}"
    return _collect_pages(f"{base}/", f"{base}/page/{{page}}/", max_items)


def fetch_tag(slug: str, max_items: int = MAX_RESULTS) -> list[dict]:
    slug = (slug or "").strip()
    if not slug:
        return []
    base = f"{BASE_URL}/tag/{slug}"
    return _collect_pages(f"{base}/", f"{base}/page/{{page}}/", max_items)


def fetch_actor(slug: str, max_items: int = MAX_RESULTS) -> list[dict]:
    slug = (slug or "").strip()
    if not slug:
        return []
    return _collect_pages(
        f"{BASE_URL}/?actors={slug}",
        f"{BASE_URL}/page/{{page}}/?actors={slug}",
        max_items,
    )


def fetch_categories() -> list[dict]:
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
    slot = get_tags_cache()
    now = time.time()
    if slot.get("list") and now - slot.get("ts", 0) < INDEX_TTL:
        return slot["list"]

    tags = []
    seen = set()
    raw = http_get(f"{BASE_URL}/9-video-tags/")
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


def fetch_actors() -> list[dict]:
    actors_slot = get_actors_cache()
    now = time.time()
    if actors_slot.get("list") and now - actors_slot.get("ts", 0) < INDEX_TTL:
        return actors_slot["list"]

    actors = []
    seen = set()
    raw = http_get(f"{BASE_URL}/11314-video-actors/")
    if raw:
        soup = BeautifulSoup(raw, "html.parser")
        for block in soup.select("div.video-block"):
            a = block.select_one("a.thumb[href]") or block.select_one("a[href]")
            if not a:
                continue
            m = _ACTOR_RE.search(a.get("href") or "")
            if not m:
                continue
            slug = m.group(1).strip()
            if slug in seen:
                continue
            seen.add(slug)
            span = block.select_one("span.title")
            name = (span.get_text(" ", strip=True) if span else "") or slug.replace("-", " ").title()
            cnt_el = block.select_one("div.video-datas-category-count") or block.select_one(".views-number")
            count = cnt_el.get_text(" ", strip=True).strip("() ") if cnt_el else ""
            count = re.sub(r"\s*videos?\s*$", "", count, flags=re.I).strip()
            img = block.find("img")
            thumb = (img.get("src") or img.get("data-src") or "").strip() if img else ""
            actors.append({
                "slug": slug,
                "name": " ".join(name.split()),
                "count": count,
                "thumb": thumb,
                "url": f"{BASE_URL}/?actors={slug}",
            })

    if actors:
        actors_slot["list"] = actors
        actors_slot["ts"] = now
    return actors or actors_slot.get("list") or []


def fetch_detail(url: str) -> dict:
    raw = http_get(url, referer=f"{BASE_URL}/")
    if not raw:
        return {"kind": "unknown", "title": "", "url": url}

    soup = BeautifulSoup(raw, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = re.sub(r"\s*\|\s*CosXplay\.com\s*$", "", soup.title.get_text(strip=True), flags=re.I)
    title = re.sub(r"\s*[-|]\s*CosXplay.*$", "", title, flags=re.I).strip()

    thumb = ""
    vtag = soup.find("video")
    if vtag and vtag.get("poster"):
        thumb = vtag["poster"].strip()
    if not thumb:
        ogi = soup.select_one('meta[property="og:image"]')
        if ogi and ogi.get("content"):
            thumb = ogi["content"].strip()
    if not thumb:
        img = soup.select_one("div.video-block img") or soup.find("img")
        if img:
            thumb = (img.get("src") or img.get("data-src") or "").strip()

    duration = ""
    dur = soup.select_one(".duration")
    if dur:
        duration = " ".join(dur.get_text(" ", strip=True).split())

    views = ""
    vw = soup.select_one(".views-number")
    if vw:
        views = _clean_views(vw.get_text(" ", strip=True))

    rating = ""
    rt = soup.select_one(".rating-nolike") or soup.select_one(".rating")
    if rt:
        rating = _clean_views(rt.get_text(" ", strip=True))

    cats, tags = [], []
    scope = soup.select_one("div.tags-list") or soup.select_one("div.category-menu.tag") or soup
    for a in scope.find_all("a", href=True):
        href = a["href"] or ""
        name = " ".join(a.get_text(" ", strip=True).split())
        if not name:
            continue
        cls = " ".join(a.get("class", []))
        if "label-tag-video" in cls or "/tag/" in href:
            if name not in tags:
                tags.append(name)
        elif "label-cat-video" in cls or _CAT_RE.match(href):
            if name not in cats:
                cats.append(name)

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
