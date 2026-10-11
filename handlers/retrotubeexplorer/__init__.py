"""Explorer RetroTube (`/becekku` & `/lendirqu`) — browse, kategori, tag, unduh.

FLOW
----
1. `/becekku` / `/lendirqu` tanpa argumen:
   -> Gate premium + NSFW.
   -> Menu Explorer Rich Message (Bot API 10.3) + fallback plain HTML.
2. Dengan argumen -> search `/?s=<q>` (maks 100) sebagai listing Rich Message.
3. Browse:
   - `Latest`      : `?filter=latest` (`/page/N/?filter=latest`).
   - `Most Viewed` : `?filter=most-viewed`.
   - `Categories`  : `/categories/` -> `/category/<slug>/page/N/`.
   - `Tags`        : `/tags/` (tag cloud) -> `/tag/<slug>/page/N/`.
4. Detail post: judul, views, tanggal, kategori, tag, embed `voe.sx`.
5. Unduh: `_start_dl_task` -> `handlers/dl/retrotube/` (VOE resolve + upload).

Kedua situs memakai tema WP-Script `retrotube` sehingga selector HTML identik;
satu paket ini melayani keduanya (dikonfigurasi lewat `SITES` di `constants.py`).

Modul di paket ini:
- `constants.py` : `SITES` (label/prefix/base/gate/contoh) + batas & cache.
- `state.py`     : cache sesi + kategori + tag cloud (per situs).
- `scraper.py`   : HTTP client, parser kartu/detail, paginasi, kategori, tag.
- `render.py`    : builder UI Rich Message + plain HTML fallback + keyboard.
- `rich.py`      : `send_rich` / `edit_rich` (Bot API 10.3 + fallback).
- `commands.py`  : handler command per situs.
- `callbacks.py` : handler callback `bq:` / `lq:`.
"""
from .commands import make_site_cmd
from .callbacks import becekku_callback, lendirqu_callback

becekku_cmd = make_site_cmd("becekku")
lendirqu_cmd = make_site_cmd("lendirqu")

__all__ = ["becekku_cmd", "lendirqu_cmd", "becekku_callback", "lendirqu_callback"]
