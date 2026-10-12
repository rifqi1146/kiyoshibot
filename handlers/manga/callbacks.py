"""Central callback query dispatcher untuk manga reader.

Routing berdasarkan prefix:
- `mgdex:` / `mgmaid:` / `mgnh:` -> layer search Rich Message (`search.py`):
  `nav`, `open` (hapus pesan search + kirim foto detail), `close`.
- `detailmanga_`, `readmanga_`, `switchch_`, `nav_`, `pdfdex_` -> MangaDex.
- `maiddet_`, `maidread_`, `maidnav_`, `pdfmaid_` -> Maid-Manga.
- `nhdetail_`, `nhread_`, `nhnav_`, `pdfnh_` -> nHentai.
- `ignore`, `close_manga` -> umum.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import premium
from database.nsfw_db import is_nsfw_allowed
from handlers.join import require_join_or_block

from . import search
from .maid import handle_maid_callback
from .mangadex import handle_mangadex_callback
from .nhentai import handle_nhentai_callback
from .state import _clear_message_lock, _ensure_callback_lock

log = logging.getLogger(__name__)

_SEARCH_PREFIXES = ("mgdex:", "mgmaid:", "mgnh:")


def _is_nsfw_enabled(chat_id: int, chat_type: str) -> bool:
    return is_nsfw_allowed(chat_id, chat_type)


async def manga_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    query = update.callback_query
    data = query.data

    if not _is_nsfw_enabled(query.message.chat_id, query.message.chat.type):
        return await query.answer("❌ NSFW is not enabled in this group.", show_alert=True)

    if not await _ensure_callback_lock(query):
        return

    # nHentai gate premium
    if data.startswith(("mgnh:", "nhdetail_", "nhread_", "nhnav_", "pdfnh_")):
        user = update.effective_user
        if not user or not premium.check(user.id):
            return await query.answer("❌ NH manga is only available for premium users.", show_alert=True)

    if data == "ignore":
        return await query.answer()

    if data == "close_manga":
        await query.answer("Reader closed.")
        _clear_message_lock(query.message.chat_id, query.message.message_id)
        try:
            return await query.message.delete()
        except Exception:
            return

    # Layer search Rich Message
    if data.startswith(_SEARCH_PREFIXES):
        return await search.handle_search_callback(query, context, data)

    # Detail / reader per provider
    if data.startswith(("detailmanga_", "readmanga_", "switchch_", "nav_", "pdfdex_")):
        return await handle_mangadex_callback(query, context, data)

    if data.startswith(("maiddet_", "maidread_", "maidnav_", "pdfmaid_")):
        return await handle_maid_callback(query, context, data)

    if data.startswith(("nhdetail_", "nhread_", "nhnav_", "pdfnh_")):
        return await handle_nhentai_callback(query, context, data)

    log.debug("Unhandled manga callback: %s", data)
