"""Explorer CosXplay (`/cosxplay`) — browse, kategori, tags, models, unduh.

FLOW
----
1. `/cosxplay` tanpa argumen:
   -> Gate premium + NSFW.
   -> Menu Explorer Rich Message (Bot API 10.3) + fallback plain HTML.
   -> Navigasi: Search, Latest, Categories (176), Tags (900+),
      Cosplay Models, shortcut (Popular Costumes, Maid, Schoolgirl, 2B Nier),
      Close.
2. Dengan argumen -> search `/?s=<q>` (maks 100) sebagai listing Rich Message.
3. Browse:
   - `Latest`         : beranda `/` -> `/page/N/`.
   - `Categories`     : `/categories/` (176 kategori) -> `/<id>-<slug>/page/N/`.
   - `Tags`           : `/9-video-tags/` (900+ tag) -> `/tag/<slug>/page/N/`.
   - `Cosplay Models` : `/11314-video-actors/` -> `/?actors=<slug>`.
4. Detail post: judul, durasi, views, rating, kategori, tag.
5. Unduh: tombol Video (MP4) atau Audio (MP3) -> `_process_choice` ->
   resolution picker -> `handlers/dl/cosxplay/` (xcdn1.nosofiles.com direct MP4).
"""
from .commands import cosxplay_cmd
from .callbacks import cosxplay_callback

__all__ = ["cosxplay_cmd", "cosxplay_callback"]
