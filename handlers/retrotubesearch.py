import asyncio
import html
import logging
import math
import uuid
import time
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

# Tema WP-Script dipakai bersama oleh situs ini, jadi selector-nya sama persis:
#   <article class="loop-video ..."><a href="{post}" title="{judul}">
SITES = {
    "lendirqu": {
        "label": "LendirQu",
        "prefix": "lq",
        "search_base": "https://lendirqu.stream/",
        "gate_url": "https://lendirqu.stream/",
        "example": "/lendirqu bokep sma",
    },
    "becekku": {
        "label": "Becekku",
        "prefix": "bq",
        "search_base": "https://becekku.live/",
        "gate_url": "https://becekku.live/",
        "example": "/becekku bocil",
    },
}
_PREFIX_TO_SITE = {cfg["prefix"]: key for key, cfg in SITES.items()}

MAX_RESULTS = 15
PER_PAGE = 5
CACHE_TTL = 3600  # detik

# search_id -> { site, query, results, page, ts, user_id, chat_id }
_SEARCH_CACHE = {}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _article_stat(article, class_name: str) -> str | None:
    element = article.find("span", class_=class_name)
    if not element:
        return None
    # Ikon Font Awesome ikut berada di dalam span; ambil teks yang terlihat saja.
    value = element.get_text(" ", strip=True)
    return " ".join(value.split()) or None


def _article_thumbnail(article, link_tag) -> str | None:
    thumb = article.get("data-main-thumb")
    if thumb:
        return thumb
    image = article.find("img")
    if image:
        return image.get("src") or image.get("data-src") or image.get("data-lazy-src")
    return None


def _do_search_sync(site_key: str, query: str):
    site = SITES[site_key]
    url = f"{site['search_base']}?s={quote_plus(query)}"
    try:
        r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for art in soup.find_all("article"):
            a_tag = art.find("a")
            if not a_tag:
                continue
            title = a_tag.get("title") or a_tag.get_text(strip=True) or "Unknown title"
            link = a_tag.get("href")
            if link:
                results.append({
                    "title": title,
                    "url": link,
                    "thumbnail": _article_thumbnail(art, a_tag),
                    "duration": _article_stat(art, "duration"),
                    "views": _article_stat(art, "views"),
                })
        return results[:MAX_RESULTS]
    except Exception as e:
        log.warning("Search failed | site=%s q=%r err=%r", site_key, query, e)
        return []


def _result_meta(item: dict) -> str:
    parts = []
    if item.get("duration"):
        parts.append(html.escape(item["duration"]))
    if item.get("views"):
        parts.append(f"{html.escape(item['views'])} views")
    return "  ·  ".join(parts) if parts else ""


async def _do_search(site_key: str, query: str):
    return await asyncio.to_thread(_do_search_sync, site_key, query)


def _render_page(site_key: str, search_id: str, data: dict):
    site = SITES[site_key]
    prefix = site["prefix"]
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
        f"<b>{site['label']} Search</b>\n"
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
        InlineKeyboardButton(str(start + i + 1), callback_data=f"{prefix}:dl:{search_id}:{start + i}")
        for i in range(len(chunk))
    ]
    if row_nums:
        keyboard.append(row_nums)

    row_nav = []
    if page > 0:
        row_nav.append(InlineKeyboardButton("Prev", callback_data=f"{prefix}:nav:{search_id}:{page - 1}"))
    row_nav.append(InlineKeyboardButton("Close", callback_data=f"{prefix}:close:{search_id}:0"))
    if page < max_page - 1:
        row_nav.append(InlineKeyboardButton("Next", callback_data=f"{prefix}:nav:{search_id}:{page + 1}"))
    keyboard.append(row_nav)

    return text, InlineKeyboardMarkup(keyboard)


def _purge_expired_cache():
    now = time.time()
    for key in [k for k, v in _SEARCH_CACHE.items() if now - v["ts"] > CACHE_TTL]:
        _SEARCH_CACHE.pop(key, None)


def make_site_cmd(site_key: str):
    site = SITES[site_key]

    async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await require_join_or_block(update, context):
            return

        msg = update.message
        if not msg or not update.effective_user:
            return

        chat = update.effective_chat
        user_id = update.effective_user.id

        # Gate: domain ini hanya untuk user premium, dan hanya di private chat
        # atau grup yang mengaktifkan NSFW.
        ok, reason = _premium_link_allowed(site["gate_url"], user_id, chat.id, chat.type)
        if not ok:
            return await msg.reply_text(_premium_link_block_text("single", reason), parse_mode="HTML")

        query = " ".join(context.args).strip()
        if not query:
            return await msg.reply_text(
                f"Please provide a search keyword. Example:\n<code>{site['example']}</code>",
                parse_mode="HTML",
            )

        _purge_expired_cache()

        status = await msg.reply_text(
            f"🔍 Searching <code>{html.escape(query)}</code>...", parse_mode="HTML"
        )

        results = await _do_search(site_key, query)
        if not results:
            return await status.edit_text(
                f"❌ No results for <code>{html.escape(query)}</code>.", parse_mode="HTML"
            )

        search_id = uuid.uuid4().hex[:8]
        _SEARCH_CACHE[search_id] = {
            "site": site_key,
            "query": query,
            "results": results,
            "page": 0,
            "ts": time.time(),
            "user_id": user_id,
            "chat_id": chat.id,
        }

        text, markup = _render_page(site_key, search_id, _SEARCH_CACHE[search_id])
        await status.edit_text(
            text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True
        )

    site_cmd.__name__ = f"{site_key}_cmd"
    return site_cmd


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, site_key: str):
    site = SITES[site_key]
    q = update.callback_query
    if not q or not q.data:
        return

    parts = q.data.split(":")
    if len(parts) != 4 or parts[0] != site["prefix"]:
        return

    _, action, search_id, arg = parts
    data = _SEARCH_CACHE.get(search_id)
    if not data:
        return await q.answer("Search expired.", show_alert=True)

    # Cache harus milik site yang sama (tolak tombol silang antar site).
    if data.get("site") != site_key:
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
        text, markup = _render_page(site_key, search_id, data)
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

        # Menu pencarian hilang dari cache supaya tombol lama tak bisa dipakai lagi.
        _SEARCH_CACHE.pop(search_id, None)

        # Edit pesan hasil pencarian langsung menjadi status download
        # (tanpa mengirim pesan baru).
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


def make_site_callback(site_key: str):
    async def site_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await _handle_callback(update, context, site_key)

    site_callback.__name__ = f"{site_key}_callback"
    return site_callback


lendirqu_cmd = make_site_cmd("lendirqu")
becekku_cmd = make_site_cmd("becekku")

lendirqu_callback = make_site_callback("lendirqu")
becekku_callback = make_site_callback("becekku")
