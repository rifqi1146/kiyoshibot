import asyncio
import html
import logging
import math
import re
import time
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from urllib.parse import quote, urlsplit
from curl_cffi import requests as curl_requests, CurlOpt
from bs4 import BeautifulSoup

from handlers.join import require_join_or_block
from handlers.dl.router import (
    _start_dl_task,
    _premium_link_allowed,
    _premium_link_block_text,
    _metadata_status,
)
from handlers.dl.nekopoi.constants import FORCE_IPV4

log = logging.getLogger(__name__)

# Endpoint pencarian Nekopoi, mis. https://nekopoi.care/search/elaina
# Struktur hasil: div.nk-search-results ul li > a.nk-search-item
#   h2 judul, div.nk-search-thumb[style=url('...')], p.nk-search-desc.
LABEL = "Nekopoi"
PREFIX = "nq"
SEARCH_BASE = "https://nekopoi.care/search"
GATE_URL = "https://nekopoi.care/"
EXAMPLE = "/nekopoi elaina"

MAX_RESULTS = 30
PER_PAGE = 5

# Halaman seri (`/hentai/<slug>/`, `/jav/<slug>/`) itu LISTING — berisi daftar
# episode, bukan video sendiri. Embed-nya cuma widget discord, jadi unduhan selalu
# gagal. Dibuang dari hasil pencarian; episode aslinya ada di URL terpisah
# (`/…-episode-N-subtitle-indonesia/`), tetap masuk hasil.
_LISTING_PATH = re.compile(r"^/(?:hentai|jav)/[^/]+/?$")
CACHE_TTL = 3600  # detik

# search_id -> { query, results, page, ts, user_id, chat_id }
_SEARCH_CACHE = {}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _make_session():
    if FORCE_IPV4:
        return curl_requests.Session(
            impersonate="chrome",
            curl_options={CurlOpt.IPRESOLVE: 1},  # CURL_IPRESOLVE_V4
        )
    return curl_requests.Session(impersonate="chrome")


def _build_url(query: str, page: int = 1, base: str | None = None) -> str:
    slug = quote((query or "").strip(), safe="")
    root = (base or f"{SEARCH_BASE}/{slug}").rstrip("/")
    if page <= 1:
        return root
    return f"{root}/page/{page}/"


def _parse_page(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    results = []
    for li in soup.select("div.nk-search-results ul li"):
        a = li.find("a", class_="nk-search-item")
        if not a or not a.get("href"):
            continue
        h2 = li.find("h2")
        desc = li.find("p", class_="nk-search-desc")
        thumb = li.find("div", class_="nk-search-thumb")
        thumb_url = ""
        if thumb and thumb.get("style"):
            m = re.search(r"url\('([^']+)'\)", thumb["style"])
            if m:
                thumb_url = m.group(1)
        results.append({
            "title": h2.get_text(strip=True) if h2 else "Unknown title",
            "url": a.get("href"),
            "thumbnail": thumb_url,
            "desc": desc.get_text(" ", strip=True) if desc else "",
        })

    pages = set()
    page_base = None
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        m = re.search(r"(https?://[^/]+/search/[^/?#]+)/page/(\d+)/?", href)
        if m:
            page_base = (page_base or m.group(1)).rstrip("/")
            try:
                pages.add(int(m.group(2)))
            except (TypeError, ValueError):
                continue
    return results, page_base, sorted(pages)


def _result_meta(item: dict) -> str:
    desc = item.get("desc") or ""
    parts = []
    m = re.search(r"Duration\s*:\s*([^.]+?)(?:\s+Size\s*:|\s*$)", desc, re.I)
    if m and m.group(1).strip():
        parts.append(html.escape(m.group(1).strip()))
    m = re.search(r"Size\s*:\s*([^.]+?)\s*$", desc, re.I)
    if m and m.group(1).strip():
        parts.append("Size " + html.escape(m.group(1).strip()))
    if parts:
        return "  ·  ".join(parts)
    short = " ".join(desc.split())
    if len(short) > 90:
        short = short[:90].rstrip() + "..."
    return html.escape(short) if short else ""


def _do_search_sync(query: str):
    sess = _make_session()
    try:
        results = []
        seen = set()
        base = _build_url(query, 1)
        queue = [1]
        fetched = 0
        # Server Nekopoi menampilkan 10 item/halaman -> 4 halaman cukup untuk 30.
        max_fetch = math.ceil(MAX_RESULTS / 10) + 1
        while queue and len(results) < MAX_RESULTS and fetched < max_fetch:
            page = queue.pop(0)
            fetched += 1
            try:
                r = sess.get(_build_url(query, page, base), headers={"User-Agent": UA}, timeout=20)
                if r.status_code != 200:
                    log.warning("Nekopoi search HTTP %s | q=%r page=%s", r.status_code, query, page)
                    continue
                items, found_base, found_pages = _parse_page(r.text)
                if found_base:
                    base = found_base
                for it in items:
                    if it["url"] in seen:
                        continue
                    if _LISTING_PATH.match(urlsplit(it["url"]).path):
                        continue
                    seen.add(it["url"])
                    results.append(it)
                    if len(results) >= MAX_RESULTS:
                        break
                if page == 1:
                    for p in found_pages:
                        if p > 1 and p not in queue:
                            queue.append(p)
            except Exception as e:
                log.warning("Nekopoi search failed | q=%r page=%s err=%r", query, page, e)
        return results[:MAX_RESULTS]
    finally:
        try:
            sess.close()
        except Exception:
            pass


async def _do_search(query: str):
    return await asyncio.to_thread(_do_search_sync, query)


def _render_page(search_id: str, data: dict):
    page = data["page"]
    results = data["results"]
    total = len(results)
    max_page = max(1, math.ceil(total / PER_PAGE))

    start = page * PER_PAGE
    chunk = results[start:start + PER_PAGE]

    # NBSP (U+00A0) dipakai supaya indentasi tetap terlihat di Telegram.
    indent = "\u00a0\u00a0\u00a0"
    separator = "─" * 24

    text = (
        f"<b>{LABEL} Search</b>\n"
        f"<code>{html.escape(data['query'])}</code>\n\n"
    )
    if not results:
        text += "<i>No results found.</i>"
        return text, None

    blocks = []
    for i, item in enumerate(chunk):
        idx = start + i + 1
        meta = _result_meta(item)
        block = (
            f"<b>{idx}.</b> "
            f"<a href=\"{html.escape(item['url'], quote=True)}\">{html.escape(item['title'])}</a>"
        )
        if meta:
            block += f"\n{indent}└─ {meta}"
        blocks.append(block)

    text += "\n\n".join(blocks)
    text += f"\n\n{separator}\n<i>Page {page + 1} of {max_page}</i>"

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


def _purge_expired_cache():
    now = time.time()
    for key in [k for k, v in _SEARCH_CACHE.items() if now - v.get("ts", 0) > CACHE_TTL]:
        _SEARCH_CACHE.pop(key, None)


async def nekopoi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    status = await msg.reply_text(
        f"🔍 Searching <code>{html.escape(query)}</code>...", parse_mode="HTML"
    )

    results = await _do_search(query)
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
    await status.edit_text(
        text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True
    )


nekopoi_cmd.__name__ = "nekopoi_cmd"


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


async def nekopoi_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_callback(update, context)


nekopoi_callback.__name__ = "nekopoi_callback"
