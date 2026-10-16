import html
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.rich_search import build_search_content

from .constants import CATS_PER_PAGE, PER_PAGE
from .state import get_categories_cache


def menu_markup(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Search", callback_data=f"{prefix}:prompt:0:0"),
            InlineKeyboardButton("Latest", callback_data=f"{prefix}:sec:terbaru:0"),
        ],
        [
            InlineKeyboardButton("Trending", callback_data=f"{prefix}:sec:trending:0"),
            InlineKeyboardButton("VIP 18+", callback_data=f"{prefix}:sec:vip:0"),
        ],
        [
            InlineKeyboardButton("Bokep Indo", callback_data=f"{prefix}:sec:indo:0"),
            InlineKeyboardButton("JAV Sub Indo", callback_data=f"{prefix}:sec:jav:0"),
        ],
        [
            InlineKeyboardButton("Film Semi", callback_data=f"{prefix}:sec:semi:0"),
            InlineKeyboardButton("Hentai", callback_data=f"{prefix}:sec:hentai:0"),
        ],
        [
            InlineKeyboardButton("Categories", callback_data=f"{prefix}:cats:0:0"),
            InlineKeyboardButton("Close", callback_data=f"{prefix}:close:0:0"),
        ],
    ])


def build_search_prompt_content(label: str, prefix: str) -> tuple[str, str, InlineKeyboardMarkup]:
    lbl = html.escape(label)
    rich_html = (
        f"<h1>🔍 Search {lbl}</h1>"
        "<p>To search, type your command with a keyword:</p>"
        "<pre><code>/simontok &lt;keyword&gt;</code></pre>"
        "<hr/>"
        "<aside>💡 Example: <code>/simontok hijab</code></aside>"
    )
    plain_html = (
        f"<b>🔍 Search {lbl}</b>\n\n"
        "To search, send your command with a keyword:\n"
        "<code>/simontok &lt;keyword&gt;</code>\n\n"
        "Example:\n"
        "<code>/simontok hijab</code>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Back to Menu", callback_data=f"{prefix}:menu:0:0")]
    ])
    return rich_html, plain_html, markup


def build_menu_content(label: str) -> tuple[str, str]:
    lbl = html.escape(label)
    rich_html = (
        f"<h1>🎬 {lbl} Explorer</h1>"
        "<p>Browse and stream Indonesian viral videos, JAV and adult movies in HD.</p>"
        "<hr/>"
        "<h3>✨ Quick Navigation</h3>"
        "<ul>"
        "<li><b>Search</b> — Find videos by keyword</li>"
        "<li><b>Latest</b> — Newest uploads across all genres</li>"
        "<li><b>Trending</b> — Most-viewed viral content</li>"
        "<li><b>VIP 18+</b> — Premium selected releases</li>"
        "<li><b>Bokep Indo</b> — Indonesian viral videos</li>"
        "<li><b>JAV Sub Indo</b> — Japanese AV with Indonesian subtitles</li>"
        "<li><b>Film Semi & Hentai</b> — Erotic cinema and animation</li>"
        "<li><b>Categories</b> — Curated genre categories</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Select an option below to begin browsing.</aside>"
        f"<footer>{lbl} Telegram Service</footer>"
    )
    plain_html = (
        f"<b>🎬 {lbl} Explorer</b>\n"
        "Browse and stream Indonesian viral videos, JAV and adult movies in HD.\n\n"
        "<b>Quick Navigation:</b>\n"
        "• <b>Search</b> — Find videos by keyword\n"
        "• <b>Latest</b> — Newest uploads across all genres\n"
        "• <b>Trending</b> — Most-viewed viral content\n"
        "• <b>VIP 18+</b> — Premium selected releases\n"
        "• <b>Bokep Indo</b> — Indonesian viral videos\n"
        "• <b>JAV Sub Indo</b> — Japanese AV with Indonesian subtitles\n"
        "• <b>Film Semi & Hentai</b> — Erotic cinema and animation\n"
        "• <b>Categories</b> — Curated genre categories\n\n"
        "Select an option below to begin browsing."
    )
    return rich_html, plain_html


def build_results_content(label: str, prefix: str, session_id: str, data: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    for it in data.get("results") or []:
        if not it.get("meta"):
            parts = []
            if it.get("duration"):
                parts.append(it["duration"])
            if it.get("views"):
                parts.append(f"{it['views']} views")
            if it.get("badge"):
                parts.append(it["badge"])
            it["meta"] = "  ·  ".join(parts)

    origin = data.get("origin") or {}
    back_callback = None
    back_label = "Back"
    if origin.get("action") == "cats":
        page = int(origin.get("page") or 0)
        back_callback = f"{prefix}:cats:{page}:0"
        back_label = "Back to Categories"

    return build_search_content(
        label,
        prefix,
        session_id,
        data,
        per_page=PER_PAGE,
        heading=data.get("title") or f"{label} Results",
        number_action="info",
        close_action="menu",
        intro="Select a numbered button below to view details and download.",
        back_callback=back_callback,
        back_label=back_label,
    )


def _pair_table(chunk: list, prefix: str, action: str, picker_page: int = 0) -> tuple[list, str]:
    rows = []
    for i in range(0, len(chunk), 2):
        row = [InlineKeyboardButton(chunk[i]["name"][:28], callback_data=f"{prefix}:{action}:{chunk[i]['slug']}:{picker_page}")]
        if i + 1 < len(chunk):
            row.append(InlineKeyboardButton(chunk[i + 1]["name"][:28], callback_data=f"{prefix}:{action}:{chunk[i + 1]['slug']}:{picker_page}"))
        rows.append(row)
    table = "".join(
        f"<tr><td><b>{html.escape(chunk[i]['name'])}</b></td>"
        f"<td>{('<b>' + html.escape(chunk[i + 1]['name']) + '</b>') if i + 1 < len(chunk) else ''}</td></tr>"
        for i in range(0, len(chunk), 2)
    )
    return rows, table


def build_categories_content(label: str, prefix: str, page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    cats = get_categories_cache().get("list") or []
    total = len(cats)
    max_page = max(1, math.ceil(total / CATS_PER_PAGE)) if total else 1
    page = max(0, min(page, max_page - 1))
    chunk = cats[page * CATS_PER_PAGE: page * CATS_PER_PAGE + CATS_PER_PAGE]

    rows, table = _pair_table(chunk, prefix, "cat", page)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{prefix}:cats:{page - 1}:0"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{prefix}:menu:0:0"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{prefix}:cats:{page + 1}:0"))
    rows.append(nav)

    rich_html = (
        "<h1>📂 Categories</h1>"
        f"<p>Page {page + 1} of {max_page} • {total} categories</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>Category</th><th>Category</th></tr>"
        f"{table}"
        "</table>"
        "<hr/>"
        "<aside>💡 Select a category button below to view its listing.</aside>"
    )
    plain_html = (
        f"<b>📂 Categories</b>\nPage {page + 1} of {max_page} · {total} categories\n\n"
        "Select a category below:"
    )
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_detail_content(label: str, prefix: str, detail: dict, session_id: str, idx: int) -> tuple[str, str, InlineKeyboardMarkup]:
    title = html.escape(detail.get("title") or label)
    rows_table = []
    if detail.get("duration"):
        rows_table.append(f"<tr><td><b>Duration</b></td><td>{html.escape(str(detail['duration']))}</td></tr>")
    if detail.get("views"):
        rows_table.append(f"<tr><td><b>Views</b></td><td>{html.escape(str(detail['views']))}</td></tr>")
    if detail.get("badge"):
        rows_table.append(f"<tr><td><b>Badge</b></td><td>{html.escape(str(detail['badge']))}</td></tr>")
    if detail.get("categories"):
        cats_join = ", ".join(detail["categories"][:8])
        if len(detail["categories"]) > 8:
            cats_join += "…"
        rows_table.append(f"<tr><td><b>Genres</b></td><td>{html.escape(cats_join)}</td></tr>")
    meta_table = f"<table compact>{''.join(rows_table)}</table>" if rows_table else ""

    rich_html = (
        f"<h1>🎬 {title}</h1>"
        f"{meta_table}"
        "<hr/>"
        "<aside>💡 Select a format below to download the media.</aside>"
    )
    plain_parts = [f"<b>🎬 {title}</b>", ""]
    for key, val in (
        ("Duration", detail.get("duration")),
        ("Views", detail.get("views")),
        ("Badge", detail.get("badge")),
        ("Genres", ", ".join((detail.get("categories") or [])[:8])),
    ):
        if val:
            plain_parts.append(f"• <b>{key}:</b> {html.escape(str(val))}")
    plain_parts.append("")
    plain_parts.append("Select a format below to download:")
    plain_html = "\n".join(plain_parts)

    rows = [
        [
            InlineKeyboardButton("Video", callback_data=f"{prefix}:dl:{session_id}:{idx}:video"),
            InlineKeyboardButton("Audio", callback_data=f"{prefix}:dl:{session_id}:{idx}:mp3"),
        ],
    ]
    if detail.get("url"):
        rows.append([InlineKeyboardButton("Open Website", url=detail["url"])])
    rows.append([
        InlineKeyboardButton("Back", callback_data=f"{prefix}:back:{session_id}:0"),
        InlineKeyboardButton("Close", callback_data=f"{prefix}:close:0:0"),
    ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)
