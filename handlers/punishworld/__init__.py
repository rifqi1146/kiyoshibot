"""Explorer PunishWorld (`/punish`) — browse, kategori, tags, unduh.

FLOW
----
1. `/punish` tanpa argumen:
   -> Gate premium + NSFW.
   -> Menu Explorer Rich Message (Bot API 10.3) + fallback plain HTML.
   -> Navigasi: Search, Latest, All Categories (126), Tags (900+), shortcuts
      (Anal, BDSM, Deepthroat, Femdom), Close.
2. Dengan argumen -> search `/?s=<q>` (maks 100) sebagai listing Rich Message.
3. Browse:
   - `Latest`      : beranda `/` -> `/page/N/`.
   - `All Categories`: `/categories/` (126 kategori) -> `/<id>-<slug>/page/N/`.
   - `Tags`        : `/10-video-tags/` (900+ tag) -> `/tag/<slug>/page/N/`.
4. Detail post: judul, durasi, views, rating, kategori, tag.
5. Unduh: tombol Video (MP4) atau Audio (MP3) -> `_start_dl_task` ->
   `handlers/dl/punishworld/` (xcdn1.nosofiles.com direct MP4).
"""
from .commands import punish_cmd
from .callbacks import punish_callback

__all__ = ["punish_cmd", "punish_callback"]
