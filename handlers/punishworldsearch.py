"""Command /punish — search punishworld.com.

FLOW SEARCH
-----------
1. `/punish <query>` -> gate premium+NSFW -> status "Searching...".
2. `sync_search(query)` fetch halaman `https://punishworld.com/?s=<q>`, lalu
   halaman `/page/2/?s=<q>` kalau hasilnya masih kurang dari `MAX_RESULTS`
   (satu halaman berisi 48 video, jadi 48 + 2 = 50 pas).
3. Parse `div.row.no-gutters div.video-block`:
   - judul  : `a.thumb[aria-label]`
   - link   : `a.thumb[href]`
   - thumb  : `img[src]`
   - meta   : `.duration`, `.views-number`, `.rating-nolike`
4. Cache `search_id` -> hasil (TTL 1 jam, kepemilikan user dijaga di callback).
5. Render 5 hasil/halaman + tombol nomor, `Prev`/`Close`/`Next`.
6. Select -> tutup cache -> edit pesan jadi status -> `_start_dl_task`
   (`fmt_key="video"`, `status_ready=True`) -> worker `punishworld_download`.
"""
import asyncio
import html
import logging
import math
import re
import time
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from urllib.parse import quote_plus
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

LABEL = "PunishWorld"
PREFIX = "pw"
SEARCH_BASE = "https://punishworld.com/"
GATE_URL = "https://punishworld.com/"
EXAMPLE = "/punish cuckold"

MAX_RESULTS = 50
PER_PAGE = 5
# Satu halaman search memuat 48 video -> butuh halaman ke-2 untuk menggenapi 50.
PER_SERVER_PAGE = 48
MAX_PAGES = math.ceil(MAX_RESULTS / PER_SERVER_PAGE) + 1

CACHE_TTL = 3600  # detik

_SEARCH_CACHE = {}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _page_url(query: str, page: int = 1) -> str:
    if page <= 1:
        return f"{SEARCH_BASE}?s={quote_plus(query)}"
    return f"{SEARCH_BASE}page/{page}/?s={quote_plus(query)}"


def _parse_page(text: str) -> list[dict]:
    soup = BeautifulSoup(text, "html.parser")
    results = []
    for block in soup.select("div.row.no-gutters div.video-block"):
        link = block.select_one("a.thumb[href]")
        if not link:
            continue
        url = link.get("href")
        if not url:
            continue
        title = (link.get("aria-label") or "").strip()
        if not title:
            span = block.select_one("a.infos span.title")
            title = span.get_text(strip=True) if span else "Unknown title"
        img = link.select_one("img")
        thumb = (img.get("src") or img.get("data-src") or "") if img else ""

        parts = []
        dur = block.select_one(".duration")
        if dur:
            parts.append(dur.get_text(strip=True))
        views = block.select_one(".views-number")
        if views:
            # ikon FontAwesome ikut terbawa, buang supaya rapi
            vtxt = re.sub(r"\s+", " ", views.get_text(" ", strip=True)).strip()
            vtxt = vtxt.lstrip("👁").replace("\u00a0", " ").strip()
            vtxt = re.sub(r"^[^\d]*", "", vtxt).strip()
            if vtxt:
                parts.append(f"{vtxt} views")
        rating = block.select_one(".rating-nolike")
        if rating:
            rtxt = re.sub(r"^[^\d]*", "", re.sub(r"\s+", " ", rating.get_text(" ", strip=True))).strip()
            if rtxt:
                parts.append(f"👍 {rtxt}" if rtxt.endswith("%") else rtxt)

        results.append({
            "title": title,
            "url": url,
            "thumbnail": thumb,
            "meta": "  ·  ".join(parts),
        })
    return results


def _has_results(text: str) -> bool:
    """Halaman search menaruh jumlah video di h1: `<query> Porn (N Videos)`.
    N == 0 -> site menampilkan video fallback (bukan hasil search) -> buang."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S | re.I)
    if not m:
        return True
    plain = re.sub(r"<[^>]+>", "", m.group(1))
    cnt = re.search(r"\((\d+)\s*Videos?\)", plain, re.I)
    if not cnt:
        return True
    return int(cnt.group(1)) > 0


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
                log.warning("PunishWorld search request failed | page=%s err=%r", page, e)
                break
            if resp.status_code != 200:
                log.warning("PunishWorld search HTTP %s | page=%s", resp.status_code, page)
                break
            if page == 1 and not _has_results(resp.text):
                break  # fallback site "Porn (0 Videos)" -> tidak ada hasil
            items = _parse_page(resp.text)
            if not items:
                break
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


async def punish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "Search start | site=punishworld q=%r chat_id=%s user_id=%s",
        query, chat.id, user_id,
    )
    status = await msg.reply_text(
        f"🔍 Searching <code>{html.escape(query)}</code>...", parse_mode="HTML"
    )

    started = time.monotonic()
    results = await _do_search(query)
    log.info(
        "Search done | site=punishworld q=%r results=%d elapsed=%.1fs",
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


punish_cmd.__name__ = "punish_cmd"


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


async def punish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_callback(update, context)


punish_callback.__name__ = "punish_callback"
