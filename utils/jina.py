"""Grounding web via Jina AI (Search + Reader).

- web_search(query):  https://s.jina.ai?q=...  — SERP + snippet halaman
- read_url(url):      https://r.jina.ai/<url>  — konten halaman jadi markdown
Butuh JINA_API_KEY (utils.config). Tanpa key, modul menolak dengan jelas
kecuali dipakai versi gratis yang rate-limit-nya ketat.
"""
import json
import logging

import aiohttp

from utils.config import JINA_API_KEY
from utils.http import get_http_session

log = logging.getLogger(__name__)

SEARCH_URL = "https://s.jina.ai"
READ_URL = "https://r.jina.ai"
_TIMEOUT = aiohttp.ClientTimeout(total=45)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {JINA_API_KEY}",
        "Accept": "application/json",
        "X-Retain-Images": "none",
        "X-With-Links-Summary": "false",
        # locale bikin hasil search relevan buat user Indonesia
        "X-Locale": "id-ID",
    }


def _require_key():
    if not JINA_API_KEY:
        raise RuntimeError("JINA_API_KEY belum diset di env.")


async def web_search(query: str, max_results: int = 5) -> str:
    """Search web via s.jina.ai, kembalikan SERP ringkas (judul+url+snippet)."""
    _require_key()
    query = (query or "").strip()
    if not query:
        raise ValueError("query kosong")

    params = {
        "q": query,
        "num": max_results,
        "timeout": 20,
    }
    session = await get_http_session()
    async with session.get(SEARCH_URL, params=params, headers=_headers(), timeout=_TIMEOUT) as resp:
        raw = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"jina search {resp.status}: {raw[:300]}")

    try:
        data = json.loads(raw)
    except ValueError:
        # s.jina.ai balas markdown/teks polos kalau Accept tidak dihormati
        text = raw.strip()
        return text[:6000] if text else "Tidak ada hasil pencarian."

    results = data.get("data") or []
    lines = [f"### Hasil pencarian: {query}"]
    for item in results[:max_results]:
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        snippet = (item.get("description") or item.get("content") or "").strip()
        if len(snippet) > 600:
            snippet = snippet[:600] + "…"
        if title and url:
            lines.append(f"- **{title}**\n  {url}\n  {snippet}")
    if len(lines) == 1:
        return "Tidak ada hasil pencarian."
    return "\n".join(lines)


async def read_url(url: str, max_chars: int = 8000) -> str:
    """Baca halaman via r.jina.ai, kembalikan markdown diringkas."""
    _require_key()
    url = (url or "").strip()
    if not url:
        raise ValueError("url kosong")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    session = await get_http_session()
    headers = _headers()
    headers["Accept"] = "text/plain"
    async with session.get(
        READ_URL + "/" + url,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"jina reader {resp.status}: {text[:300]}")

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n…(dipotong)"
    return text.strip() or "Halaman kosong."


def extract_urls(text: str) -> list[str]:
    """Ambil URL http(s) yang disebut user di prompt (buat dibaca langsung)."""
    import re

    found = re.findall(r"https?://[^\s<>\]\)\"']+", text or "")
    seen: set[str] = set()
    out = []
    for u in found:
        u = u.rstrip(".,;:!?")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:3]
