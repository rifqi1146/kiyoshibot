"""Command /simontok — search simontok.study.

FLOW SEARCH
-----------
1. `/simontok <query>` -> gate premium+NSFW -> status "Searching...".
2. `search_sync(query)` fetch halaman `https://simontok.study/?s=<q>`, lalu
   `/page/2/?s=<q>` kalau hasilnya masih kurang dari `MAX_RESULTS`
   (satu halaman berisi 24 video, jadi 24 + 6 = 30 pas).
3. Parse `<article class="smt-kartu">`:
   - judul  : `h3 > a` (fallback `img[alt]`)
   - link   : `h3 > a[href]` (fallback `a.smt-layar[href]`)
   - thumb  : `img[src]`
   - meta   : `span.smt-menit` (durasi), `span.smt-hit` (views),
              `span.smt-jenis` (kategori)
   Halaman kosong tetap memuat artikel filler "Video Terbaru", jadi
   `<section class="smt-kata">` ("Tidak ada hasil...") dicek lebih dulu.
4. Cache `search_id` -> hasil (TTL 1 jam, kepemilikan user dijaga di callback).
5. Render 5 hasil/halaman + tombol nomor, `Prev`/`Close`/`Next`.
6. Select -> tutup cache -> edit pesan jadi status -> `_start_dl_task`
   (`fmt_key="video"`, `status_ready=True`) -> worker `simontok_download`.
"""
import asyncio
import html
import logging
import math
import time
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from urllib.parse import quote_plus, urlsplit
from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

from handlers.join import require_join_or_block
from handlers.dl.router import (
    _start_dl_task,
    _premium_link_allowed,
    _premium_link_block_text,
    _metadata_status,
)

log = logging.getLogger(__name__)

LABEL = "Simontok"
PREFIX = "sm"
SEARCH_BASE = "https://simontok.study/"
GATE_URL = "https://simontok.study/"
EXAMPLE = "/simontok hijab"

MAX_RESULTS = 30
PER_PAGE = 5
# Satu halaman search memuat 24 video -> butuh halaman ke-2 untuk menggenapi 30.
PER_SERVER_PAGE = 24
MAX_PAGES = math.ceil(MAX_RESULTS / PER_SERVER_PAGE) + 1

CACHE_TTL = 3600  # detik
# Batas keras entri cache pencarian. TTL membersihkan yang basi, tapi kalau ada
# lonjakan query unik dalam satu jam cache-nya bisa membengkak tanpa batas di
# RAM. Lewat batas, buang yang paling lama (dict menjaga urutan insert).
MAX_SEARCH_ENTRIES = 120

# search_id -> { query, results, page, ts, user_id, chat_id }
_SEARCH_CACHE = {}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

EMPTY_NOTICE = "Tidak ada hasil"


def _page_url(query: str, page: int = 1) -> str:
    if page <= 1:
        return f"{SEARCH_BASE}?s={quote_plus(query)}"
    return f"{SEARCH_BASE}page/{page}/?s={quote_plus(query)}"


def _is_post_url(url: str) -> bool:
    """True kalau href adalah post (bukan kategori/pagination/halaman statis)."""
    if not url:
        return False
    parts = urlsplit(url)
    if (parts.netloc or "").lower().removeprefix("www.") != "simontok.study":
        return False
    slug = (parts.path or "").strip("/")
    return bool(slug) and "/" not in slug


def _parse_page(text: str) -> list[dict]:
    """Parse 1 halaman HTML -> daftar hasil. Halaman kosong -> [].

    Halaman kosong tetap memuat artikel filler "Video Terbaru", jadi notice
    `section.smt-kata` wajib dicek lebih dulu sebelum memungut artikel.
    """
    soup = BeautifulSoup(text, "html.parser")

    notice = soup.select_one("section.smt-kata")
    if notice and EMPTY_NOTICE in notice.get_text(" ", strip=True):
        return []

    results = []
    for art in soup.find_all("article", class_="smt-kartu"):
        h3 = art.find("h3")
        link = h3.find("a", href=True) if h3 else None
        if not link:
            link = art.find("a", class_="smt-layar", href=True)
        if not link:
            link = art.find("a", href=True)
        if not link:
            continue

        url = (link.get("href") or "").strip()
        if not _is_post_url(url):
            continue

        title = h3.get_text(" ", strip=True) if h3 else ""
        if not title:
            img = art.find("img")
            title = (img.get("alt") or "").strip() if img else ""
        title = " ".join((title or "Unknown title").split())

        img = art.find("img")
        thumb = (img.get("src") or img.get("data-src") or "") if img else ""

        parts = []
        dur = art.select_one("span.smt-menit")
        if dur:
            dur_text = dur.get_text(" ", strip=True)
            if dur_text:
                parts.append(dur_text)
        views = art.select_one("span.smt-hit")
        if views:
            views_text = re_clean_views(views.get_text(" ", strip=True))
            if views_text:
                parts.append(f"{views_text} views")
        genre = art.select_one("span.smt-jenis")
        if genre:
            genre_text = genre.get_text(" ", strip=True)
            if genre_text:
                parts.append(genre_text)

        results.append({
            "title": title,
            "url": url,
            "thumbnail": thumb,
            "meta": "  ·  ".join(parts),
        })
    return results


def re_clean_views(text: str) -> str:
    """Rapikan '820 rb' -> '820 rb' (buang whitespace berlebih)."""
    return " ".join((text or "").split())


def _has_next_page(text: str, page: int) -> bool:
    """True kalau `nav.smt-urut` punya link ke halaman berikutnya.

    Server memakai `/page/<N>/?s=<q>`; link `next` di nav adalah satu-satunya
    penanda halaman lanjutan. Tanpa ini, query hasil sedikit tetap memanggil
    `/page/2/` yang balas 404 (noise di log).
    """
    soup = BeautifulSoup(text, "html.parser")
    nav = soup.select_one("nav.smt-urut")
    if not nav:
        return False
    target = f"/page/{page + 1}/"
    for a in nav.find_all("a", href=True):
        if target in a["href"]:
            return True
    return False


def _sync_search(query: str) -> list[dict]:
    sess = curl_requests.Session(impersonate="chrome")
    results, seen = [], set()
    try:
        for page in range(1, MAX_PAGES + 1):
            if len(results) >= MAX_RESULTS:
                break
            url = _page_url(query, page)
            try:
                resp = sess.get(url, headers={"User-Agent": UA}, timeout=25)
            except Exception as e:
                log.warning("Simontok search request failed | page=%s err=%r", page, e)
                break
            if resp.status_code != 200:
                log.warning("Simontok search HTTP %s | page=%s", resp.status_code, page)
                break
            items = _parse_page(resp.text)
            if not items:
                break  # halaman kosong / sudah habis
            fresh = 0
            for it in items:
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                results.append(it)
                fresh += 1
                if len(results) >= MAX_RESULTS:
                    break
            if fresh == 0:  # halaman duplikat semua -> berhenti
                break
            if not _has_next_page(resp.text, page):
                break  # tidak ada navigasi lanjutan -> halaman terakhir
    finally:
        try:
            sess.close()
        except Exception:
            pass
    return results[:MAX_RESULTS]


async def _do_search(query: str) -> list[dict]:
    return await asyncio.to_thread(_sync_search, query)


def _purge_expired_cache():
    now = time.time()
    for key in [k for k, v in list(_SEARCH_CACHE.items()) if now - v.get("ts", 0) > CACHE_TTL]:
        _SEARCH_CACHE.pop(key, None)
    if len(_SEARCH_CACHE) > MAX_SEARCH_ENTRIES:
        for key in list(_SEARCH_CACHE)[: len(_SEARCH_CACHE) - MAX_SEARCH_ENTRIES]:
            _SEARCH_CACHE.pop(key, None)


def _render_page(search_id: str, data: dict):
    page = data["page"]
    results = data["results"]
    total = len(results)
    max_page = max(1, math.ceil(total / PER_PAGE))

    start = page * PER_PAGE
    chunk = results[start:start + PER_PAGE]

    indent = "\u00a0\u00a0\u00a0"
    separator = "─" * 24

    text = f"<b>{LABEL} Search</b>\n<code>{html.escape(data['query'])}</code>\n\n"
    if not results:
        text += "<i>No results found.</i>"
        return text, None

    blocks = []
    for i, item in enumerate(chunk):
        idx = start + i + 1
        block = (
            f"<b>{idx}.</b> "
            f"<a href=\"{html.escape(item['url'], quote=True)}\">{html.escape(item['title'])}</a>"
        )
        if item.get("meta"):
            block += f"\n{indent}└─ {html.escape(item['meta'])}"
        blocks.append(block)

    text += "\n\n".join(blocks)
    text += f"\n\n{separator}\n<i>Page {page + 1} of {max_page} · {total} results</i>"

    keyboard = []
    row_nums = [
        InlineKeyboardButton(str(start + i + 1), callback_data=f"{PREFIX}:dl:{search_id}:{start + i}")
        for i in range(len(chunk))
    ]
    if row_nums:
        keyboard.append(row_nums)

    row_nav = []
    if page > 0:
        row_nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:nav:{search_id}:{page - 1}"))
    row_nav.append(InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:{search_id}:0"))
    if page < max_page - 1:
        row_nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:nav:{search_id}:{page + 1}"))
    keyboard.append(row_nav)

    return text, InlineKeyboardMarkup(keyboard)


async def simontok_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return

    msg = update.message
    if not msg or not update.effective_user:
        return

    chat = update.effective_chat
    user_id = update.effective_user.id

    ok, reason = _premium_link_allowed(GATE_URL, user_id, chat.id, chat.type)
    if not ok:
        return await msg.reply_text(_premium_link_block_text("single", reason), parse_mode="HTML")

    query = " ".join(context.args).strip()
    if not query:
        return await msg.reply_text(
            f"Please provide a search keyword. Example:\n<code>{EXAMPLE}</code>",
            parse_mode="HTML",
        )

    _purge_expired_cache()

    log.info(
        "Search start | site=simontok q=%r chat_id=%s user_id=%s",
        query, chat.id, user_id,
    )
    status = await msg.reply_text(
        f"🔍 Searching <code>{html.escape(query)}</code>...", parse_mode="HTML"
    )

    started = time.monotonic()
    results = await _do_search(query)
    log.info(
        "Search done | site=simontok q=%r results=%d elapsed=%.1fs",
        query, len(results), time.monotonic() - started,
    )
    if not results:
        return await status.edit_text(
            f"❌ No results for <code>{html.escape(query)}</code>.", parse_mode="HTML"
        )

    search_id = uuid.uuid4().hex[:8]
    _SEARCH_CACHE[search_id] = {
        "query": query,
        "results": results,
        "page": 0,
        "ts": time.time(),
        "user_id": user_id,
        "chat_id": chat.id,
    }

    text, markup = _render_page(search_id, _SEARCH_CACHE[search_id])
    await status.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


simontok_cmd.__name__ = "simontok_cmd"


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    parts = q.data.split(":")
    if len(parts) != 4 or parts[0] != PREFIX:
        return

    _, action, search_id, arg = parts
    data = _SEARCH_CACHE.get(search_id)
    if not data:
        return await q.answer("Search expired.", show_alert=True)

    if q.from_user.id != data["user_id"]:
        return await q.answer("This search does not belong to you.", show_alert=True)

    if action == "close":
        _SEARCH_CACHE.pop(search_id, None)
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("Closed.")
        return await q.answer()

    if action == "nav":
        try:
            data["page"] = int(arg)
        except (TypeError, ValueError):
            return await q.answer("Invalid page.", show_alert=True)
        text, markup = _render_page(search_id, data)
        await q.edit_message_text(
            text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True
        )
        return await q.answer()

    if action == "dl":
        try:
            idx = int(arg)
        except (TypeError, ValueError):
            return await q.answer("Invalid selection.", show_alert=True)

        results = data["results"]
        if idx < 0 or idx >= len(results):
            return await q.answer("Invalid selection.", show_alert=True)

        url = results[idx]["url"]
        ok, reason = _premium_link_allowed(
            url, q.from_user.id, q.message.chat.id, q.message.chat.type
        )
        if not ok:
            return await q.answer("Access denied.", show_alert=True)

        await q.answer("Downloading...")
        _SEARCH_CACHE.pop(search_id, None)

        try:
            await q.edit_message_text(text=_metadata_status(url), parse_mode="HTML")
        except Exception as e:
            log.debug("Failed to edit search message | err=%r", e)

        reply_to_id = (
            q.message.reply_to_message.message_id if q.message.reply_to_message else None
        )
        dl_data = {
            "url": url,
            "user": q.from_user.id,
            "chat_id": q.message.chat.id,
            "chat_type": q.message.chat.type,
            "reply_to": reply_to_id,
            "message_thread_id": getattr(q.message, "message_thread_id", None),
            "ts": time.time(),
        }

        await _start_dl_task(
            context=context,
            message=q.message,
            data=dl_data,
            fmt_key="video",
            status_ready=True,
        )


async def simontok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_callback(update, context)


simontok_callback.__name__ = "simontok_callback"
