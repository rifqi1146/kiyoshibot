"""Renderer Rich Message (Bot API 10.3) + plain HTML fallback untuk /drakorid.

Setiap builder mengembalikan tuple:
`(rich_html, plain_html, markup)` atau `(rich_html, plain_html)`.
"""
import html
import math
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .constants import CATS_PER_PAGE, LABEL, PER_PAGE, PREFIX
from .state import CATEGORIES_CACHE


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Search", callback_data=f"{PREFIX}:prompt:search"),
            InlineKeyboardButton("Latest", callback_data=f"{PREFIX}:latest"),
        ],
        [
            InlineKeyboardButton("Drama Korea", callback_data=f"{PREFIX}:cat:drama-korea"),
            InlineKeyboardButton("Film Korea", callback_data=f"{PREFIX}:cat:film-korea"),
        ],
        [
            InlineKeyboardButton("Drama China", callback_data=f"{PREFIX}:cat:drama-china"),
            InlineKeyboardButton("Romance", callback_data=f"{PREFIX}:cat:romance"),
        ],
        [
            InlineKeyboardButton("Categories", callback_data=f"{PREFIX}:cats:0"),
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
        ],
    ])


def build_menu_content() -> tuple[str, str]:
    rich_html = (
        "<h1>🎬 Drakor.id Explorer</h1>"
        "<p>Watch and download Korean &amp; Asian dramas, movies, and variety shows in HD.</p>"
        "<hr/>"
        "<h3>✨ Quick Navigation</h3>"
        "<ul>"
        "<li><b>Search Drama</b> — Find titles by keyword or actor</li>"
        "<li><b>Latest Episodes</b> — Daily fresh uploads and ongoing dramas</li>"
        "<li><b>Category Browse</b> — 60+ genres: Romance, Comedy, Action, etc.</li>"
        "<li><b>HD Video Download</b> — Fast direct MP4 downloads</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Select an option below to begin browsing.</aside>"
        "<footer>Drakor.id Telegram Service</footer>"
    )
    plain_html = (
        "<b>🎬 Drakor.id Explorer</b>\n"
        "Watch and download Korean &amp; Asian dramas, movies, and variety shows in HD.\n\n"
        "<b>Quick Navigation:</b>\n"
        "• <b>Search Drama</b> — Find titles by keyword or actor\n"
        "• <b>Latest Episodes</b> — Daily fresh uploads and ongoing dramas\n"
        "• <b>Category Browse</b> — 60+ genres: Romance, Comedy, Action, etc.\n"
        "• <b>HD Video Download</b> — Fast direct MP4 downloads\n\n"
        "Select an option below to begin browsing."
    )
    return rich_html, plain_html


def build_results_content(session_id: str, data: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    page = data["page"]
    results = data["results"]
    total = len(results)
    max_page = max(1, math.ceil(total / PER_PAGE))

    start = page * PER_PAGE
    chunk = results[start : start + PER_PAGE]
    header = data.get("title") or f"{LABEL} Results"
    query = data.get("query") or ""

    if not results:
        r_html = f"<h1>{html.escape(header)}</h1><p><i>No results found.</i></p>"
        p_html = f"<b>{html.escape(header)}</b>\n\n<i>No results found.</i>"
        return r_html, p_html, InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu")]])

    rows_table = []
    plain_blocks = []
    for i, item in enumerate(chunk):
        idx = start + i + 1
        t_esc = html.escape(item["title"])
        u_esc = html.escape(item["url"], quote=True)
        rows_table.append(f"<tr><td><b>{idx}</b></td><td><a href=\"{u_esc}\">{t_esc}</a></td></tr>")
        plain_blocks.append(f"<b>{idx}.</b> <a href=\"{u_esc}\">{t_esc}</a>")

    q_info = f"Query: <code>{html.escape(query)}</code> • " if query else ""
    rich_html = (
        f"<h1>{html.escape(header)}</h1>"
        f"<p>{q_info}Page {page + 1} of {max_page} • {total} results</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>#</th><th>Drama Title</th></tr>"
        f"{''.join(rows_table)}"
        "</table>"
        "<hr/>"
        "<aside>💡 Select a numbered button below to view details and download.</aside>"
    )

    separator = "─" * 24
    plain_html = (
        f"<b>{html.escape(header)}</b>\n"
        + (f"<code>{html.escape(query)}</code>\n\n" if query else "\n")
        + "\n\n".join(plain_blocks)
        + f"\n\n{separator}\n<i>Page {page + 1} of {max_page} · {total} results</i>"
    )

    keyboard = []
    row_nums = [
        InlineKeyboardButton(str(start + i + 1), callback_data=f"{PREFIX}:view:{session_id}:{start + i}")
        for i in range(len(chunk))
    ]
    if row_nums:
        keyboard.append(row_nums)

    row_nav = []
    if page > 0:
        row_nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:nav:{session_id}:{page - 1}"))
    row_nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu"))
    if page < max_page - 1:
        row_nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:nav:{session_id}:{page + 1}"))
    keyboard.append(row_nav)

    return rich_html, plain_html, InlineKeyboardMarkup(keyboard)


def build_detail_content(item: dict, session_id: str, idx: int) -> tuple[str, str, InlineKeyboardMarkup]:
    title = html.escape(item.get("title") or "Drama Details")
    poster = item.get("poster") or ""
    synopsis = item.get("synopsis") or "No synopsis available."
    eps = item.get("episodes") or 0
    genres = html.escape(item.get("genres") or "-")
    network = html.escape(item.get("network") or "-")
    release = html.escape(item.get("release") or "-")

    table_rows = [
        f"<tr><td><b>Episodes</b></td><td>{eps}</td></tr>" if eps else "",
        f"<tr><td><b>Genres</b></td><td>{genres}</td></tr>" if genres != "-" else "",
        f"<tr><td><b>Network</b></td><td>{network}</td></tr>" if network != "-" else "",
        f"<tr><td><b>Release</b></td><td>{release}</td></tr>" if release != "-" else "",
    ]
    meta_table = f"<table compact>{''.join(table_rows)}</table>" if any(table_rows) else ""

    rich_html = (
        f"<h1>📺 {title}</h1>"
        f"{meta_table}"
        "<hr/>"
        "<details open>"
        "<summary><b>📖 Synopsis</b></summary>"
        f"<p>{html.escape(synopsis)}</p>"
        "</details>"
        "<hr/>"
        "<aside>💡 Tap <b>Download</b> below to choose an episode and format.</aside>"
    )

    lines = []
    if poster:
        lines.append(f'<a href="{html.escape(poster, quote=True)}">&#8205;</a>')
    lines.append(f"<b>📺 {title}</b>\n")
    if eps:
        lines.append(f"<b>Episodes:</b> {eps}")
    if genres != "-":
        lines.append(f"<b>Genres:</b> {genres}")
    if network != "-":
        lines.append(f"<b>Network:</b> {network}")
    if release != "-":
        lines.append(f"<b>Release:</b> {release}")
    lines.append(f"\n<b>Synopsis:</b>\n{html.escape(synopsis)}")
    plain_html = "\n".join(lines)

    rows = []
    if eps and not item.get("restricted"):
        rows.append([InlineKeyboardButton("Download", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:0")])
    if item.get("url"):
        rows.append([InlineKeyboardButton("Open Website", url=item["url"])])

    rows.append([
        InlineKeyboardButton("Back", callback_data=f"{PREFIX}:back:{session_id}"),
        InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
    ])

    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_categories_content(page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    cats = CATEGORIES_CACHE.get("list") or []
    total = len(cats)
    max_page = max(1, math.ceil(total / CATS_PER_PAGE))
    page = max(0, min(page, max_page - 1))

    start = page * CATS_PER_PAGE
    chunk = cats[start : start + CATS_PER_PAGE]

    table_rows = []
    for i in range(0, len(chunk), 2):
        c1 = chunk[i]
        c2 = chunk[i + 1] if i + 1 < len(chunk) else None
        left = f"<b>{html.escape(c1['name'])}</b> ({c1['count']})" if c1.get("count") else f"<b>{html.escape(c1['name'])}</b>"
        right = f"<b>{html.escape(c2['name'])}</b> ({c2['count']})" if (c2 and c2.get("count")) else (f"<b>{html.escape(c2['name'])}</b>" if c2 else "")
        table_rows.append(f"<tr><td>{left}</td><td>{right}</td></tr>")

    rich_html = (
        "<h1>📂 Categories</h1>"
        f"<p>Page {page + 1} of {max_page} • {total} categories</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>Category</th><th>Category</th></tr>"
        f"{''.join(table_rows)}"
        "</table>"
        "<hr/>"
        "<aside>💡 Select a category button below to view dramas.</aside>"
    )

    plain_html = (
        f"<b>📂 Categories</b>\n"
        f"Page {page + 1} of {max_page} · {total} categories\n\n"
        "Select a category below:"
    )

    rows = []
    for i in range(0, len(chunk), 2):
        row = [InlineKeyboardButton(chunk[i]["name"], callback_data=f"{PREFIX}:cat:{chunk[i]['slug']}")]
        if i + 1 < len(chunk):
            row.append(
                InlineKeyboardButton(chunk[i + 1]["name"], callback_data=f"{PREFIX}:cat:{chunk[i + 1]['slug']}")
            )
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:cats:{page - 1}"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:cats:{page + 1}"))
    rows.append(nav)

    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_episodes_content(title: str, total_eps: int, session_id: str, idx: int, page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    eps_per_page = 15
    all_eps = list(range(1, max(1, total_eps) + 1))
    max_page = max(1, math.ceil(len(all_eps) / eps_per_page))
    page = max(0, min(page, max_page - 1))
    start = page * eps_per_page
    chunk = all_eps[start : start + eps_per_page]

    t_esc = html.escape(title)
    rich_html = (
        "<h1>📦 Select Episode</h1>"
        f"<p><b>Drama:</b> <code>{t_esc}</code></p>"
        f"<p>Total: <b>{total_eps} Episodes</b> • Page {page + 1} of {max_page}</p>"
        "<hr/>"
        "<aside>💡 Choose an episode below to proceed with download.</aside>"
    )

    plain_html = (
        f"<b>📦 Select Episode</b>\n"
        f"<code>{t_esc}</code>\n\n"
        f"Total: {total_eps} episodes · Page {page + 1} of {max_page}\n\n"
        "Choose an episode to download:"
    )

    rows = []
    for i in range(0, len(chunk), 5):
        rows.append([
            InlineKeyboardButton(f"Ep {ep}", callback_data=f"{PREFIX}:pick:{session_id}:{idx}:{ep}")
            for ep in chunk[i : i + 5]
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:{page - 1}"))
    nav.append(InlineKeyboardButton("Back", callback_data=f"{PREFIX}:view:{session_id}:{idx}"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:{page + 1}"))
    rows.append(nav)

    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_download_choice_content(title: str, session_id: str, idx: int, episode: int) -> tuple[str, str, InlineKeyboardMarkup]:
    t_esc = html.escape(title)
    rich_html = (
        f"<h1>🎬 {t_esc}</h1>"
        f"<p><b>Selected:</b> Episode {episode}</p>"
        "<hr/>"
        "<h3>Format Selection</h3>"
        "<ul>"
        "<li><b>Video</b> — HD MP4 video file</li>"
        "<li><b>MP3</b> — Extracted audio track</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Tap a format below to begin download.</aside>"
    )
    plain_html = (
        f"<b>🎬 {t_esc}</b>\n"
        f"<b>Episode:</b> {episode}\n\n"
        "Choose a download format:"
    )
    rows = [
        [
            InlineKeyboardButton("Video", callback_data=f"{PREFIX}:dl:{session_id}:{idx}:{episode}:video"),
            InlineKeyboardButton("MP3", callback_data=f"{PREFIX}:dl:{session_id}:{idx}:{episode}:mp3"),
        ],
        [
            InlineKeyboardButton("Change Episode", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:0"),
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
        ],
    ]
    return rich_html, plain_html, InlineKeyboardMarkup(rows)
