#!/usr/bin/env python3
"""Resolve Instagram media URLs using the saved mobile-device session.

This is the local replacement for the Neoxr fallback on age-gated posts: it
loads ``data/ig_session.json`` (created by ``scripts/ig_login.py``) and calls the
private mobile endpoint ``media/<pk>/info/`` which is NOT gated the way the
logged-out web GraphQL is.

Usage:
    venv/bin/python scripts/ig_resolve.py <instagram-url-or-shortcode> [...]
    venv/bin/python scripts/ig_resolve.py Dc6e1d1BHa8

Exit codes:
    0  at least one URL resolved
    1  session file missing / not logged in
    2  no URL could be resolved
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SESSION_PATH = REPO_ROOT / "data" / "ig_session.json"


def _client():
    from instagrapi import Client

    cl = Client()
    cl.load_settings(str(SESSION_PATH))
    return cl


def _pk_from_input(cl, raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("empty input")
    if "/" in value or value.startswith("http"):
        return cl.media_pk_from_url(value)
    return cl.media_pk_from_code(value)


def _collect(media) -> list[dict]:
    items = []
    if media.media_type == 8 and media.resources:
        for res in media.resources:
            items.append({
                "type": "video" if res.video_url else "photo",
                "url": str(res.video_url or res.thumbnail_url or ""),
                "thumbnail": str(res.thumbnail_url or ""),
            })
        return items
    if media.video_url:
        items.append({"type": "video", "url": str(media.video_url),
                      "thumbnail": str(media.thumbnail_url or "")})
    elif media.thumbnail_url:
        items.append({"type": "photo", "url": str(media.thumbnail_url),
                      "thumbnail": ""})
    return items


def main() -> int:
    if not SESSION_PATH.exists():
        print(f"No session file at {SESSION_PATH}.")
        print("Run: venv/bin/python scripts/ig_login.py")
        return 1

    targets = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not targets:
        print(__doc__)
        return 1

    import importlib.util

    if importlib.util.find_spec("instagrapi") is None:
        print("instagrapi is not installed. Run: venv/bin/pip install instagrapi")
        return 1

    cl = _client()
    rc = 0
    for raw in targets:
        print("=" * 60)
        print("Input:", raw)
        try:
            pk = _pk_from_input(cl, raw)
            media = cl.media_info(pk)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            rc = 2
            continue
        items = _collect(media)
        print(f"  pk={pk} type={media.media_type} user=@{getattr(media.user, 'username', '?')}")
        print(f"  caption: {(media.caption_text or '')[:70]!r}")
        if not items:
            print("  no media URLs returned")
            rc = 2
            continue
        for i, it in enumerate(items):
            print(f"  [{i}] {it['type']:5s} {it['url'][:90]}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
