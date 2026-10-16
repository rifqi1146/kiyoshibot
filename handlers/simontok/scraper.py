import logging
import math
import re
import time
from urllib.parse import quote_plus, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .constants import (
    BASE_URL,
    EMPTY_NOTICE,
    FALLBACK_CATEGORIES,
    HTTP_TIMEOUT,
    INDEX_TTL,
    MAX_RESULTS,
    SECTIONS,
    UA,
)
from .state import get_categories_cache

log = logging.getLogger(__name__)

_sessions: dict = {}

POST_HOST = "simontok.study"


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
        log.warning("Simontok explorer HTTP request failed | url=%s err=%r", url, e)
        return ""


def is_post_url(url: str) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != POST_HOST:
        return False
    slug = (parts.path or "").strip("/")
    return bool(slug) and "/" not in slug


def _stat(art, class_name: str) -> str:
    el = art.find("span", class_=class_name)
    if not el:
        return ""
    return " ".join(el.get_text(" ", strip=True).split())


def parse_cards(html_text: str) -> list[dict]:
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")

    notice = soup.select_one("section.smt-kata")
    if notice and EMPTY_NOTICE in notice.get_text(" ", strip=True):
        return []

    out = []
    seen = set()
    for art in soup.find_all("article", class_="smt-kartu"):
        h3 = art.find("h3")
        link = h3.find("a", href=True) if h3 else None
        if not link:
            link = art.find("a", class_="smt-layar", href=True)
        if not link:
            link = art.find("a", href=True)
        if not link:
            continue

        url = (link.get("href") or "").strip()
        if not is_post_url(url) or url in seen:
            continue
        seen.add(url)

        title = h3.get_text(" ", strip=True) if h3 else ""
        img = art.find("img")
        if not title:
            title = (img.get("alt") or "").strip() if img else ""
        title = " ".join((title or "Unknown title").split())

        thumb = ""
        if img:
            thumb = (img.get("src") or img.get("data-src") or "").strip()

        duration = _stat(art, "smt-menit")
        views = _stat(art, "smt-hit")
        badge = _stat(art, "smt-jenis")

        parts = []
        if duration:
            parts.append(duration)
        if views:
            parts.append(f"{views} views")
        if badge:
            parts.append(badge)

        out.append({
            "title": title,
            "url": url,
            "thumbnail": thumb,
            "duration": duration,
            "views": views,
            "badge": badge,
            "meta": "  ·  ".join(parts),
        })
    return out


def _has_next_page(html_text: str, page: int) -> bool:
    if not html_text:
        return False
    soup = BeautifulSoup(html_text, "html.parser")
    nav = soup.select_one("nav.smt-urut") or soup
    target = f"/page/{page + 1}/"
    for a in nav.find_all("a", href=True):
        if target in a["href"]:
            return True
    return False


def _collect_pages(first_url: str, page_tmpl: str, max_items: int) -> list[dict]:
    raw = http_get(first_url)
    results = parse_cards(raw)
    if not results:
        return []
    seen = {c["url"] for c in results}
    if len(results) >= max_items:
        return results[:max_items]

    max_pages = max(2, math.ceil(max_items / 24) + 1)
    last_raw = raw
    for page in range(2, max_pages + 1):
        if not _has_next_page(last_raw, page - 1):
            break
        last_raw = http_get(page_tmpl.format(page=page))
        more = parse_cards(last_raw)
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


def fetch_paginated(url_template: str, max_items: int = MAX_RESULTS) -> list[dict]:
    first = url_template.format(page=1)
    return _collect_pages(first, url_template, max_items)


def fetch_section(section_key: str, max_items: int = MAX_RESULTS) -> list[dict]:
    section = SECTIONS.get(section_key)
    if not section:
        return []
    base = section[1].rstrip("/")
    return _collect_pages(
        f"{base}/page/1/",
        f"{base}/page/{{page}}/",
        max_items,
    )


def fetch_genre(slug: str, max_items: int = MAX_RESULTS) -> list[dict]:
    slug = (slug or "").strip()
    if not slug:
        return []
    base = f"{BASE_URL}/genre/{slug}"
    return _collect_pages(
        f"{base}/page/1/",
        f"{base}/page/{{page}}/",
        max_items,
    )


def search(query: str, max_items: int = MAX_RESULTS) -> list[dict]:
    slug = quote_plus((query or "").strip())
    return _collect_pages(
        f"{BASE_URL}/?s={slug}",
        f"{BASE_URL}/page/{{page}}/?s={slug}",
        max_items,
    )


def _parse_category_links(raw: str) -> list[dict]:
    if not raw:
        return []
    soup = BeautifulSoup(raw, "html.parser")
    block = soup.select_one("div.smt-kategori")
    if not block:
        return []
    cats = []
    seen = set()
    for a in block.find_all("a", href=True):
        m = re.search(r"/genre/([^/]+)/", a.get("href") or "")
        if not m:
            continue
        slug = m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)
        span = a.find("span")
        count = span.get_text(" ", strip=True) if span else ""
        if span:
            span.extract()
        name = " ".join(a.get_text(" ", strip=True).split()) or slug.replace("-", " ").title()
        cats.append({"slug": slug, "name": name, "count": count})
    return cats


def fetch_categories() -> list[dict]:
    slot = get_categories_cache()
    now = time.time()
    if slot.get("list") and now - slot.get("ts", 0) < INDEX_TTL:
        return slot["list"]

    cats = _parse_category_links(http_get(f"{BASE_URL}/"))
    if not cats:
        cats = [dict(c) for c in FALLBACK_CATEGORIES]

    if cats:
        slot["list"] = cats
        slot["ts"] = now
    return cats


def fetch_detail(url: str) -> dict:
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
    title = re.sub(r"\s*[-|]\s*Simontok.*$", "", title, flags=re.I).strip()

    thumb = ""
    ogi = soup.select_one('meta[property="og:image"]')
    if ogi and ogi.get("content"):
        thumb = ogi["content"].strip()

    embed = ""
    for iframe in soup.find_all("iframe"):
        src = (iframe.get("src") or "").strip()
        if src and ("putarin." in src or "puterin." in src):
            embed = src
            break
    if not embed:
        for iframe in soup.find_all("iframe"):
            src = (iframe.get("src") or "").strip()
            if src:
                embed = src
                break

    duration = ""
    views = ""
    badge = ""
    hero = soup.select_one("div.smt-putar-video, div.smt-video, article.smt-kartu")
    if hero:
        duration = _stat(hero, "smt-menit")
        views = _stat(hero, "smt-hit")
        badge = _stat(hero, "smt-jenis")

    cats = []
    for a in soup.find_all("a", href=True):
        href = a["href"] or ""
        if "/genre/" in href:
            name = a.get_text(" ", strip=True)
            if name and name not in cats:
                cats.append(name)

    return {
        "kind": "video" if embed else "unknown",
        "url": url,
        "title": " ".join(title.split()),
        "thumbnail": thumb,
        "embed": embed,
        "duration": duration,
        "views": views,
        "badge": badge,
        "categories": cats,
    }
