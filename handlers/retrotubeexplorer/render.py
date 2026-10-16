import html
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.rich_search import build_search_content

from .constants import (
    CATS_PER_PAGE,
    PER_PAGE,
    TAGS_PER_PAGE,
)
from .state import get_categories_cache, get_tags_cache

COMMANDS = {"bq": "becekku", "lq": "lendirqu"}


def menu_markup(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Search", callback_data=f"{prefix}:prompt:0:0"),
            InlineKeyboardButton("Latest", callback_data=f"{prefix}:latest:0:0"),
        ],
        [
            InlineKeyboardButton("Most Viewed", callback_data=f"{prefix}:viewed:0:0"),
            InlineKeyboardButton("Categories", callback_data=f"{prefix}:cats:0:0"),
        ],
        [
            InlineKeyboardButton("Tags", callback_data=f"{prefix}:tags:0:0"),
            InlineKeyboardButton("Close", callback_data=f"{prefix}:close:0:0"),
        ],
    ])


def build_search_prompt_content(label: str, prefix: str) -> tuple[str, str, InlineKeyboardMarkup]:
    lbl = html.escape(label)
    cmd = COMMANDS.get(prefix, prefix)
    rich_html = (
        f"<h1>🔍 Search {lbl}</h1>"
        "<p>To search, type your command with a keyword:</p>"
        f"<pre><code>/{cmd} &lt;keyword&gt;</code></pre>"
        "<hr/>"
        f"<aside>💡 Example: <code>/{cmd} bocil</code></aside>"
    )
    plain_html = (
        f"<b>🔍 Search {lbl}</b>\n\n"
        "To search, send your command with a keyword:\n"
        f"<code>/{cmd} &lt;keyword&gt;</code>\n\n"
        "Example:\n"
        f"<code>/{cmd} bocil</code>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Back to Menu", callback_data=f"{prefix}:menu:0:0")]
    ])
    return rich_html, plain_html, markup


def build_menu_content(label: str) -> tuple[str, str]:
    lbl = html.escape(label)
    rich_html = (
        f"<h1>🎬 {lbl} Explorer</h1>"
        "<p>Browse and download Indonesian viral videos in HD.</p>"
        "<hr/>"
        "<h3>✨ Quick Navigation</h3>"
        "<ul>"
        "<li><b>Search</b> — Find videos by keyword</li>"
        "<li><b>Latest</b> — Newest uploads</li>"
        "<li><b>Most Viewed</b> — Most popular videos</li>"
        "<li><b>Categories</b> — Curated categories</li>"
        "<li><b>Tags</b> — Hundreds of tags: bocil, hijab, tante, chindo, etc.</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Select an option below to begin browsing.</aside>"
        f"<footer>{lbl} Telegram Service</footer>"
    )
    plain_html = (
        f"<b>🎬 {lbl} Explorer</b>\n"
        "Browse and download Indonesian viral videos in HD.\n\n"
        "<b>Quick Navigation:</b>\n"
        "• <b>Search</b> — Find videos by keyword\n"
        "• <b>Latest</b> — Newest uploads\n"
        "• <b>Most Viewed</b> — Most popular videos\n"
        "• <b>Categories</b> — Curated categories\n"
        "• <b>Tags</b> — Hundreds of tags: bocil, hijab, tante, chindo, etc.\n\n"
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
            if it.get("rating"):
                parts.append(it["rating"])
            it["meta"] = "  ·  ".join(parts)

    origin = data.get("origin") or {}
    back_callback = None
    back_label = "Back"
    if origin.get("action") in ("tags", "cats"):
        page = int(origin.get("page") or 0)
        back_callback = f"{prefix}:{origin['action']}:{page}:0"
        back_label = "Back to Tags" if origin["action"] == "tags" else "Back to Categories"

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
    cats = get_categories_cache(prefix).get("list") or []
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


def build_tags_content(label: str, prefix: str, page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    tags = get_tags_cache(prefix).get("list") or []
    total = len(tags)
    max_page = max(1, math.ceil(total / TAGS_PER_PAGE)) if total else 1
    page = max(0, min(page, max_page - 1))
    chunk = tags[page * TAGS_PER_PAGE: page * TAGS_PER_PAGE + TAGS_PER_PAGE]

    rows, table = _pair_table(chunk, prefix, "tag", page)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{prefix}:tags:{page - 1}:0"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{prefix}:menu:0:0"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{prefix}:tags:{page + 1}:0"))
    rows.append(nav)

    rich_html = (
        "<h1>🏷 Tags</h1>"
        f"<p>Page {page + 1} of {max_page} • {total} tags</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>Tag</th><th>Tag</th></tr>"
        f"{table}"
        "</table>"
        "<hr/>"
        "<aside>💡 Select a tag button below to view matching videos.</aside>"
    )
    plain_html = (
        f"<b>🏷 Tags</b>\nPage {page + 1} of {max_page} · {total} tags\n\n"
        "Select a tag below:"
    )
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_detail_content(label: str, prefix: str, detail: dict, session_id: str, idx: int) -> tuple[str, str, InlineKeyboardMarkup]:
    title = html.escape(detail.get("title") or label)
    rows_table = []
    if detail.get("views"):
        rows_table.append(f"<tr><td><b>Views</b></td><td>{html.escape(str(detail['views']))}</td></tr>")
    if detail.get("date"):
        rows_table.append(f"<tr><td><b>Date</b></td><td>{html.escape(str(detail['date']))}</td></tr>")
    if detail.get("categories"):
        rows_table.append(f"<tr><td><b>Category</b></td><td>{html.escape(', '.join(detail['categories']))}</td></tr>")
    if detail.get("tags"):
        tags_join = ", ".join(detail["tags"][:12])
        if len(detail["tags"]) > 12:
            tags_join += "…"
        rows_table.append(f"<tr><td><b>Tags</b></td><td>{html.escape(tags_join)}</td></tr>")
    meta_table = f"<table compact>{''.join(rows_table)}</table>" if rows_table else ""

    rich_html = (
        f"<h1>🎬 {title}</h1>"
        f"{meta_table}"
        "<hr/>"
        "<aside>💡 Tap <b>Download</b> below to grab the video.</aside>"
    )
    plain_parts = [f"<b>🎬 {title}</b>", ""]
    for key, val in (
        ("Views", detail.get("views")),
        ("Date", detail.get("date")),
        ("Category", ", ".join(detail.get("categories") or [])),
        ("Tags", ", ".join((detail.get("tags") or [])[:12])),
    ):
        if val:
            plain_parts.append(f"• <b>{key}:</b> {html.escape(str(val))}")
    plain_parts.append("")
    plain_parts.append("Tap Download to grab the video.")
    plain_html = "\n".join(plain_parts)

    rows = [[InlineKeyboardButton("Download", callback_data=f"{prefix}:dl:{session_id}:{idx}")]]
    if detail.get("url"):
        rows.append([InlineKeyboardButton("Open Website", url=detail["url"])])
    rows.append([
        InlineKeyboardButton("Back", callback_data=f"{prefix}:back:{session_id}:0"),
        InlineKeyboardButton("Close", callback_data=f"{prefix}:close:0:0"),
    ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)
