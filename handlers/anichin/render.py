"""Renderer Rich Message (Bot API 10.3) + plain HTML fallback untuk `/anichin`.

Setiap builder mengembalikan tuple `(rich_html, plain_html, markup)`.

Grammar callback `ac:`:
- `ac:menu:0:0`             -> menu utama
- `ac:prompt:0:0`           -> layar instruksi search
- `ac:close:0:0`            -> hapus pesan
- `ac:list:<key>:<page>`    -> listing statis (`list`/`ongoing`/`completed`/`upcoming`/`drop`)
- `ac:genres:<page>:0`      -> picker genre
- `ac:genre:<slug>:<page>`  -> listing genre
- `ac:az:<page>:0`          -> picker huruf A-Z
- `ac:azl:<letter>:<page>`  -> listing satu huruf
- `ac:schedule:<day>:0`     -> jadwal per hari (index hari)
- `ac:nav:<sid>:<page>`     -> pindah halaman listing
- `ac:back:<sid>:0`         -> balik ke listing
- `ac:view:<sid>:<idx>`     -> detail series / episode picker
- `ac:eps:<sid>:<idx>:<page>` -> halaman episode picker
- `ac:pick:<sid>:<idx>:<ep>`  -> pilih episode -> format picker
- `ac:dl:<sid>:<idx>:<ep>:<fmt>` -> unduh episode
- `ac:epdl:<sid>:<idx>:<fmt>`    -> unduh post episode langsung
"""
import html
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.rich_search import build_search_content

from .constants import (
    AZ_PER_PAGE,
    EPISODES_PER_PAGE,
    GENRES_PER_PAGE,
    LABEL,
    PER_PAGE,
    PREFIX,
)
from .state import AZ_CACHE, GENRES_CACHE, SCHEDULE_CACHE

# Label metadata Anichin memakai bahasa Indonesia (`Tipe`, `Negara`, `Durasi`,
# `Tanggal rilis`, `Subber`). Diterjemahkan ke Inggris supaya konsisten dengan
# kebijakan UI bot (semua UI English, minim emote).
META_LABEL_MAP = {
    "Status": "Status",
    "Type": "Type",
    "Tipe": "Type",
    "Studio": "Studio",
    "Network": "Network",
    "Season": "Season",
    "Negara": "Country",
    "Durasi": "Duration",
    "Episode": "Episodes",
    "Tanggal rilis": "Released",
    "Subber": "Fansub",
}
# Urutan tampilan metadata di kartu detail series.
META_ORDER = ("Status", "Type", "Tipe", "Studio", "Network", "Season",
              "Negara", "Durasi", "Episode", "Tanggal rilis", "Subber")


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Search", callback_data=f"{PREFIX}:prompt:0:0"),
            InlineKeyboardButton("Schedule", callback_data=f"{PREFIX}:schedule:0:0"),
        ],
        [
            InlineKeyboardButton("Donghua List", callback_data=f"{PREFIX}:list:list:0"),
            InlineKeyboardButton("Ongoing", callback_data=f"{PREFIX}:list:ongoing:0"),
        ],
        [
            InlineKeyboardButton("Completed", callback_data=f"{PREFIX}:list:completed:0"),
            InlineKeyboardButton("Upcoming", callback_data=f"{PREFIX}:list:upcoming:0"),
        ],
        [
            InlineKeyboardButton("Drop", callback_data=f"{PREFIX}:list:drop:0"),
            InlineKeyboardButton("Genres", callback_data=f"{PREFIX}:genres:0:0"),
        ],
        [
            InlineKeyboardButton("A–Z List", callback_data=f"{PREFIX}:az:0:0"),
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"),
        ],
    ])


def build_menu_content() -> tuple[str, str]:
    rich_html = (
        f"<h1>🐉 {html.escape(LABEL)} Explorer</h1>"
        "<p>Streaming and downloading Donghua (Chinese animation) with Indonesian subtitles.</p>"
        "<hr/>"
        "<h3>✨ Quick Navigation</h3>"
        "<ul>"
        "<li><b>Search</b> — Find titles by keyword</li>"
        "<li><b>Donghua List</b> — Full catalogue</li>"
        "<li><b>Ongoing / Completed / Upcoming / Drop</b> — Browse by status</li>"
        "<li><b>Genres</b> — 47 tags: Action, Cultivation, Isekai, etc.</li>"
        "<li><b>A–Z List</b> — Alphabetical index</li>"
        "<li><b>Schedule</b> — Weekly release schedule</li>"
        "</ul>"
        "<hr/>"
        "<aside>💡 Select an option below to begin browsing.</aside>"
        f"<footer>{html.escape(LABEL)} Telegram Service</footer>"
    )
    plain_html = (
        f"<b>🐉 {html.escape(LABEL)} Explorer</b>\n"
        "Streaming and downloading Donghua (Chinese animation) with Indonesian subtitles.\n\n"
        "<b>Quick Navigation:</b>\n"
        "• <b>Search</b> — Find titles by keyword\n"
        "• <b>Donghua List</b> — Full catalogue\n"
        "• <b>Ongoing / Completed / Upcoming / Drop</b> — Browse by status\n"
        "• <b>Genres</b> — 47 tags: Action, Cultivation, Isekai, etc.\n"
        "• <b>A–Z List</b> — Alphabetical index\n"
        "• <b>Schedule</b> — Weekly release schedule\n\n"
        "Select an option below to begin browsing."
    )
    return rich_html, plain_html


def build_search_prompt_content() -> tuple[str, str, InlineKeyboardMarkup]:
    rich_html = (
        f"<h1>🔍 Search {html.escape(LABEL)}</h1>"
        "<p>To search, type your command with a keyword:</p>"
        "<pre><code>/anichin &lt;keyword&gt;</code></pre>"
        "<hr/>"
        "<aside>💡 Example: <code>/anichin renegade</code></aside>"
    )
    plain_html = (
        f"<b>🔍 Search {html.escape(LABEL)}</b>\n\n"
        "To search, send your command with a keyword:\n"
        "<code>/anichin &lt;keyword&gt;</code>\n\n"
        "Example:\n<code>/anichin renegade</code>"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Back to Menu", callback_data=f"{PREFIX}:menu:0:0")]])
    return rich_html, plain_html, markup


def build_results_content(session_id: str, data: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    """Listing (search/statis/genre/A-Z), 5 kartu per halaman."""
    for it in data.get("results") or []:
        if not it.get("meta"):
            parts = []
            if it.get("ep"):
                parts.append(it["ep"])
            if it.get("type"):
                parts.append(it["type"])
            if it.get("sub"):
                parts.append(it["sub"])
            it["meta"] = "  ·  ".join(parts)

    origin = data.get("origin") or {}
    back_callback = None
    back_label = "Back"
    if origin.get("action") == "genres":
        back_callback = f"{PREFIX}:genres:{int(origin.get('page') or 0)}:0"
        back_label = "Back to Genres"
    elif origin.get("action") == "az":
        back_callback = f"{PREFIX}:az:{int(origin.get('page') or 0)}:0"
        back_label = "Back to A–Z"

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
        back_callback=back_callback,
        back_label=back_label,
    )


def _pair_table(chunk: list, prefix: str, action: str, picker_page: int = 0) -> tuple[list, str]:
    rows = []
    for i in range(0, len(chunk), 2):
        rows.append(
            [InlineKeyboardButton(chunk[i]["name"][:28], callback_data=f"{prefix}:{action}:{chunk[i]['slug']}:{picker_page}")]
            + ([InlineKeyboardButton(chunk[i + 1]["name"][:28], callback_data=f"{prefix}:{action}:{chunk[i + 1]['slug']}:{picker_page}")]
               if i + 1 < len(chunk) else [])
        )
    table = "".join(
        f"<tr><td><b>{html.escape(chunk[i]['name'])}</b></td>"
        f"<td>{('<b>' + html.escape(chunk[i + 1]['name']) + '</b>') if i + 1 < len(chunk) else ''}</td></tr>"
        for i in range(0, len(chunk), 2)
    )
    return rows, table


def build_genres_content(page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    genres = GENRES_CACHE.get("list") or []
    total = len(genres)
    max_page = max(1, math.ceil(total / GENRES_PER_PAGE)) if total else 1
    page = max(0, min(page, max_page - 1))
    chunk = genres[page * GENRES_PER_PAGE: page * GENRES_PER_PAGE + GENRES_PER_PAGE]

    rows, table = _pair_table(chunk, PREFIX, "genre", page)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:genres:{page - 1}:0"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu:0:0"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:genres:{page + 1}:0"))
    rows.append(nav)

    rich_html = (
        "<h1>🏷 Genres</h1>"
        f"<p>Page {page + 1} of {max_page} • {total} genres</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>Genre</th><th>Genre</th></tr>"
        f"{table}"
        "</table>"
        "<hr/>"
        "<aside>💡 Select a genre button below to view matching titles.</aside>"
    )
    plain_html = f"<b>🏷 Genres</b>\nPage {page + 1} of {max_page} · {total} genres\n\nSelect a genre below:"
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_az_content(page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    letters = AZ_CACHE.get("letters") or []
    total = len(letters)
    max_page = max(1, math.ceil(total / AZ_PER_PAGE)) if total else 1
    page = max(0, min(page, max_page - 1))
    chunk = letters[page * AZ_PER_PAGE: page * AZ_PER_PAGE + AZ_PER_PAGE]

    rows = []
    for i in range(0, len(chunk), 7):
        rows.append([
            InlineKeyboardButton(l, callback_data=f"{PREFIX}:azl:{l}:0")
            for l in chunk[i:i + 7]
        ])

    # Navigasi halaman: 27 huruf > 10/halaman -> WAJIB ada Prev/Next,
    # kalau tidak halaman 2-3 (J..Z) tidak bisa diakses sama sekali.
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:az:{page - 1}:0"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu:0:0"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:az:{page + 1}:0"))
    rows.append(nav)

    rich_html = (
        "<h1>🔤 A–Z List</h1>"
        f"<p>Page {page + 1} of {max_page} • {total} letters</p>"
        "<hr/>"
        f"<p>{html.escape(' • '.join(chunk))}</p>"
        "<hr/>"
        "<aside>💡 Select a letter below to list titles starting with it.</aside>"
    )
    plain_html = f"<b>🔤 A–Z List</b>\nPage {page + 1} of {max_page} · {total} letters\n\nSelect a letter below:"
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_schedule_content(day_index: int = 0) -> tuple[str, str, InlineKeyboardMarkup]:
    days = SCHEDULE_CACHE.get("days") or []
    if not days:
        rich_html = "<h1>🗓 Schedule</h1><p><i>No schedule data available.</i></p>"
        plain_html = "<b>🗓 Schedule</b>\n\n<i>No schedule data available.</i>"
        return rich_html, plain_html, InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu:0:0")]])

    day_index = max(0, min(day_index, len(days) - 1))
    day = days[day_index]
    items = day.get("items") or []

    body = "".join(
        f"<tr><td><b>{html.escape(it['title'])}</b></td>"
        f"<td>{html.escape(it.get('time') or '-')}</td>"
        f"<td>{html.escape(it.get('ep') or '-')}</td></tr>"
        for it in items
    )
    rich_html = (
        f"<h1>🗓 {html.escape(day['day'])}</h1>"
        f"<p>{len(items)} releases • Day {day_index + 1} of {len(days)}</p>"
        "<hr/>"
        + (f"<table compact><tr><th>Title</th><th>Time</th><th>Status</th></tr>{body}</table>"
           if items else "<p><i>No releases on this day.</i></p>")
        + "<hr/>"
        "<aside>💡 Schedule is informational — open a title to download it.</aside>"
    )
    plain_parts = [f"<b>🗓 {html.escape(day['day'])}</b>", f"{len(items)} releases", ""]
    for it in items:
        status = it.get("ep") or ""
        plain_parts.append(f"• <b>{html.escape(it['title'])}</b> — {html.escape(it.get('time') or '')} {html.escape(status)}")
    plain_html = "\n".join(plain_parts)

    nav = []
    if day_index > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:schedule:{day_index - 1}:0"))
    nav.append(InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu:0:0"))
    if day_index < len(days) - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:schedule:{day_index + 1}:0"))
    return rich_html, plain_html, InlineKeyboardMarkup([nav])


def build_detail_content(detail: dict, session_id: str, idx: int) -> tuple[str, str, InlineKeyboardMarkup]:
    """Detail series: poster, meta, genre, sinopsis, tombol Download."""
    title = html.escape(detail.get("title") or LABEL)
    synopsis = detail.get("synopsis") or "No synopsis available."
    meta = detail.get("meta") or {}
    episodes = detail.get("episodes") or []
    genres = detail.get("genres") or []

    meta_rows = []
    seen_meta = set()
    for key in META_ORDER:
        val = meta.get(key)
        en_key = META_LABEL_MAP.get(key, key)
        if val and en_key not in seen_meta:
            seen_meta.add(en_key)
            meta_rows.append(f"<tr><td><b>{html.escape(en_key)}</b></td><td>{html.escape(val)}</td></tr>")
    meta_table = f"<table compact>{''.join(meta_rows)}</table>" if meta_rows else ""
    genre_line = ("<p><b>Genres:</b> " + html.escape(", ".join(genres)) + "</p>") if genres else ""

    rich_html = (
        f"<h1>🐉 {title}</h1>"
        f"{meta_table}"
        f"{genre_line}"
        "<hr/>"
        "<details open>"
        "<summary><b>📖 Synopsis</b></summary>"
        f"<p>{html.escape(synopsis)}</p>"
        "</details>"
        "<hr/>"
        f"<aside>💡 {len(episodes)} episode(s) available. Tap <b>Download</b> to pick one.</aside>"
    )

    plain_parts = [f"<b>🐉 {title}</b>", ""]
    seen_plain = set()
    for key in META_ORDER:
        val = meta.get(key)
        en_key = META_LABEL_MAP.get(key, key)
        if val and en_key not in seen_plain:
            seen_plain.add(en_key)
            plain_parts.append(f"• <b>{en_key}:</b> {html.escape(val)}")
    if genres:
        plain_parts.append(f"• <b>Genres:</b> {html.escape(', '.join(genres))}")
    plain_parts.append("")
    plain_parts.append(f"<b>Synopsis:</b>\n{html.escape(synopsis)}")
    plain_html = "\n".join(plain_parts)

    rows = []
    if episodes:
        rows.append([InlineKeyboardButton("Download", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:0")])
    if detail.get("url"):
        rows.append([InlineKeyboardButton("Open Website", url=detail["url"])])
    rows.append([
        InlineKeyboardButton("Back", callback_data=f"{PREFIX}:back:{session_id}:0"),
        InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"),
    ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_episodes_content(detail: dict, session_id: str, idx: int, page: int) -> tuple[str, str, InlineKeyboardMarkup]:
    """Episode picker grid untuk series."""
    episodes = detail.get("episodes") or []
    total = len(episodes)
    max_page = max(1, math.ceil(total / EPISODES_PER_PAGE))
    page = max(0, min(page, max_page - 1))
    chunk = episodes[page * EPISODES_PER_PAGE: page * EPISODES_PER_PAGE + EPISODES_PER_PAGE]
    title = html.escape(detail.get("title") or LABEL)

    rich_html = (
        "<h1>📦 Select Episode</h1>"
        f"<p><b>Series:</b> <code>{title}</code></p>"
        f"<p>Total: <b>{total} Episodes</b> • Page {page + 1} of {max_page}</p>"
        "<hr/>"
        "<aside>💡 Choose an episode below to proceed with download.</aside>"
    )
    plain_html = (
        f"<b>📦 Select Episode</b>\n<code>{title}</code>\n\n"
        f"Total: {total} episodes · Page {page + 1} of {max_page}\n\nChoose an episode to download:"
    )

    rows = []
    for i in range(0, len(chunk), 5):
        row = []
        for item in chunk[i:i + 5]:
            ep = item.get("ep") or 0
            if ep <= 0:
                continue
            row.append(InlineKeyboardButton(f"Ep {ep}", callback_data=f"{PREFIX}:pick:{session_id}:{idx}:{ep}"))
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


def build_format_choice_content(title: str, *, session_id: str, idx: int, episode: int = 0, direct: bool = False) -> tuple[str, str, InlineKeyboardMarkup]:
    """Pilih format Video/MP3."""
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
        f"<h1>🐉 {t_esc}</h1>"
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
    plain_html = f"<b>🐉 {t_esc}</b>\n{plain_sub}\n\nChoose a download format:"

    rows = [[
        InlineKeyboardButton("Video", callback_data=dl_cb.format(fmt="video")),
        InlineKeyboardButton("MP3", callback_data=dl_cb.format(fmt="mp3")),
    ]]
    if direct:
        rows.append([
            InlineKeyboardButton("Back", callback_data=f"{PREFIX}:back:{session_id}:0"),
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("Change Episode", callback_data=f"{PREFIX}:eps:{session_id}:{idx}:0"),
            InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"),
        ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


def build_episode_content(detail: dict, session_id: str, idx: int) -> tuple[str, str, InlineKeyboardMarkup]:
    """Detail halaman episode: resolusi mirror + daftar server streaming."""
    title = html.escape(detail.get("title") or LABEL)
    mirrors = detail.get("mirrors") or []
    servers = detail.get("servers") or []

    mirror_rows = "".join(
        f"<tr><td><b>{html.escape(m.get('label') or '-')}</b></td>"
        f"<td>{html.escape(m.get('name') or 'Mirror')}</td></tr>"
        for m in mirrors
    )
    server_names = ", ".join(html.escape(s.get("label") or "") for s in servers[:8])
    rich_html = (
        f"<h1>🎬 {title}</h1>"
        "<hr/>"
        "<h3>💾 Download</h3>"
        + (f"<table compact><tr><th>Quality</th><th>Source</th></tr>{mirror_rows}</table>"
           if mirrors else "<p><i>No download mirrors.</i></p>")
        + "<hr/>"
        + (f"<h3>📺 Stream Servers</h3><p>{server_names}</p>" if servers else "")
        + "<aside>💡 Tap <b>Download</b> below to pick a quality.</aside>"
    )
    plain_parts = [f"<b>🎬 {title}</b>", ""]
    if mirrors:
        plain_parts.append("<b>Download mirrors:</b>")
        for m in mirrors:
            plain_parts.append(f"• <b>{html.escape(m.get('label') or '-')}</b> — {html.escape(m.get('name') or 'Mirror')}")
    plain_html = "\n".join(plain_parts)

    rows = []
    if mirrors:
        rows.append([InlineKeyboardButton("Download", callback_data=f"{PREFIX}:epdl:{session_id}:{idx}:video")])
    rows.append([
        InlineKeyboardButton("Back", callback_data=f"{PREFIX}:back:{session_id}:0"),
        InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"),
    ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)
