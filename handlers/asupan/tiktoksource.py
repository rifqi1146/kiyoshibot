"""Sumber pencarian video TikTok untuk Asupan — memakai library vendored.

Library asli: `handlers/asupan/tiktokapi/` (vendored dari
https://github.com/omkarcloud/tiktok-scraper, MIT — lihat __init__.py).

Modul ini membungkusnya jadi coroutine + pacing, dan menyediakan:
    - search_videos(keyword) -> list[{id, unique_id, link}]
    - warmup()               -> cek library bisa jalan (best-effort)

CATATAN URL (penting):
    `search_videos` mengembalikan `play_link` ber-CD N (`v19-webapp-prime*`)
    yang menolak stream tanpa `tt_chain_token` (HTTP 403). `get_media` /
    `/videos/details` mengembalikan `www.tiktok.com/aweme/v1/play/` yang
    stream-nya hanya valid dari IP region yang meng-host video (di server ini
    sering 403). Karena itu resolve URL untuk dikirim ke Telegram TETAP lewat
    tikwm (lihat fetcher.py) — modul ini hanya SUMBER PENCARIAN.

Pacing: TikTok menghukum volume cepat (signed /api/* balas body kosong = 
TikTokBlocked). Semua panggilan lewat `_GATE` (jarak minimal `_INTERVAL`).
"""

from __future__ import annotations

import os
import time
import asyncio
import logging

log = logging.getLogger(__name__)

# Sumber dinyalakan default; matikan via env ASUPAN_TIKTOKAPI=0.
ENABLED = os.getenv("ASUPAN_TIKTOKAPI", "1").strip().lower() not in ("0", "false", "no")

# Jeda antar panggilan search (detik). Sesi signed sekali pakai per panggilan.
_INTERVAL = float(os.getenv("ASUPAN_TIKTOKAPI_INTERVAL", "2.0"))

_GATE = asyncio.Lock()
_LAST_CALL = 0.0
_AVAILABLE: bool | None = None


def configured() -> bool:
    return ENABLED


def _search_sync(keyword: str) -> list[dict]:
    """Blocking: search via library vendored. Dipanggil dari thread."""
    from .tiktokapi import search_videos as _vendor_search

    payload = _vendor_search(keyword)
    out = []
    for v in (payload.get("videos") or []):
        vid = v.get("id")
        if not vid:
            continue
        author = v.get("author") or {}
        out.append({
            "id": vid,
            "link": v.get("link") or "",
            "unique_id": author.get("username") or "_",
        })
    return out


async def _gated_search(keyword: str) -> list[dict]:
    global _LAST_CALL
    async with _GATE:
        wait = _INTERVAL - (time.monotonic() - _LAST_CALL)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CALL = time.monotonic()
        return await asyncio.to_thread(_search_sync, keyword)


async def search_videos(keyword: str) -> list[dict]:
    """Cari video per keyword. Return list[{id, unique_id, link}] atau []."""
    if not configured():
        return []
    try:
        return await _gated_search(keyword)
    except Exception as e:
        log.warning("[ASUPAN] tiktokapi search gagal | keyword=%r err=%r", keyword, e)
        return []


async def warmup() -> bool:
    """Cek library bisa mengimpor & search jalan (1 panggilan percobaan)."""
    global _AVAILABLE
    if not configured():
        return False
    try:
        from . import tiktokapi  # noqa: F401
    except Exception as e:
        log.warning("[ASUPAN] tiktokapi tidak bisa diimpor | err=%r", e)
        _AVAILABLE = False
        return False
    _AVAILABLE = True
    return True
