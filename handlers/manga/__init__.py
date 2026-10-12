"""Manga reader (`/manga`) — modular package.

Sumber: MangaDex, Maid-Manga, dan nHentai.

Struktur:
- `constants.py` : endpoint API, UA, `NH_HEADERS`, semaphore PDF, tuning cache.
- `state.py`     : cache per-user ber-TTL + lock kepemilikan tombol per pesan.
- `http.py`      : HTTP fetcher (json/html/bytes), resize Pillow, `safe_render_page`,
                  `send_detail_photo` (kirim foto cover + caption).
- `search.py`    : layer pencarian Rich Message (MangaDex, Maid, nHentai); klik
                  tombol nomor -> hapus pesan search -> kirim foto cover + caption.
- `pdf.py`       : worker pembuatan + pengiriman PDF chapter.
- `mangadex.py`  : MangaDex client (`fetch_detail_view`, reader, `detailmanga_`, ...).
- `maid.py`      : Maid-Manga scraper (`fetch_detail_view`, reader, `maiddet_`, ...).
- `nhentai.py`   : nHentai client (`fetch_detail_view`, reader, `nhdetail_`, ...).
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
