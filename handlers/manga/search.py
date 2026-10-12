"""Rich-message search layer untuk manga reader (MangaDex, Maid-Manga, nHentai).

Semua sumber memakai helper bersama `utils.rich_search.build_search_content`
supaya tampilan hasil search seragam dengan `/simontok`, `/punish`, `/nekopoi`:
heading besar, tabel `#`/`Title`/`Info`, footer `aside`, tombol nomor + Prev/Close/Next.

Saat user menekan tombol nomor, pesan hasil search **DIHAPUS** lalu detail manga
dikirim sebagai **foto cover + caption** dengan tombol chapter.

Grammar callback (4 segmen: `prefix:action:search_id:arg`):
- `mgdex:` / `mgmaid:` / `mgnh:` = prefix per sumber.
- `nav:<sid>:<page>`   -> pindah halaman hasil search.
- `open:<sid>:<idx>`   -> buka detail (hapus pesan search + kirim foto).
- `close:<sid>:0`      -> hapus pesan hasil search.
"""
import hashlib
import logging
import time
import urllib.parse
import uuid

from bs4 import BeautifulSoup

from utils.rich_search import build_search_content, edit_search_rich

from . import maid, mangadex, nhentai
from .constants import MAID_URL, MANGADEX_API, NH_API_URL, NH_HEADERS
from .http import fetch_html, fetch_json, send_detail_photo
from .state import _move_message_lock, _udata_get, _udata_set

log = logging.getLogger(__name__)

PREFIX_BY_SOURCE = {"dex": "mgdex", "maid": "mgmaid", "nh": "mgnh"}
LABEL_BY_SOURCE = {"dex": "MangaDex", "maid": "Maid-Manga", "nh": "nHentai"}
SOURCE_BY_PREFIX = {v: k for k, v in PREFIX_BY_SOURCE.items()}
REFERER_BY_SOURCE = {
    "dex": "https://mangadex.org/",
    "maid": MAID_URL,
    "nh": "https://nhentai.net/",
}

PER_PAGE = 5
MAX_RESULTS = 30
CONTENT_RATINGS = ["safe", "suggestive", "erotica", "pornographic"]


def new_search_id() -> str:
    return uuid.uuid4().hex[:8]


def store_search(context, sid: str, data: dict) -> None:
    data["ts"] = time.time()
    _udata_set(context, f"search_{sid}", data)


def get_search(context, sid: str):
    return _udata_get(context, f"search_{sid}")


# ---------------------------------------------------------------------------
# Collector per sumber
# ---------------------------------------------------------------------------
async def collect_results(source: str, query: str, context):
    if source == "dex":
        return await _collect_dex(query)
    if source == "maid":
        return await _collect_maid(query, context)
    if source == "nh":
        return await _collect_nh(query)
    return []


async def _collect_dex(query: str):
    data = await fetch_json(f"{MANGADEX_API}/manga", {
        "title": query,
        "limit": MAX_RESULTS,
        "contentRating[]": CONTENT_RATINGS,
    })
    if not data or not data.get("data"):
        return []
    out = []
    for manga in data["data"]:
        attrs = manga.get("attributes") or {}
        titles = attrs.get("title") or {}
        title = titles.get("en") or titles.get("ja-ro") or next(iter(titles.values()), "Unknown")
        parts = []
        if attrs.get("year"):
            parts.append(str(attrs["year"]))
        if attrs.get("status"):
            parts.append(str(attrs["status"]).capitalize())
        if attrs.get("originalLanguage"):
            parts.append(str(attrs["originalLanguage"]).upper())
        out.append({
            "title": title,
            "url": f"https://mangadex.org/title/{manga['id']}",
            "meta": "  ·  ".join(parts),
            "id": manga["id"],
        })
    return out


async def _collect_maid(query: str, context):
    html_doc = await fetch_html(f"{MAID_URL}/?s={urllib.parse.quote(query)}")
    if not html_doc:
        return []
    soup = BeautifulSoup(html_doc, "html.parser")
    out, seen = [], set()
    for link in soup.select('a[href*="/manga/"], a[href*="/komik/"]'):
        href = (link.get("href") or "").strip()
        title = " ".join((link.get("title") or link.get_text(" ", strip=True)).split())
        if not href or not title or href in seen:
            continue
        seen.add(href)
        path = href.replace(MAID_URL, "")
        sid = hashlib.md5(path.encode()).hexdigest()[:8]
        _udata_set(context, f"maid_map_{sid}", path)
        out.append({"title": title, "url": href, "meta": "", "path": path, "sid": sid})
        if len(out) >= MAX_RESULTS:
            break
    return out


async def _collect_nh(query: str):
    data = await fetch_json(f"{NH_API_URL}/search", {"query": query, "page": 1}, custom_headers=NH_HEADERS)
    if not data or not data.get("result"):
        return []
    out = []
    for item in data["result"]:
        title = item.get("english_title") or item.get("japanese_title") or "Unknown"
        parts = []
        if item.get("num_pages"):
            parts.append(f"{item['num_pages']} pages")
        if item.get("num_favorites") is not None:
            parts.append(f"❤ {item['num_favorites']}")
        out.append({
            "title": title,
            "url": f"https://nhentai.net/g/{item['id']}/",
            "meta": "  ·  ".join(parts),
            "id": str(item["id"]),
        })
        if len(out) >= MAX_RESULTS:
            break
    return out


# ---------------------------------------------------------------------------
# Render + callback
# ---------------------------------------------------------------------------
def render_search_page(source: str, sid: str, data: dict):
    label = LABEL_BY_SOURCE[source]
    prefix = PREFIX_BY_SOURCE[source]
    return build_search_content(
        label,
        prefix,
        sid,
        data,
        per_page=PER_PAGE,
        heading=f"🔍 {label} Search",
        number_action="open",
        close_action="close",
        intro="Select a numbered button below to view details and chapters.",
    )


async def _build_detail(source: str, item: dict, context):
    if source == "dex":
        return await mangadex.fetch_detail_view(item["id"], 0)
    if source == "maid":
        return await maid.fetch_detail_view(item.get("path", ""), item.get("sid", ""), 0, context)
    return await nhentai.fetch_detail_view(item["id"])


async def handle_search_callback(query, context, data_str: str):
    parts = data_str.split(":")
    if len(parts) != 4:
        return
    prefix, action, arg1, arg2 = parts
    source = SOURCE_BY_PREFIX.get(prefix)
    if not source:
        return

    if action == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return await query.answer()

    sid = arg1
    data = get_search(context, sid)
    if not data:
        return await query.answer("Search session expired. Please search again.", show_alert=True)

    if int(query.from_user.id) != int(data.get("user_id") or 0):
        return await query.answer("This search does not belong to you.", show_alert=True)

    if action == "nav":
        data["page"] = int(arg2) if arg2.isdigit() else 0
        rich, plain, markup = render_search_page(source, sid, data)
        await edit_search_rich(context.bot, query.message.chat_id, query.message.message_id, rich, plain, markup)
        return await query.answer()

    if action == "open":
        idx = int(arg2) if arg2.isdigit() else -1
        results = data.get("results") or []
        if idx < 0 or idx >= len(results):
            return await query.answer("Invalid selection.", show_alert=True)
        item = results[idx]
        await query.answer("Loading details...")
        caption, keyboard, cover_url = await _build_detail(source, item, context)
        if not caption:
            return await query.answer("Failed to load details.", show_alert=True)

        chat_id = query.message.chat_id
        message_id = query.message.message_id
        thread_id = getattr(query.message, "message_thread_id", None)
        # Pesan hasil search hilang, lalu detail dikirim sebagai foto baru.
        try:
            await query.message.delete()
        except Exception:
            pass
        sent = await send_detail_photo(
            context, chat_id, thread_id, caption, keyboard, cover_url,
            referer=REFERER_BY_SOURCE.get(source, ""), owner_id=query.from_user.id,
        )
        if sent:
            _move_message_lock(chat_id, message_id, sent.chat_id, sent.message_id, fallback_user_id=query.from_user.id)
        return
