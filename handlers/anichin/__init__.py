"""Command `/anichin` — Anichin explorer (browse, genre, A-Z, jadwal, unduh Donghua).

FLOW
----
1. `/anichin` tanpa argumen:
   -> Gate premium **tanpa NSFW** (`anichin.moe` ada di `PREMIUM_NO_NSFW_DOMAINS`).
   -> Menu Explorer Rich Message (Bot API 10.3) + fallback plain HTML.
2. `/anichin <query>` -> search `/?s=<q>` (maks 100) sebagai listing Rich Message.
3. Browse:
   - `Donghua List` : `/anime` (`/page/N/`).
   - `Ongoing`      : `/ongoing/`.
   - `Completed`    : `/completed/` (15 halaman).
   - `Upcoming`     : `/upcoming-donghua/`.
   - `Drop`         : `/drop/`.
   - `Genres`       : 47 genre dari `/genres/` -> `/genres/<slug>/page/N/`.
   - `A–Z List`     : huruf A-Z -> `/az-lists/?show=<L>` + `/az-lists/page/N/`.
   - `Schedule`     : `/schedule/` per hari (informasi saja).
4. Detail:
   - Halaman SERIES -> metadata + episode picker (`div.eplister`).
   - Halaman EPISODE -> mirror unduh + daftar server streaming.
5. Unduh:
   -> Format (Video/MP3) -> resolution picker (`ac:` -> `_process_choice` ->
      engine `anichin`) -> `handlers/dl/anichin/` (resolve shortener ->
      mirrored.to -> host file) -> upload Telegram.

Modul di paket ini:
- `constants.py` : LABEL/PREFIX/BASE_URL/UA + batas, tombol browse, hari jadwal.
- `state.py`     : cache sesi + genre + A-Z + jadwal.
- `scraper.py`   : HTTP client, parser kartu/detail/episode, genre, A-Z, jadwal.
- `render.py`    : builder UI Rich Message + plain HTML fallback + keyboard.
- `rich.py`      : `send_rich` / `edit_rich` (Bot API 10.3 + fallback).
- `commands.py`  : handler `/anichin`.
- `callbacks.py` : handler callback `ac:`.
"""
from .commands import anichin_cmd
from .callbacks import anichin_callback

__all__ = ["anichin_cmd", "anichin_callback"]
