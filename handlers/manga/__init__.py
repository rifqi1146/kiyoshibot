"""Manga reader (`/manga`) — modular package.

Sumber: MangaDex, Maid-Manga, dan nHentai.

Struktur:
- `constants.py` : endpoint API, UA, `NH_HEADERS`, semaphore PDF, tuning cache.
- `state.py`     : cache per-user ber-TTL + lock kepemilikan tombol per pesan.
- `http.py`      : HTTP fetcher (json/html/bytes), resize Pillow, `safe_render_page`,
                  `send_detail_view` (kirim detail sebagai Rich Message).
- `rich.py`      : helper Rich Message Bot API 10.3 untuk detail (`build_nh_detail_rich`,
                  `build_mangadex_detail_rich`, `build_maid_detail_rich`,
                  `send_manga_rich`, `edit_manga_rich`). Cover dikirim sebagai
                  `<img src>` di dalam blok rich, tabel info via `<table compact>`.
- `search.py`    : layer pencarian Rich Message (MangaDex, Maid, nHentai); klik
                  tombol nomor -> hapus pesan search -> kirim detail Rich Message.
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
