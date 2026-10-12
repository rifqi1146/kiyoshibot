"""Command handler `/manga <source> <query/code>`.

Search manga memakai Rich Message (Bot API 10.3) yang seragam dengan `/simontok`,
`/punish`, dan `/nekopoi` (lihat `search.py`). Klik tombol nomor -> pesan hasil
search dihapus, lalu detail dikirim sebagai foto cover + caption.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import premium
from database.nsfw_db import is_nsfw_allowed
from handlers.join import require_join_or_block
from utils.menu_lock import register_message_owner
from utils.rich_search import edit_search_rich

from .constants import NH_API_URL, NH_HEADERS
from .http import _escape, fetch_json
from .nhentai import fetch_detail_view as nh_fetch_detail_view
from .search import (
    LABEL_BY_SOURCE,
    collect_results,
    get_search,
    new_search_id,
    render_search_page,
    store_search,
)

log = logging.getLogger(__name__)

_SOURCE_ALIASES = {
    "dex": "dex", "mangadex": "dex",
    "maid": "maid", "maidmanga": "maid",
    "nh": "nh", "nhentai": "nh",
}


def _is_nsfw_enabled(chat_id: int, chat_type: str) -> bool:
    return is_nsfw_allowed(chat_id, chat_type)


async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not _is_nsfw_enabled(chat.id, chat.type):
        return await msg.reply_text("❌ NSFW is not enabled in this group.")

    if not context.args or len(context.args) < 2:
        help_txt = (
            "⚠️ <b>Invalid format!</b>\n\n"
            "Use: <code>/manga &lt;source&gt; &lt;title/code&gt;</code>\n\n"
            "📚 <b>Sources:</b> <code>dex</code> | <code>maid</code> | <code>nh</code>\n"
            "💡 <b>Examples:</b>\n"
            "<code>/manga dex Haimiya-senpai</code>\n"
            "<code>/manga maid Osananajimi wo Onnanoko</code>\n"
            "<code>/manga nh Zenless Zone Zero Or 642563</code>\n"
        )
        return await msg.reply_text(help_txt, parse_mode="HTML")

    source = _SOURCE_ALIASES.get(context.args[0].lower())
    full_query = " ".join(context.args[1:]).strip()
    user = update.effective_user

    if source is None:
        return await msg.reply_text(
            "❌ <b>Unknown source.</b>\nChoose <code>dex</code>, <code>maid</code>, or <code>nh</code>.",
            parse_mode="HTML",
        )

    if source == "nh" and (not user or not premium.check(user.id)):
        return await msg.reply_text("❌ NH manga is only available for premium users.")

    status = await msg.reply_text(
        f"🔍 Searching <code>{_escape(full_query)}</code> in <b>{_escape(LABEL_BY_SOURCE[source])}</b>...",
        parse_mode="HTML",
    )
    register_message_owner(status.chat_id, status.message_id, user.id if user else 0)

    # nHentai: input kode numerik -> langsung buka detail Rich Message.
    if source == "nh" and full_query.isdigit():
        await status.edit_text("⏳ Loading details...", parse_mode="HTML")
        rich_html, plain_html, markup, cover_url = await nh_fetch_detail_view(full_query)
        if not rich_html:
            return await status.edit_text("❌ Doujin not found (invalid code).", parse_mode="HTML")
        from .http import send_detail_view
        from .state import set_reply_target
        try:
            await status.delete()
        except Exception:
            pass
        sent = await send_detail_view(
            context, status.chat_id, getattr(status, "message_thread_id", None),
            rich_html, plain_html, markup, owner_id=user.id if user else 0,
            reply_to_message_id=msg.message_id,
        )
        if sent:
            msg_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
            if msg_id:
                set_reply_target(status.chat_id, msg_id, msg.message_id)
        return

    results = await collect_results(source, full_query, context)
    if not results:
        return await status.edit_text(
            f"❌ No results for <code>{_escape(full_query)}</code> in <b>{_escape(LABEL_BY_SOURCE[source])}</b>.",
            parse_mode="HTML",
        )

    sid = new_search_id()
    store_search(context, sid, {
        "source": source,
        "query": full_query,
        "results": results,
        "page": 0,
        "user_id": user.id if user else 0,
        "chat_id": chat.id,
        "cmd_msg_id": msg.message_id,
    })
    rich, plain, markup = render_search_page(source, sid, get_search(context, sid))
    await edit_search_rich(context.bot, status.chat_id, status.message_id, rich, plain, markup)
