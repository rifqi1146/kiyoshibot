"""Manga reader (`/manga`) — modular package.

Sumber: MangaDex, Maid-Manga, dan nHentai.

Struktur:
- `constants.py` : endpoint API, UA, `NH_HEADERS`, semaphore PDF, tuning cache.
- `state.py`     : cache per-user ber-TTL + lock kepemilikan tombol per pesan.
- `http.py`      : HTTP fetcher (json/html/bytes), resize Pillow, `safe_render_page`.
- `pdf.py`       : worker pembuatan + pengiriman PDF chapter.
- `mangadex.py`  : MangaDex client + UI + callback (`msearch_`, `detailmanga_`, ...).
- `maid.py`      : Maid-Manga scraper + UI + callback (`maiddet_`, `maidread_`, ...).
- `nhentai.py`   : nHentai client + UI + callback (`nhsearch_`, `nhdetail_`, ...).
- `commands.py`  : `/manga <source> <query>`.
- `callbacks.py` : dispatcher callback pusat.
"""
import logging

from .commands import manga_cmd
from .callbacks import manga_callback

log = logging.getLogger(__name__)

try:
    from database.nsfw_db import nsfw_db_init
    nsfw_db_init()
except Exception as e:
    log.warning(f"Failed to initialize manga NSFW database: {e}")

__all__ = ["manga_cmd", "manga_callback"]
