"""Renderer Rich Message (Bot API 10.3) + plain HTML fallback untuk `/nekopoi`.

Setiap builder mengembalikan tuple `(rich_html, plain_html, markup)`.

Grammar callback `nk:`:
- `nk:menu`                        -> menu utama
- `nk:prompt:search`               -> instruksi search
- `nk:latest`                      -> Episode Terbaru (beranda)
- `nk:cat:<slug>`                  -> listing kategori
- `nk:cats:<page>`                 -> picker kategori
- `nk:genres:<page>`               -> picker genre
- `nk:genre:<slug>`                -> listing genre
- `nk:az:<kind>:<page>`            -> picker huruf A-Z
- `nk:azl:<kind>:<letter>:<page>`  -> daftar series satu huruf
- `nk:schedule`                    -> jadwal rilis
- `nk:nav:<sid>:<page>`            -> pindah halaman listing
- `nk:back:<sid>`                  -> balik ke listing
- `nk:view:<sid>:<idx>`            -> detail (series -> episode picker, post -> format)
- `nk:eps:<sid>:<idx>:<page>`      -> halaman episode picker
- `nk:pick:<sid>:<idx>:<ep>`       -> pilih format (series)
- `nk:epdl:<sid>:<idx>:<fmt>`      -> unduh post langsung (episode page)
- `nk:dl:<sid>:<idx>:<ep>:<fmt>`   -> unduh episode series
- `nk:close`
"""
import html
import math
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .constants import (
    CATEGORIES,
    CATS_PER_PAGE,
    EPISODES_PER_PAGE,
    GENRES_PER_PAGE,
    LABEL,
    PER_PAGE,
    PREFIX,
)
from .state import GENRES_CACHE
from utils.rich_search import build_search_content


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Search", callback_data=f"{PREFIX}:prompt:search"),
            InlineKeyboardButton("Latest", callback_data=f"{PREFIX}:latest"),
        ],
        [
            InlineKeyboardButton("Hentai", callback_data=f"{PREFIX}:cat:hentai"),
            InlineKeyboardButton("2D Animation", callback_data=f"{PREFIX}:cat:2d-animation"),
        ],
        [
            InlineKeyboardButton("3D Hentai", callback_data=f"{PREFIX}:cat:3d-hentai"),
            InlineKeyboardButton("JAV", callback_data=f"{PREFIX}:cat:jav"),
        ],
        [
            InlineKeyboardButton("JAV Cosplay", callback_data=f"{PREFIX}:cat:jav-cosplay"),
            InlineKeyboardButton("Genres", callback_data=f"{PREFIX}:genres:0"),
        ],
        [
            InlineKeyboardButton("Hentai List", callback_data=f"{PREFIX}:az:hentai:0"),
            InlineKeyboardButton("JAV List", callback_data=f"{PREFIX}:az:jav:0"),
        ],
        [
            InlineKeyboardButton("Schedule", callback_data=f"{PREFIX}:schedule"),
            InlineKeyboardButton("Categories", callback_data=f"{PREFIX}:cats:0"),
        ],
        [
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
        ],
    ])


def build_menu_content() -> tuple[str, str]:
    rich_html = (
        "<h1>🌸 Nekopoi Explorer</h1>"
        "<p>Browse and download Hentai, 2D/3D Animation, and JAV with Indonesian subtitles.</p>"
        "<hr/>"
        "<h3>✨ Quick Navigation</h3>"
        "<ul>"
        "<li><b>Search</b> — Find titles by keyword</li>"
        "<li><b>Latest</b> — Newest episode uploads</li>"
        "<li><b>Categories</b> — Hentai, 2D Animation, 3D Hentai, JAV, JAV Cosplay</li>"
        "<li><b>Genres</b> — 78 tags: Action, Romance, Big Oppai, etc.</li>"
        "<li><b>A–Z Lists</b> — 1058 hentai series &amp; 2088 JAV titles</li>"
        "<li><b>Schedule</b> — Upcoming monthly releases</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Select an option below to begin browsing.</aside>"
        "<footer>Nekopoi Telegram Service</footer>"
    )
    plain_html = (
        "<b>🌸 Nekopoi Explorer</b>\n"
        "Browse and download Hentai, 2D/3D Animation, and JAV with Indonesian subtitles.\n\n"
        "<b>Quick Navigation:</b>\n"
        "• <b>Search</b> — Find titles by keyword\n"
        "• <b>Latest</b> — Newest episode uploads\n"
        "• <b>Categories</b> — Hentai, 2D Animation, 3D Hentai, JAV, JAV Cosplay\n"
        "• <b>Genres</b> — 78 tags: Action, Romance, Big Oppai, etc.\n"
        "• <b>A–Z Lists</b> — 1058 hentai series & 2088 JAV titles\n"
        "• <b>Schedule</b> — Upcoming monthly releases\n\n"
        "Select an option below to begin browsing."
    )
    return rich_html, plain_html


def build_results_content(session_id: str, data: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    """Listing (search/kategori/genre/A-Z satu huruf/latest), 5 kartu per halaman.

    Memakai helper bersama `utils.rich_search.build_search_content` supaya
    tampilan identik dengan search command lain (`/lendirqu`, `/punish`,
    `/simontok`, `/becekku`): heading + tabel `#`/`Title`/`Info` + aside.
    Bedanya cuma aksi tombol angka (`view` -> detail) & tombol tengah (`menu`).
    """
    # Isi `meta` untuk kolom Info: genre dulu, lalu deskripsi pendek.
    for it in data.get("results") or []:
        if it.get("meta"):
            continue
        info = it.get("genres") or ""
        if not info:
            desc = it.get("desc") or ""
            m = re.search(r"(?:Duration|Durasi)\s*:\s*([^.]+?)(?:\s+(?:Size|Ukuran)\s*:|$)", desc, re.I)
            info = m.group(1).strip() if m else desc
        info = " ".join(info.split())
        if len(info) > 70:
            info = info[:70].rstrip() + "…"
        it["meta"] = info

    return build_search_content(
        LABEL,
        PREFIX,
        session_id,
        data,
        per_page=PER_PAGE,
        heading=data.get("title") or f"{LABEL} Results",
        number_action="view",
        close_action="menu",
        intro="Select a numbered button below to view details and download.",
    )


def build_categories_content(page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    total = len(CATEGORIES)
    max_page = max(1, math.ceil(total / CATS_PER_PAGE))
    page = max(0, min(page, max_page - 1))
    chunk = CATEGORIES[page * CATS_PER_PAGE: page * CATS_PER_PAGE + CATS_PER_PAGE]

    rows = []
    for i in range(0, len(chunk), 2):
        row = [InlineKeyboardButton(chunk[i]["name"], callback_data=f"{PREFIX}:cat:{chunk[i]['slug']}")]
        if i + 1 < len(chunk):
            row.append(InlineKeyboardButton(chunk[i + 1]["name"], callback_data=f"{PREFIX}:cat:{chunk[i + 1]['slug']}"))
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:cats:{page - 1}"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:cats:{page + 1}"))
    rows.append(nav)

    rich_html = (
        "<h1>📂 Categories</h1>"
        f"<p>Page {page + 1} of {max_page} • {total} categories</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>Category</th><th>Category</th></tr>"
        + "".join(
            f"<tr><td><b>{html.escape(chunk[i]['name'])}</b></td>"
            f"<td>{('<b>' + html.escape(chunk[i + 1]['name']) + '</b>') if i + 1 < len(chunk) else ''}</td></tr>"
            for i in range(0, len(chunk), 2)
        )
        + "</table>"
        "<hr/>"
        "<aside>💡 Select a category button below to view its listing.</aside>"
    )
    plain_html = (
        f"<b>📂 Categories</b>\nPage {page + 1} of {max_page} · {total} categories\n\n"
        "Select a category below:"
    )
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_genres_content(page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    genres = GENRES_CACHE.get("list") or []
    total = len(genres)
    max_page = max(1, math.ceil(total / GENRES_PER_PAGE)) if total else 1
    page = max(0, min(page, max_page - 1))
    chunk = genres[page * GENRES_PER_PAGE: page * GENRES_PER_PAGE + GENRES_PER_PAGE]

    rows = []
    for i in range(0, len(chunk), 2):
        row = [InlineKeyboardButton(chunk[i]["name"], callback_data=f"{PREFIX}:genre:{chunk[i]['slug']}")]
        if i + 1 < len(chunk):
            row.append(InlineKeyboardButton(chunk[i + 1]["name"], callback_data=f"{PREFIX}:genre:{chunk[i + 1]['slug']}"))
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:genres:{page - 1}"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:genres:{page + 1}"))
    rows.append(nav)

    rich_html = (
        "<h1>🏷 Genres</h1>"
        f"<p>Page {page + 1} of {max_page} • {total} genres</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>Genre</th><th>Genre</th></tr>"
        + "".join(
            f"<tr><td><b>{html.escape(chunk[i]['name'])}</b></td>"
            f"<td>{('<b>' + html.escape(chunk[i + 1]['name']) + '</b>') if i + 1 < len(chunk) else ''}</td></tr>"
            for i in range(0, len(chunk), 2)
        )
        + "</table>"
        "<hr/>"
        "<aside>💡 Select a genre button below to view matching titles.</aside>"
    )
    plain_html = (
        f"<b>🏷 Genres</b>\nPage {page + 1} of {max_page} · {total} genres\n\n"
        "Select a genre below:"
    )
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_az_letters_content(kind: str, az: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    """Picker huruf A-Z untuk hentai-list / jav-list."""
    letters = list((az.get("letters") or {}).keys())
    label = "Hentai List" if kind == "hentai" else "JAV List"
    total = len(az.get("flat") or [])

    rows = []
    for i in range(0, len(letters), 7):
        rows.append([
            InlineKeyboardButton(l, callback_data=f"{PREFIX}:azl:{kind}:{l}:0")
            for l in letters[i:i + 7]
        ])
    rows.append([InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu")])

    counts = " • ".join(f"{l}={len(az['letters'].get(l) or [])}" for l in letters[:12])
    rich_html = (
        f"<h1>🔤 {html.escape(label)}</h1>"
        f"<p>{total} titles • {len(letters)} letter groups</p>"
        "<hr/>"
        f"<p>{html.escape(counts)}</p>"
        "<hr/>"
        "<aside>💡 Select a letter below to list titles starting with it.</aside>"
    )
    plain_html = (
        f"<b>🔤 {html.escape(label)}</b>\n{total} titles · {len(letters)} letter groups\n\n"
        "Select a letter below:"
    )
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_schedule_content(months: list[dict]) -> tuple[str, str, InlineKeyboardMarkup]:
    if not months:
        rich_html = "<h1>🗓 Schedule</h1><p><i>No schedule data available.</i></p>"
        plain_html = "<b>🗓 Schedule</b>\n\n<i>No schedule data available.</i>"
        return rich_html, plain_html, InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu")]])

    rich_parts = ["<h1>🗓 New Hentai Schedule</h1>", "<hr/>"]
    plain_parts = ["<b>🗓 New Hentai Schedule</b>", ""]
    for month in months:
        rich_parts.append(f"<h3>{html.escape(month['month'])}</h3>")
        plain_parts.append(f"<b>{html.escape(month['month'])}</b>")
        body = "".join(
            f"<tr><td><b>{html.escape(it['title'])}</b></td>"
            f"<td>{html.escape(it.get('episode') or '-')}</td>"
            f"<td>{html.escape(it.get('release') or '-')}</td></tr>"
            for it in month["items"]
        )
        rich_parts.append(
            "<table compact>"
            "<tr><th>Title</th><th>Episode</th><th>Release</th></tr>"
            f"{body}</table>"
        )
        rich_parts.append("<hr/>")
        for it in month["items"]:
            plain_parts.append(
                f"• <b>{html.escape(it['title'])}</b> {html.escape(it.get('episode') or '')} "
                f"— {html.escape(it.get('release') or '')}"
            )
        plain_parts.append("")

    rich_parts.append("<aside>💡 Schedule is informational — no direct download links.</aside>")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu")]])
    return "".join(rich_parts), "\n".join(plain_parts), markup


def build_detail_content(detail: dict, session_id: str, idx: int) -> tuple[str, str, InlineKeyboardMarkup]:
    """Detail halaman SERIES: poster, meta, sinopsis, tombol Download."""
    title = html.escape(detail.get("title") or "Nekopoi")
    poster = detail.get("poster") or ""
    synopsis = detail.get("synopsis") or "No synopsis available."
    meta = detail.get("meta") or {}
    episodes = detail.get("episodes") or []

    meta_rows = []
    for key in ("Jenis", "Status", "Episode", "Tayang", "Produser", "Durasi", "Skor", "Genre"):
        val = meta.get(key)
        if val:
            meta_rows.append(f"<tr><td><b>{html.escape(key)}</b></td><td>{html.escape(val)}</td></tr>")
    meta_table = f"<table compact>{''.join(meta_rows)}</table>" if meta_rows else ""

    rich_html = (
        f"<h1>🌸 {title}</h1>"
        f"{meta_table}"
        "<hr/>"
        "<details open>"
        "<summary><b>📖 Synopsis</b></summary>"
        f"<p>{html.escape(synopsis)}</p>"
        "</details>"
        "<hr/>"
        f"<aside>💡 {len(episodes)} episode(s) available. Tap <b>Download</b> to pick one.</aside>"
    )

    lines = []
    if poster:
        lines.append(f'<a href="{html.escape(poster, quote=True)}">&#8205;</a>')
    lines.append(f"<b>🌸 {title}</b>\n")
    for key in ("Jenis", "Status", "Episode", "Tayang", "Produser", "Durasi", "Skor", "Genre"):
        if meta.get(key):
            lines.append(f"<b>{html.escape(key)}:</b> {html.escape(meta[key])}")
    lines.append(f"\n<b>Synopsis:</b>\n{html.escape(synopsis)}")
    plain_html = "\n".join(lines)

    rows = []
    if episodes:
        rows.append([InlineKeyboardButton("Download", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:0")])
    if detail.get("url"):
        rows.append([InlineKeyboardButton("Open Website", url=detail["url"])])
    rows.append([
        InlineKeyboardButton("Back", callback_data=f"{PREFIX}:back:{session_id}"),
        InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
    ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_episodes_content(detail: dict, session_id: str, idx: int, page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    """Episode picker grid untuk halaman SERIES."""
    episodes = detail.get("episodes") or []
    total = len(episodes)
    max_page = max(1, math.ceil(total / EPISODES_PER_PAGE))
    page = max(0, min(page, max_page - 1))
    chunk = episodes[page * EPISODES_PER_PAGE: page * EPISODES_PER_PAGE + EPISODES_PER_PAGE]
    title = html.escape(detail.get("title") or "Nekopoi")

    rich_html = (
        "<h1>📦 Select Episode</h1>"
        f"<p><b>Series:</b> <code>{title}</code></p>"
        f"<p>Total: <b>{total} Episodes</b> • Page {page + 1} of {max_page}</p>"
        "<hr/>"
        "<aside>💡 Choose an episode below to proceed with download.</aside>"
    )
    plain_html = (
        f"<b>📦 Select Episode</b>\n<code>{title}</code>\n\n"
        f"Total: {total} episodes · Page {page + 1} of {max_page}\n\n"
        "Choose an episode to download:"
    )

    rows = []
    for i in range(0, len(chunk), 5):
        row = []
        for item in chunk[i:i + 5]:
            ep = item.get("ep") or 0
            if ep <= 0:
                continue
            row.append(InlineKeyboardButton(
                f"Ep {ep}", callback_data=f"{PREFIX}:pick:{session_id}:{idx}:{ep}"
            ))
        if row:
            rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:{page - 1}"))
    nav.append(InlineKeyboardButton("Back", callback_data=f"{PREFIX}:view:{session_id}:{idx}"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:{page + 1}"))
    rows.append(nav)
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_format_choice_content(
    title: str,
    *,
    session_id: str,
    idx: int,
    episode: int = 0,
    direct: bool = False,
) -> tuple[str, str, InlineKeyboardMarkup]:
    """Pilih format Video/MP3.

    `direct=True` -> post/EPISODE page (tidak ada episode picker).
    """
    t_esc = html.escape(title)
    if direct:
        sub = "<p><b>Source:</b> Episode page (direct post)</p>"
        plain_sub = "<b>Source:</b> Episode page (direct post)"
        dl_cb = f"{PREFIX}:epdl:{session_id}:{idx}:{{fmt}}"
    else:
        sub = f"<p><b>Selected:</b> Episode {episode}</p>"
        plain_sub = f"<b>Episode:</b> {episode}"
        dl_cb = f"{PREFIX}:dl:{session_id}:{idx}:{episode}:{{fmt}}"

    rich_html = (
        f"<h1>🌸 {t_esc}</h1>"
        f"{sub}"
        "<hr/>"
        "<h3>Format Selection</h3>"
        "<ul>"
        "<li><b>Video</b> — MP4 video</li>"
        "<li><b>MP3</b> — Extracted audio track</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Tap a format below to begin download.</aside>"
    )
    plain_html = f"<b>🌸 {t_esc}</b>\n{plain_sub}\n\nChoose a download format:"

    rows = [
        [
            InlineKeyboardButton("Video", callback_data=dl_cb.format(fmt="video")),
            InlineKeyboardButton("MP3", callback_data=dl_cb.format(fmt="mp3")),
        ],
    ]
    if direct:
        # Post/EPISODE page: tidak ada episode picker -> tombol Back ke listing.
        rows.append([
            InlineKeyboardButton("Back", callback_data=f"{PREFIX}:back:{session_id}"),
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
        ])
    else:
        # Series: sama seperti `/drakorid` — Change Episode + Close (tanpa Back).
        rows.append([
            InlineKeyboardButton("Change Episode", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:0"),
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close"),
        ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)
