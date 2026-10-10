"""Helper Rich Message (Bot API 10.3) untuk halaman hasil pencarian.

Dipakai bersama oleh `/becekku`, `/lendirqu`, `/punish`, `/simontok`, dan
`/nekopoi` supaya tampilan hasil search seragam: heading besar, tabel
`#`/`Title`/`Info`, dan footer `aside`.

Semua fungsi WAJIB punya fallback ke HTML biasa (`parse_mode="HTML"`) supaya
tetap jalan di server Bot API lama / klien tanpa dukungan rich message.
"""
import html
import logging
import math
import warnings
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)


def build_search_content(
    label: str,
    prefix: str,
    search_id: str,
    data: dict,
    *,
    per_page: int = 5,
    indent_meta: bool = True,
    intro: str | None = None,
) -> tuple[str, str, InlineKeyboardMarkup | None]:
    """Bangun `(rich_html, plain_html, markup)` untuk satu halaman hasil search.

    `data` = `{ query, results: [{title, url, meta?}], page }`.
    `meta` opsional; kalau kosong kolom Info diisi `-`.
    """
    page = int(data.get("page") or 0)
    results = data.get("results") or []
    total = len(results)
    max_page = max(1, math.ceil(total / per_page))

    start = page * per_page
    chunk = results[start : start + per_page]
    query = data.get("query") or ""
    indent = "\u00a0\u00a0\u00a0"
    separator = "─" * 24

    if not results:
        rich_html = (
            f"<h1>🔍 {html.escape(label)} Search</h1>"
            f"<p>Query: <code>{html.escape(query)}</code></p>"
            "<hr/>"
            "<p><i>No results found.</i></p>"
        )
        plain_html = (
            f"<b>🔍 {html.escape(label)} Search</b>\n"
            f"<code>{html.escape(query)}</code>\n\n"
            "<i>No results found.</i>"
        )
        return rich_html, plain_html, None

    rows_table = []
    plain_blocks = []
    for i, item in enumerate(chunk):
        idx = start + i + 1
        t_esc = html.escape(item.get("title") or "Unknown title")
        u_esc = html.escape(item.get("url") or "", quote=True)
        meta = item.get("meta") or ""

        info = html.escape(meta) if meta else "-"
        rows_table.append(
            f"<tr><td><b>{idx}</b></td><td><a href=\"{u_esc}\">{t_esc}</a></td><td><code>{info}</code></td></tr>"
        )

        block = f"<b>{idx}.</b> <a href=\"{u_esc}\">{t_esc}</a>"
        if meta:
            block += f"\n{indent}└─ {html.escape(meta)}"
        plain_blocks.append(block)

    rich_html = (
        f"<h1>🔍 {html.escape(label)} Search</h1>"
        f"<p>Query: <code>{html.escape(query)}</code> • Page {page + 1} of {max_page} • {total} results</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>#</th><th>Title</th><th>Info</th></tr>"
        f"{''.join(rows_table)}"
        "</table>"
        "<hr/>"
        f"<aside>💡 {html.escape(intro or 'Select a numbered button below to view and download.')}</aside>"
    )

    plain_html = (
        f"<b>🔍 {html.escape(label)} Search</b>\n"
        f"<code>{html.escape(query)}</code>\n\n"
        + "\n\n".join(plain_blocks)
        + f"\n\n{separator}\n<i>Page {page + 1} of {max_page} · {total} results</i>"
    )

    keyboard = []
    row_nums = [
        InlineKeyboardButton(str(start + i + 1), callback_data=f"{prefix}:dl:{search_id}:{start + i}")
        for i in range(len(chunk))
    ]
    if row_nums:
        keyboard.append(row_nums)

    row_nav = []
    if page > 0:
        row_nav.append(InlineKeyboardButton("Prev", callback_data=f"{prefix}:nav:{search_id}:{page - 1}"))
    row_nav.append(InlineKeyboardButton("Close", callback_data=f"{prefix}:close:{search_id}:0"))
    if page < max_page - 1:
        row_nav.append(InlineKeyboardButton("Next", callback_data=f"{prefix}:nav:{search_id}:{page + 1}"))
    keyboard.append(row_nav)

    return rich_html, plain_html, InlineKeyboardMarkup(keyboard)


def _markup_to_dict(markup) -> dict | None:
    if markup is None:
        return None
    if hasattr(markup, "to_dict"):
        return markup.to_dict()
    if isinstance(markup, dict):
        return markup
    return None


async def send_search_rich(
    bot,
    chat_id: int,
    rich_html: str,
    plain_html: str,
    markup: InlineKeyboardMarkup | None,
    *,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
):
    """Kirim halaman search sebagai rich message; fallback ke HTML biasa."""
    payload: dict = {"chat_id": chat_id, "rich_message": {"html": rich_html}}
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    md = _markup_to_dict(markup)
    if md:
        payload["reply_markup"] = md

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("sendRichMessage", payload)
    except Exception as e:
        log.debug("sendRichMessage (search) fallback | err=%r", e)

    return await bot.send_message(
        chat_id=chat_id,
        text=plain_html,
        reply_markup=markup,
        parse_mode="HTML",
        reply_to_message_id=reply_to_message_id,
        disable_web_page_preview=True,
    )


async def edit_search_rich(
    bot,
    chat_id: int,
    message_id: int,
    rich_html: str,
    plain_html: str,
    markup: InlineKeyboardMarkup | None,
):
    """Edit pesan search jadi rich message; fallback ke HTML biasa."""
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": {"html": rich_html},
    }
    md = _markup_to_dict(markup)
    if md:
        payload["reply_markup"] = md

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("editMessageText", payload)
    except Exception as e:
        log.debug("editMessageText rich (search) fallback | err=%r", e)

    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=plain_html,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.debug("edit_message_text (search) fallback failed | err=%r", e)
        return None
