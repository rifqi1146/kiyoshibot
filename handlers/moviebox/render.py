import html
import math
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .constants import (
    LABEL,
    MENU_LAYOUT,
    PER_PAGE,
    POPULAR_TITLES,
    PREFIX,
    SECTIONS,
    SECTIONS_PER_PAGE,
)


def menu_markup() -> InlineKeyboardMarkup:
    rows = []
    for pair in MENU_LAYOUT:
        row = []
        for key in pair:
            if key == "search":
                row.append(InlineKeyboardButton("Search", callback_data=f"{PREFIX}:prompt:search"))
            elif key == "all":
                row.append(InlineKeyboardButton("All Sections", callback_data=f"{PREFIX}:sections:0"))
            elif key == "popular":
                row.append(InlineKeyboardButton("Popular Titles", callback_data=f"{PREFIX}:popular:0"))
            elif key == "close":
                row.append(InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"))
            elif key in SECTIONS:
                label = SECTIONS[key][0]
                row.append(InlineKeyboardButton(label, callback_data=f"{PREFIX}:sec:{key}:0"))
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_menu_content() -> tuple[str, str]:
    rich_html = (
        f"<h1>🎬 {html.escape(LABEL)} Explorer</h1>"
        "<p>Browse and download Hollywood and international movies and TV series in HD.</p>"
        "<hr/>"
        "<h3>✨ Quick Navigation</h3>"
        "<ul>"
        "<li><b>Search</b> — Find movies and series by keyword</li>"
        "<li><b>Trending</b> — Most popular titles right now</li>"
        "<li><b>Coming Soon</b> — Anticipated upcoming releases</li>"
        "<li><b>Hot Short TV</b> — Viral short dramas &amp; series</li>"
        "<li><b>Asian Drama</b> — K-Drama, C-Drama &amp; Indo content</li>"
        "<li><b>Anime</b> — English dubbed anime &amp; animations</li>"
        "<li><b>All Sections</b> — 30+ curated genres and countries</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Select an option below to begin browsing.</aside>"
        f"<footer>{html.escape(LABEL)} Telegram Service</footer>"
    )
    plain_html = (
        f"<b>🎬 {html.escape(LABEL)} Explorer</b>\n"
        "Browse and download Hollywood and international movies and TV series in HD.\n\n"
        "<b>Quick Navigation:</b>\n"
        "• <b>Search</b> — Find movies and series by keyword\n"
        "• <b>Trending</b> — Most popular titles right now\n"
        "• <b>Coming Soon</b> — Anticipated upcoming releases\n"
        "• <b>Hot Short TV</b> — Viral short dramas & series\n"
        "• <b>Asian Drama</b> — K-Drama, C-Drama & Indo content\n"
        "• <b>Anime</b> — English dubbed anime & animations\n"
        "• <b>All Sections</b> — 30+ curated genres and countries\n\n"
        "Select an option below to begin browsing."
    )
    return rich_html, plain_html


def build_all_sections_content(page: int = 0) -> tuple[str, str, InlineKeyboardMarkup]:
    keys = list(SECTIONS.keys())
    total = len(keys)
    max_page = max(1, math.ceil(total / SECTIONS_PER_PAGE))
    page = max(0, min(page, max_page - 1))

    start = page * SECTIONS_PER_PAGE
    chunk = keys[start : start + SECTIONS_PER_PAGE]

    table_rows = []
    for i in range(0, len(chunk), 2):
        k1 = chunk[i]
        k2 = chunk[i + 1] if i + 1 < len(chunk) else None
        l1 = html.escape(SECTIONS[k1][0])
        l2 = html.escape(SECTIONS[k2][0]) if k2 else ""
        table_rows.append(f"<tr><td><b>{l1}</b></td><td><b>{l2}</b></td></tr>")

    rich_html = (
        "<h1>📂 All MovieBox Sections</h1>"
        f"<p>Page {page + 1} of {max_page} • {total} sections</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>Section</th><th>Section</th></tr>"
        f"{''.join(table_rows)}"
        "</table>"
        "<hr/>"
        "<aside>💡 Tap a section button below to view titles.</aside>"
    )

    plain_html = (
        "<b>📂 All MovieBox Sections</b>\n"
        f"Page {page + 1} of {max_page} · {total} sections\n\n"
        "Select a section below:"
    )

    rows = []
    for i in range(0, len(chunk), 2):
        k1 = chunk[i]
        k2 = chunk[i + 1] if i + 1 < len(chunk) else None
        r = [InlineKeyboardButton(SECTIONS[k1][0], callback_data=f"{PREFIX}:sec:{k1}:0")]
        if k2:
            r.append(InlineKeyboardButton(SECTIONS[k2][0], callback_data=f"{PREFIX}:sec:{k2}:0"))
        rows.append(r)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:sections:{page - 1}"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:sections:{page + 1}"))
    rows.append(nav)

    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_results_content(session_id: str, data: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    page = data.get("page", 0)
    results = data.get("results") or []
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
        t_esc = html.escape(item.get("display_title") or item.get("title") or "Unknown")
        u_esc = html.escape(item.get("url") or "", quote=True)
        meta_esc = html.escape(item.get("meta") or item.get("type") or "-")
        rows_table.append(
            f"<tr><td><b>{idx}</b></td><td><a href=\"{u_esc}\">{t_esc}</a></td><td>{meta_esc}</td></tr>"
        )
        plain_blocks.append(f"<b>{idx}.</b> <a href=\"{u_esc}\">{t_esc}</a>\n   └─ <i>{meta_esc}</i>")

    q_info = f"Query: <code>{html.escape(query)}</code> • " if query else ""
    rich_html = (
        f"<h1>{html.escape(header)}</h1>"
        f"<p>{q_info}Page {page + 1} of {max_page} • {total} results</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>#</th><th>Title</th><th>Info</th></tr>"
        f"{''.join(rows_table)}"
        "</table>"
        "<hr/>"
        "<aside>💡 Select a numbered button below to view details and download.</aside>"
    )

    separator = "─" * 24
    plain_body = "\n\n".join(plain_blocks)
    if len(plain_body) > 3200:
        plain_body = plain_body[:3200].rsplit("\n\n", 1)[0] + "\n\n<i>... list truncated for display ...</i>"

    plain_html = (
        f"<b>{html.escape(header)}</b>\n"
        + (f"<code>{html.escape(query)}</code>\n\n" if query else "\n")
        + plain_body
        + f"\n\n{separator}\n<i>Page {page + 1} of {max_page} · {total} results</i>"
    )

    keyboard = []
    for i in range(0, len(chunk), 5):
        row_slice = chunk[i : i + 5]
        keyboard.append([
            InlineKeyboardButton(str(start + i + j + 1), callback_data=f"{PREFIX}:view:{session_id}:{start + i + j}")
            for j in range(len(row_slice))
        ])

    row_nav = []
    if page > 0:
        row_nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:nav:{session_id}:{page - 1}"))
    row_nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu"))
    has_more = data.get("has_more")
    if has_more is None:
        has_more = page < max_page - 1
    if has_more:
        row_nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:nav:{session_id}:{page + 1}"))
    keyboard.append(row_nav)

    return rich_html, plain_html, InlineKeyboardMarkup(keyboard)


def build_detail_content(item: dict, session_id: str, idx: int) -> tuple[str, str, InlineKeyboardMarkup]:
    title = html.escape(item.get("title") or "Title")
    poster = item.get("thumbnail") or ""
    synopsis = item.get("description") or "No synopsis available."
    is_tv = item.get("is_tv", False)
    stype = "TV Series" if is_tv else "Movie"

    year = html.escape(item.get("year") or "-")
    genres = html.escape(item.get("genres") or "-")
    country = html.escape(item.get("country") or "-")
    rating = html.escape(str(item.get("rating") or "-"))
    seasons = item.get("seasons") or []

    table_rows = [
        f"<tr><td><b>Type</b></td><td>{stype}</td></tr>",
        f"<tr><td><b>Rating</b></td><td>⭐ {rating}</td></tr>" if rating != "-" else "",
        f"<tr><td><b>Year</b></td><td>{year}</td></tr>" if year != "-" else "",
        f"<tr><td><b>Country</b></td><td>{country}</td></tr>" if country != "-" else "",
        f"<tr><td><b>Genres</b></td><td>{genres}</td></tr>" if genres != "-" else "",
    ]
    if is_tv and seasons:
        s_count = len(seasons)
        total_eps = sum(s.get("max_ep", 0) for s in seasons)
        table_rows.append(f"<tr><td><b>Seasons</b></td><td>{s_count} Season(s), {total_eps} Ep(s)</td></tr>")

    meta_table = f"<table compact>{''.join(table_rows)}</table>"

    rich_html = (
        f"<h1>🎬 {title}</h1>"
        f"{meta_table}"
        "<hr/>"
        "<details open>"
        "<summary><b>📖 Synopsis</b></summary>"
        f"<p>{html.escape(synopsis)}</p>"
        "</details>"
        "<hr/>"
        "<aside>💡 Tap <b>Download</b> below to choose format / episode.</aside>"
    )

    lines = []
    if poster:
        lines.append(f'<a href="{html.escape(poster, quote=True)}">&#8205;</a>')
    lines.append(f"<b>🎬 {title}</b>\n")
    lines.append(f"• <b>Type:</b> {stype}")
    if rating != "-":
        lines.append(f"• <b>Rating:</b> ⭐ {rating}")
    if year != "-":
        lines.append(f"• <b>Year:</b> {year}")
    if country != "-":
        lines.append(f"• <b>Country:</b> {country}")
    if genres != "-":
        lines.append(f"• <b>Genres:</b> {genres}")
    if is_tv and seasons:
        lines.append(f"• <b>Content:</b> {len(seasons)} Season(s), {sum(s.get('max_ep', 0) for s in seasons)} Episodes")
    lines.append(f"\n<b>Synopsis:</b>\n{html.escape(synopsis)}")
    plain_html = "\n".join(lines)

    rows = []
    if is_tv:
        rows.append([InlineKeyboardButton("Download Episodes", callback_data=f"{PREFIX}:seasons:{session_id}:{idx}:0:0")])
    else:
        rows.append([InlineKeyboardButton("Download Movie", callback_data=f"{PREFIX}:dlchoice:{session_id}:{idx}:0:0")])

    if item.get("url"):
        rows.append([InlineKeyboardButton("Open Website", url=item["url"])])

    rows.append([
        InlineKeyboardButton("Back", callback_data=f"{PREFIX}:back:{session_id}"),
        InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
    ])

    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_episodes_content(
    title: str,
    seasons: list[dict],
    session_id: str,
    idx: int,
    season_idx: int,
    page: int,
) -> tuple[str, str, InlineKeyboardMarkup]:
    if not seasons:
        seasons = [{"season": 1, "max_ep": 1}]
    season_idx = max(0, min(season_idx, len(seasons) - 1))
    current_season = seasons[season_idx]
    se_num = current_season.get("season", season_idx + 1)
    max_ep = max(1, current_season.get("max_ep", 1))

    eps_per_page = 15
    all_eps = list(range(1, max_ep + 1))
    max_page = max(1, math.ceil(len(all_eps) / eps_per_page))
    page = max(0, min(page, max_page - 1))
    start = page * eps_per_page
    chunk = all_eps[start : start + eps_per_page]

    t_esc = html.escape(title)
    rich_html = (
        "<h1>📦 Select Episode</h1>"
        f"<p><b>Title:</b> <code>{t_esc}</code></p>"
        f"<p><b>Season {se_num}:</b> {max_ep} Episodes • Page {page + 1} of {max_page}</p>"
        "<hr/>"
        "<aside>💡 Choose an episode below to proceed with download.</aside>"
    )

    plain_html = (
        f"<b>📦 Select Episode</b>\n"
        f"<code>{t_esc}</code>\n\n"
        f"<b>Season {se_num}:</b> {max_ep} episodes · Page {page + 1} of {max_page}\n\n"
        "Choose an episode to download:"
    )

    rows = []

    if len(seasons) > 1:
        s_tabs = []
        for i, s in enumerate(seasons):
            s_num = s.get("season", i + 1)
            label = f"• S{s_num} •" if i == season_idx else f"S{s_num}"
            s_tabs.append(InlineKeyboardButton(label, callback_data=f"{PREFIX}:seasons:{session_id}:{idx}:{i}:0"))
        for i in range(0, len(s_tabs), 4):
            rows.append(s_tabs[i : i + 4])

    for i in range(0, len(chunk), 5):
        rows.append([
            InlineKeyboardButton(f"Ep {ep}", callback_data=f"{PREFIX}:dlchoice:{session_id}:{idx}:{se_num}:{ep}")
            for ep in chunk[i : i + 5]
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:seasons:{session_id}:{idx}:{season_idx}:{page - 1}"))
    nav.append(InlineKeyboardButton("Back", callback_data=f"{PREFIX}:view:{session_id}:{idx}"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:seasons:{session_id}:{idx}:{season_idx}:{page + 1}"))
    rows.append(nav)

    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_download_choice_content(
    title: str,
    session_id: str,
    idx: int,
    season: int,
    episode: int,
    is_tv: bool,
) -> tuple[str, str, InlineKeyboardMarkup]:
    t_esc = html.escape(title)
    ep_label = f"Season {season} Episode {episode}" if is_tv else "Full Movie"

    rich_html = (
        f"<h1>🎬 {t_esc}</h1>"
        f"<p><b>Selected:</b> {ep_label}</p>"
        "<hr/>"
        "<h3>Format Selection</h3>"
        "<ul>"
        "<li><b>Video</b> — Multi-quality</li>"
        "<li><b>Audio</b> — Extracted audio track</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Tap a format below to choose resolution and download.</aside>"
    )

    plain_html = (
        f"<b>🎬 {t_esc}</b>\n"
        f"<b>Selected:</b> {ep_label}\n\n"
        "Choose a download format:\n"
        "• <b>Video</b> — Multi-quality\n"
        "• <b>Audio</b> — Extracted audio track"
    )

    back_cb = f"{PREFIX}:seasons:{session_id}:{idx}:0:0" if is_tv else f"{PREFIX}:view:{session_id}:{idx}"
    rows = [
        [
            InlineKeyboardButton("Video", callback_data=f"{PREFIX}:dl:{session_id}:{idx}:{season}:{episode}:video"),
            InlineKeyboardButton("Audio", callback_data=f"{PREFIX}:dl:{session_id}:{idx}:{season}:{episode}:mp3"),
        ],
        [
            InlineKeyboardButton("Back", callback_data=back_cb),
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
        ],
    ]
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_popular_content() -> tuple[str, str, InlineKeyboardMarkup]:
    rich_html = (
        "<h1>⭐ Popular MovieBox Titles</h1>"
        "<p>Trending searches and community favorites:</p>"
        "<hr/>"
        "<aside>💡 Tap a title below to search immediately.</aside>"
    )
    plain_html = (
        "<b>⭐ Popular MovieBox Titles</b>\n\n"
        "Trending searches and community favorites:\n"
        "Tap a title below to search:"
    )

    rows = []
    for i in range(0, len(POPULAR_TITLES), 2):
        t1 = POPULAR_TITLES[i]
        t2 = POPULAR_TITLES[i + 1] if i + 1 < len(POPULAR_TITLES) else None
        r = [InlineKeyboardButton(t1, callback_data=f"{PREFIX}:popsearch:{i}")]
        if t2:
            r.append(InlineKeyboardButton(t2, callback_data=f"{PREFIX}:popsearch:{i + 1}"))
        rows.append(r)
    rows.append([
        InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu"),
        InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
    ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)
