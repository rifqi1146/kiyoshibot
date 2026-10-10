"""Vendored TikTok web-API client (search + media).

Source: https://github.com/omkarcloud/tiktok-scraper
License: MIT — Copyright (c) 2026 Chetan Jain

`signer.py` additionally carries its own provenance header: it is a pure-Python
port of TikTok's `webmssdk` (X-Dynosaur / X-Gnarly), vendored from
`Evil0ctal/Douyin_TikTok_Download_API` (Apache-2.0).

Only the pieces needed by the Asupan feature are vendored here:
search videos by keyword + fetch a video's media/play link — no server, no
external process. `marshmallow`-based schema/validation modules were dropped
because this is called as a Python library, not as an HTTP API.

Do NOT edit the vendored files unless re-vendoring from upstream. When the
signer bundle is bumped upstream and signed /api/* calls start returning HTTP
200 with an empty body, pull the new `signer.py` from upstream.
"""
from .search import search_videos, search_users
from .videos import get_media, get_details
from .refs import resolve_video, resolve_user

__all__ = [
    "search_videos",
    "search_users",
    "get_media",
    "get_details",
    "resolve_video",
    "resolve_user",
]
