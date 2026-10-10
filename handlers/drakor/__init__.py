"""Command `/drakorid` — search, browse, and download Asian dramas on Drakor.id.

FLOW
----
1. `/drakorid` without args:
   -> Gate premium user (no NSFW requirement).
   -> Displays the Drakor.id Explorer Menu using Rich Messages (Bot API 10.3)
      with graceful fallback to standard HTML.
2. `/drakorid <query>`:
   -> Search on `https://drakorid.co/cari.html?q=<q>` (max 100 results).
3. Category / Latest browsing:
   -> `https://drakorid.co/list/<page>` or `/kategori/<slug>/<page>` (max 100).
4. Detail view:
   -> `https://drakorid.co/nonton/<slug>/` (title, episodes, genres, synopsis).
   -> `Download` opens the Episode Picker grid.
5. Episode selection & resolution picker:
   -> Episode grid (`Ep N`) -> format (Video/MP3) -> resolution picker
      -> download via `handlers/dl/drakorid/` and upload to Telegram.

Modul di paket ini:
- `constants.py` : LABEL/PREFIX/BASE_URL/UA + batas hasil & cache.
- `state.py`     : cache sesi + cache kategori + purge.
- `scraper.py`   : HTTP client, parser kartu, fetch kategori/detail, paginasi.
- `render.py`    : builder UI Rich Message + plain HTML fallback + keyboard.
- `rich.py`      : `send_rich` / `edit_rich` (Bot API 10.3 + fallback).
- `commands.py`  : handler `/drakorid`.
- `callbacks.py` : handler callback `dk:`.
"""
from .commands import drakorid_cmd
from .callbacks import drakorid_callback

__all__ = ["drakorid_cmd", "drakorid_callback"]
