
import asyncio
import importlib.util
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
SESSION_PATH = Path(os.getenv("IG_SESSION_PATH", str(REPO_ROOT / "data" / "ig_session.json")))

IG_SESSION_ENABLED = os.getenv("IG_SESSION_ENABLED", "1").lower() in ("1", "true", "on", "yes")
IG_SESSION_TTL = float(os.getenv("IG_SESSION_TTL", "1800"))

_client = None
_client_lock = asyncio.Lock()
_session_failed_at = 0.0
_SESSION_RETRY_COOLDOWN = 600.0


def _instagrapi_available() -> bool:
    return importlib.util.find_spec("instagrapi") is not None


def session_available() -> bool:
    if not IG_SESSION_ENABLED or not _instagrapi_available():
        return False
    return SESSION_PATH.exists()


def _build_client():
    from instagrapi import Client

    cl = Client()
    cl.load_settings(str(SESSION_PATH))
    return cl


async def _get_client():
    global _client
    async with _client_lock:
        if _client is None:
            _client = await asyncio.to_thread(_build_client)
        return _client


def reset_session():
    global _client
    _client = None


def _pk_from_shortcode(cl, shortcode: str) -> str:
    return cl.media_pk_from_code(shortcode)


def _collect_items(media) -> list[dict]:
    items: list[dict] = []
    resources = getattr(media, "resources", None)
    if getattr(media, "media_type", 0) == 8 and resources:
        for res in resources:
            url = str(getattr(res, "video_url", "") or getattr(res, "thumbnail_url", "") or "")
            if not url:
                continue
            items.append({
                "type": "video" if getattr(res, "video_url", None) else "photo",
                "url": url,
                "thumbnail": str(getattr(res, "thumbnail_url", "") or ""),
                "width": 0,
                "height": 0,
            })
        return items

    video_url = getattr(media, "video_url", None)
    thumb = str(getattr(media, "thumbnail_url", "") or "")
    if video_url:
        items.append({"type": "video", "url": str(video_url), "thumbnail": thumb, "width": 0, "height": 0})
    elif thumb:
        items.append({"type": "photo", "url": thumb, "thumbnail": "", "width": 0, "height": 0})
    return items


def _resolve_blocking(shortcode: str) -> dict | None:
    cl = _build_client()
    pk = _pk_from_shortcode(cl, shortcode)
    media = cl.media_info(pk)
    items = _collect_items(media)
    if not items:
        return None
    user = getattr(media, "user", None)
    return {
        "caption": str(getattr(media, "caption_text", "") or ""),
        "username": str(getattr(user, "username", "") or ""),
        "nickname": str(getattr(user, "full_name", "") or ""),
        "items": items,
        "source": "Instagram Session",
    }


async def fetch_media_via_session(shortcode: str) -> dict | None:
    global _session_failed_at

    if not IG_SESSION_ENABLED:
        return None
    if not shortcode:
        return None
    if not _instagrapi_available():
        return None
    if not SESSION_PATH.exists():
        return None
    if _session_failed_at and (time.monotonic() - _session_failed_at) < _SESSION_RETRY_COOLDOWN:
        return None

    try:
        data = await asyncio.to_thread(_resolve_blocking, shortcode)
    except Exception as exc:
        _session_failed_at = time.monotonic()
        log.warning("Instagram mobile session resolve failed | code=%s err=%r", shortcode, exc)
        reset_session()
        return None

    if not data:
        return None
    log.info(
        "Instagram mobile session resolve ok | code=%s user=%r items=%s",
        shortcode, data.get("username"), len(data.get("items") or []),
    )
    return data
