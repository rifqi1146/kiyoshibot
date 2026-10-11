"""Command `/nekopoi` — Nekopoi explorer (browse, genre, A-Z, jadwal, unduh).

FLOW
----
1. `/nekopoi` tanpa argumen:
   -> Gate premium + NSFW.
   -> Menu Explorer Rich Message (Bot API 10.3) + fallback plain HTML.
2. `/nekopoi <query>`:
   -> Search `/search/<q>` (maks 100 hasil) sebagai listing Rich Message.
3. Browse:
   - `Latest`      : `div.nk-episodes-area` di beranda.
   - `Categories`  : `/category/<slug>/page/N/` (Hentai, 2D, 3D, JAV, JAV Cosplay).
   - `Genres`      : 78 genre dari `/genre-list/` -> `/genres/<slug>/page/N/`.
   - `A-Z Lists`   : `/hentai-list/` (1058) & `/jav-list/` (2088), picker huruf.
   - `Schedule`    : `/jadwal-new-hentai/` (informasi saja).
4. Detail:
   - Halaman SERIES (`/hentai/<slug>/`) -> metadata + episode picker (Ep N).
   - Halaman EPISODE/post -> langsung format picker (Video/MP3).
5. Unduh:
   -> Format (Video/MP3) -> resolution picker (`nk:` -> `_process_choice`
      -> engine `nekopoi`) -> `handlers/dl/nekopoi/` -> upload Telegram.

Modul di paket ini:
- `constants.py` : LABEL/PREFIX/BASE_URL/UA + batas hasil, kategori, cache.
- `state.py`     : cache sesi + genre + A-Z + jadwal.
- `scraper.py`   : HTTP client, parser kartu/detail/A-Z/jadwal, paginasi.
- `render.py`    : builder UI Rich Message + plain HTML fallback + keyboard.
- `rich.py`      : `send_rich` / `edit_rich` (Bot API 10.3 + fallback).
- `commands.py`  : handler `/nekopoi`.
- `callbacks.py` : handler callback `nk:`.
"""
from .commands import nekopoi_cmd
from .callbacks import nekopoi_callback

__all__ = ["nekopoi_cmd", "nekopoi_callback"]
