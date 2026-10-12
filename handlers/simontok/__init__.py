"""Explorer Simontok (`/simontok`) — browse, kategori, unduh.

FLOW
----
1. `/simontok` tanpa argumen:
   -> Gate premium + NSFW.
   -> Menu Explorer Rich Message (Bot API 10.3) + fallback plain HTML.
   -> Navigasi: Search, Latest, Trending, VIP, Indo, JAV, Semi, Hentai, Categories.
2. Dengan argumen -> search `/?s=<q>` (maks 100) sebagai listing Rich Message.
3. Browse:
   - `Latest`      : `/terbaru/page/N/`
   - `Trending`    : `/trending/page/N/`
   - `VIP 18+`     : `/vip/page/N/`
   - `Bokep Indo`  : `/indo/page/N/`
   - `JAV Sub Indo`: `/jav/page/N/`
   - `Film Semi`   : `/semi/page/N/`
   - `Hentai`      : `/hentai/page/N/`
   - `Categories`  : `div.smt-kategori` -> `/genre/<slug>/page/N/`.
4. Detail post: judul, thumbnail, durasi, views, badge, genre tags.
5. Unduh: tombol Video (MP4) atau Audio (MP3) -> `_start_dl_task` ->
   `handlers/dl/simontok/` (HLS decrypt AES-256-GCM + concat segmen paralel).
"""
from .commands import simontok_cmd
from .callbacks import simontok_callback

__all__ = ["simontok_cmd", "simontok_callback"]
