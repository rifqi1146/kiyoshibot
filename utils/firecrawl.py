"""Grounding web via Firecrawl (Search + Scrape).

- web_search(query): POST https://api.firecrawl.dev/v2/search — SERP cepat.
  Tingkat detail diatur `SEARCH_DEPTH`:
    "fast"    -> SERP polos (~1s), tanpa markdown. Model baca URL sendiri
                 lewat read_page kalau perlu. Ini default (paling cepat).
    "content" -> SERP + scrapeOptions (markdown tiap hasil, ~8s). Sekali
                 panggil dapat isi halaman, tapi jauh lebih lambat.
- read_url(url):     POST https://api.firecrawl.dev/v2/scrape — markdown (~0.6s).
Butuh FIRECRAWL_API_KEY (utils.config).
"""
import json
import logging

import aiohttp

from utils import config as app_config
from utils.http import get_http_session

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.firecrawl.dev/v2/search"
SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"


def _key() -> str:
    return (app_config.FIRECRAWL_API_KEY or "").strip()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    }


def _require_key():
    if not _key():
        raise RuntimeError("FIRECRAWL_API_KEY is not set in env.")


def _pick_results(data: dict) -> list[dict]:
    """Ambil list hasil dari berbagai bentuk respons /v2/search.

    Bentuk resmi: {"data": {"web": [...], "news": [...]}}. Dengan scrapeOptions
    kadang {"data": [...]} flat. Keduanya ditangani.
    """
    if not isinstance(data, dict):
        return []
    container = data.get("data")
    if isinstance(container, dict):
        out: list[dict] = []
        for group in ("web", "news"):
            items = container.get(group)
            if isinstance(items, list):
                out.extend(i for i in items if isinstance(i, dict))
        return out
    if isinstance(container, list):
        return [i for i in container if isinstance(i, dict)]
    return []


async def web_search(query: str, max_results: int = 5) -> str:
    """Search web via Firecrawl. Detail konten mengikuti SEARCH_DEPTH."""
    _require_key()
    query = (query or "").strip()
    if not query:
        raise ValueError("query kosong")

    deep = _search_depth() == "content"
    body = {
        "query": query,
        "limit": max_results,
        "sources": ["web"],
        "location": "Indonesia",
    }
    if deep:
        body["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}

    session = await get_http_session()
    # Timeout lebih longgar untuk mode content (scrape 5 halaman ~8s).
    total = 90 if deep else 30
    async with session.post(
        SEARCH_URL,
        json=body,
        headers=_headers(),
        timeout=aiohttp.ClientTimeout(total=total),
    ) as resp:
        raw = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"firecrawl search {resp.status}: {raw[:300]}")

    try:
        data = json.loads(raw)
    except ValueError:
        text = raw.strip()
        return text[:6000] if text else "No search results."

    results = _pick_results(data)
    lines = [f"### Hasil pencarian: {query}"]
    for item in results[:max_results]:
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        snippet = (item.get("description") or item.get("snippet") or "").strip()
        if len(snippet) > 500:
            snippet = snippet[:500] + "…"
        if not (title and url):
            continue
        block = f"- **{title}**\n  {url}\n  {snippet}"
        if deep:
            md = (item.get("markdown") or "").strip()
            if md:
                excerpt = " ".join(md.split())
                if len(excerpt) > 1500:
                    excerpt = excerpt[:1500] + "…"
                block += f"\n  Isi: {excerpt}"
        lines.append(block)

    if len(lines) == 1:
        return "No search results."
    text = "\n".join(lines)
    return text[:9000]


async def read_url(url: str, max_chars: int = 8000) -> str:
    """Scrape halaman via Firecrawl, kembalikan markdown diringkas."""
    _require_key()
    url = (url or "").strip()
    if not url:
        raise ValueError("url kosong")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    body = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    session = await get_http_session()
    async with session.post(
        SCRAPE_URL,
        json=body,
        headers=_headers(),
        timeout=aiohttp.ClientTimeout(total=45),
    ) as resp:
        raw = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"firecrawl scrape {resp.status}: {raw[:300]}")

    try:
        data = json.loads(raw)
    except ValueError:
        text = raw.strip()
        return text[:max_chars] if text else "Halaman kosong."

    page = data.get("data") if isinstance(data, dict) else None
    md = (page or {}).get("markdown") if isinstance(page, dict) else ""
    text = (md or "").strip() or "Halaman kosong."
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n…(dipotong)"
    return text


def _search_depth() -> str:
    """Kedalaman search Firecrawl: 'fast' (SERP polos, default) atau 'content'."""
    try:
        from handlers.proxy.client import get_search_depth
        return get_search_depth()
    except Exception:
        depth = str(getattr(app_config, "SEARCH_DEPTH", "") or "").strip().lower()
        return "content" if depth == "content" else "fast"
