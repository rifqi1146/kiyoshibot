"""Command `/moviebox` — search, browse, and download movies & TV series from MovieBox.

FLOW
----
1. `/moviebox` without args:
   -> Gate premium user (no NSFW requirement).
   -> Displays the MovieBox Explorer Menu using Rich Messages (Bot API 10.3)
      with graceful fallback to standard HTML.
2. `/moviebox <query>`:
   -> Search on `https://officialmoviebox.com/newWeb/searchResult?keyword=<q>`.
3. Browse:
   -> Trending via MovieBox API `subject/trending` (all / movies / TV series).
4. Detail view:
   -> MovieBox API `detail?detailPath=<slug>` (title, synopsis, seasons, episodes).
   -> `Download` opens the episode picker (TV) or format picker (movie).
5. Stream resolve & download:
   -> MovieBox API `subject/play` returns direct MP4 streams (1080p/720p/480p/360p).
   -> Download via `handlers/dl/moviebox/` and upload to Telegram.

Modules:
- `constants.py` : LABEL/PREFIX/BASE_URL/API_BASE/UA + limits.
- `state.py`     : session cache + purge.
- `scraper.py`   : search, trending, detail, stream resolution.
- `render.py`    : Rich Message builder + plain HTML fallback + keyboards.
- `rich.py`      : `send_rich` / `edit_rich` (Bot API 10.3 + fallback).
- `commands.py`  : `/moviebox` handler.
- `callbacks.py` : `mb:` callback handler.

NOTE: `commands` and `callbacks` are exported LAZILY (PEP 562). Importing them
eagerly would create a circular import: `handlers.dl.moviebox.main` ->
`handlers.moviebox.scraper` -> `handlers.moviebox.__init__` -> `callbacks` ->
`handlers.dl.router` (still initializing). The `handlers.dl.*` downloader
packages must stay importable without pulling in the UI/router layer.
"""

__all__ = ["moviebox_cmd", "moviebox_callback"]


def __getattr__(name):
    if name == "moviebox_cmd":
        from .commands import moviebox_cmd
        return moviebox_cmd
    if name == "moviebox_callback":
        from .callbacks import moviebox_callback
        return moviebox_callback
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
