"""Helper Rich Message (Bot API 10.3) untuk manga detail view dan search.

Menyediakan:
- `build_nh_detail_rich`      : nHentai detail (Heading + Cover Img + Table + Aside).
- `build_mangadex_detail_rich`: MangaDex detail (Heading + Cover Img + Table + Details + Aside).
- `build_maid_detail_rich`    : Maid-Manga detail (Heading + Cover Img + Table + Details + Aside).
- `send_manga_rich`           : Pengiriman via `sendRichMessage` dengan fallback plain HTML.
- `edit_manga_rich`           : Edit pesan via `editMessageText` dengan fallback plain HTML.
"""
import html
import logging
import warnings

from telegram import InlineKeyboardMarkup

from utils.rich_common import is_message_not_modified

log = logging.getLogger(__name__)


def build_nh_detail_rich(data: dict, cover_url: str = "") -> tuple[str, str]:
    """-> (rich_html, plain_html) untuk nHentai detail view."""
    raw_title = data.get("title", {}).get("pretty") or data.get("title", {}).get("english") or "Unknown Doujin"
    title = html.escape(raw_title)
    code = html.escape(str(data.get("id") or ""))
    pages = html.escape(str(data.get("num_pages") or ""))
    favs = html.escape(str(data.get("num_favorites") or ""))

    raw_tags = ", ".join(t["name"] for t in (data.get("tags") or []) if t.get("type") == "tag")
    if len(raw_tags) > 300:
        raw_tags = raw_tags[:300] + "…"
    tags = html.escape(raw_tags)

    img_block = f'<img src="{cover_url}"/>' if cover_url else ""

    rich_html = (
        f"<h1>📚 {title}</h1>"
        f"{img_block}"
        "<table compact>"
        f"<tr><td><b>Code</b></td><td><code>{code}</code></td></tr>"
        f"<tr><td><b>Pages</b></td><td>{pages}</td></tr>"
        f"<tr><td><b>Favorites</b></td><td>❤️ {favs}</td></tr>"
        f"<tr><td><b>Tags</b></td><td>{tags}</td></tr>"
        "</table>"
        "<hr/>"
        "<aside>💡 Select an option below to start reading or download as PDF.</aside>"
    )

    plain_html = (
        f"📚 <b>{title}</b>\n\n"
        f"🆔 <b>Code:</b> <code>{code}</code>\n"
        f"📄 <b>Pages:</b> {pages}\n"
        f"❤️ <b>Favorites:</b> {favs}\n"
        f"🏷 <b>Tags:</b> {tags}\n\n"
        "Select an option below to start reading:"
    )

    return rich_html, plain_html


def build_mangadex_detail_rich(manga: dict, cover_url: str = "") -> tuple[str, str]:
    """-> (rich_html, plain_html) untuk MangaDex detail view."""
    attrs = manga.get("attributes") or {}
    titles = attrs.get("title") or {}
    raw_title = titles.get("en") or titles.get("ja-ro") or next(iter(titles.values()), "Unknown Manga")
    title = html.escape(raw_title)

    desc_raw = (attrs.get("description") or {}).get("en") or "No description available."
    if len(desc_raw) > 280:
        desc_raw = desc_raw[:280] + "…"
    desc = html.escape(desc_raw)

    status = html.escape(str(attrs.get("status") or "Unknown").capitalize())
    year = html.escape(str(attrs.get("year") or "-"))
    lang = html.escape(str(attrs.get("originalLanguage") or "-").upper())

    img_block = f'<img src="{cover_url}"/>' if cover_url else ""

    rich_html = (
        f"<h1>📚 {title}</h1>"
        f"{img_block}"
        "<table compact>"
        f"<tr><td><b>Status</b></td><td>{status}</td></tr>"
        f"<tr><td><b>Year</b></td><td>{year}</td></tr>"
        f"<tr><td><b>Language</b></td><td>{lang}</td></tr>"
        "</table>"
        f"<details open><summary>Synopsis</summary><p>{desc}</p></details>"
        "<hr/>"
        "<aside>💡 Select a chapter below to start reading or download as PDF.</aside>"
    )

    plain_html = (
        f"📚 <b>{title}</b>\n\n"
        f"• <b>Status:</b> {status}\n"
        f"• <b>Year:</b> {year}\n"
        f"• <b>Language:</b> {lang}\n\n"
        f"📝 {desc}\n\n"
        "Select a chapter below to read:"
    )

    return rich_html, plain_html


def build_maid_detail_rich(title: str, desc: str, offset: int, total_ch: int, cover_url: str = "") -> tuple[str, str]:
    """-> (rich_html, plain_html) untuk Maid-Manga detail view."""
    title_esc = html.escape(title or "Unknown Manga")
    desc_clean = (desc or "No description available.")[:280] + "…" if len(desc or "") > 280 else (desc or "")
    desc_esc = html.escape(desc_clean)
    ch_range = f"{offset + 1}–{min(offset + 5, total_ch)} of {total_ch}"

    img_block = f'<img src="{cover_url}"/>' if cover_url else ""

    rich_html = (
        f"<h1>📚 {title_esc}</h1>"
        f"{img_block}"
        "<table compact>"
        f"<tr><td><b>Chapters</b></td><td>{ch_range}</td></tr>"
        "</table>"
        f"<details open><summary>Synopsis</summary><p>{desc_esc}</p></details>"
        "<hr/>"
        "<aside>💡 Select a chapter below to start reading or download as PDF.</aside>"
    )

    plain_html = (
        f"📚 <b>{title_esc}</b>\n\n"
        f"🗂 <i>Chapters {ch_range}</i>\n\n"
        f"📝 {desc_esc}\n\n"
        "Select a chapter below to read:"
    )

    return rich_html, plain_html


def _markup_dict(markup):
    if markup is None:
        return None
    if hasattr(markup, "to_dict"):
        return markup.to_dict()
    return markup if isinstance(markup, dict) else None


async def send_manga_rich(
    bot,
    chat_id: int,
    rich_html: str,
    plain_html: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
):
    """Kirim rich message manga; fallback ke sendMessage plain HTML."""
    payload: dict = {"chat_id": chat_id, "rich_message": {"html": rich_html}}
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    md = _markup_dict(markup)
    if md:
        payload["reply_markup"] = md

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("sendRichMessage", payload)
    except Exception as e:
        log.debug("send_manga_rich fallback | err=%r", e)

    return await bot.send_message(
        chat_id=chat_id,
        text=plain_html,
        reply_markup=markup,
        parse_mode="HTML",
        reply_to_message_id=reply_to_message_id,
        disable_web_page_preview=True,
    )


async def edit_manga_rich(
    bot,
    chat_id: int,
    message_id: int,
    rich_html: str,
    plain_html: str,
    markup: InlineKeyboardMarkup | None = None,
):
    """Edit pesan jadi rich message; fallback ke editMessageText plain HTML."""
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": {"html": rich_html},
    }
    md = _markup_dict(markup)
    if md:
        payload["reply_markup"] = md

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("editMessageText", payload)
    except Exception as e:
        if is_message_not_modified(e):
            return None
        log.debug("edit_manga_rich fallback | err=%r", e)

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
        log.debug("edit_manga_rich fallback failed | err=%r", e)
        return None
