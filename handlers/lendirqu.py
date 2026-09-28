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
from database.download_db import is_premium_user
from database.nsfw_db import is_nsfw_allowed
from handlers.dl.router import _start_dl_task, _premium_link_allowed, _premium_link_block_text, _metadata_status

log = logging.getLogger(__name__)

# Simpan state pencarian: search_id -> { query, results: [{title, url, ...}], page: 0, ts }
_SEARCH_CACHE = {}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def _do_search_sync(query: str):
    url = f"https://lendirqu.stream/?s={quote_plus(query)}"
    try:
        r = curl_requests.get(url, headers={"User-Agent": UA}, impersonate="chrome", timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        arts = soup.find_all("article")
        results = []
        for a in arts:
            a_tag = a.find("a")
            if not a_tag: continue
            title = a_tag.get("title") or a_tag.get_text(strip=True) or "Unknown Title"
            link = a_tag.get("href")
            if link:
                results.append({"title": title, "url": link})
        return results[:15] # maks 15
    except Exception as e:
        log.warning("Lendirqu search failed | q=%r err=%r", query, e)
        return []

async def _do_search(query: str):
    return await asyncio.to_thread(_do_search_sync, query)

def _render_page(search_id: str, data: dict):
    page = data["page"]
    results = data["results"]
    total = len(results)
    per_page = 5
    max_page = max(1, math.ceil(total / per_page))
    
    start = page * per_page
    end = start + per_page
    chunk = results[start:end]
    
    text = f"🔍 <b>LendirQu Search</b>\nQuery: <code>{html.escape(data['query'])}</code>\n\n"
    if not results:
        text += "<i>No results found.</i>"
        return text, None
        
    for i, item in enumerate(chunk):
        idx = start + i + 1
        text += f"<b>{idx}.</b> <a href=\"{html.escape(item['url'],quote=True)}\">{html.escape(item['title'])}</a>\n"
        
    text += f"\n<i>Page {page+1} of {max_page}</i>"
    
    # Buttons
    keyboard = []
    row_nums = []
    for i in range(len(chunk)):
        idx = start + i
        row_nums.append(InlineKeyboardButton(str(idx + 1), callback_data=f"lq:dl:{search_id}:{idx}"))
    if row_nums:
        keyboard.append(row_nums)
        
    row_nav = []
    if page > 0:
        row_nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"lq:nav:{search_id}:{page-1}"))
    row_nav.append(InlineKeyboardButton("❌ Close", callback_data=f"lq:close:{search_id}:0"))
    if page < max_page - 1:
        row_nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"lq:nav:{search_id}:{page+1}"))
        
    keyboard.append(row_nav)
    return text, InlineKeyboardMarkup(keyboard)

async def lendirqu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
        
    msg = update.message
    if not msg or not update.effective_user:
        return
        
    chat = update.effective_chat
    user_id = update.effective_user.id
    
    # Gate restriction
    ok, reason = _premium_link_allowed("https://lendirqu.stream/", user_id, chat.id, chat.type)
    if not ok:
        return await msg.reply_text(_premium_link_block_text("single", reason), parse_mode="HTML")
        
    query = " ".join(context.args).strip()
    if not query:
        return await msg.reply_text("Silakan masukkan kata kunci pencarian. Contoh:\n<code>/lendirqu bokep sma</code>", parse_mode="HTML")
        
    # Bersihkan cache lama (lebih dari 1 jam)
    now = time.time()
    expired = [k for k, v in _SEARCH_CACHE.items() if now - v["ts"] > 3600]
    for k in expired:
        _SEARCH_CACHE.pop(k, None)
        
    status = await msg.reply_text(f"🔍 Mencari <code>{query}</code>...", parse_mode="HTML")
    
    results = await _do_search(query)
    if not results:
        return await status.edit_text(f"❌ Tidak ada hasil untuk <code>{query}</code>.", parse_mode="HTML")
        
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

async def lendirqu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
        
    parts = q.data.split(":")
    if len(parts) != 4 or parts[0] != "lq":
        return
        
    _, action, search_id, arg = parts
    data = _SEARCH_CACHE.get(search_id)
    if not data:
        return await q.answer("Pencarian kadaluarsa.", show_alert=True)
        
    if q.from_user.id != data["user_id"]:
        return await q.answer("Ini bukan pencarianmu.", show_alert=True)
        
    if action == "close":
        _SEARCH_CACHE.pop(search_id, None)
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("Closed.")
        return await q.answer()
        
    if action == "nav":
        data["page"] = int(arg)
        text, markup = _render_page(search_id, data)
        await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
        return await q.answer()
        
    if action == "dl":
        idx = int(arg)
        results = data["results"]
        if idx < 0 or idx >= len(results):
            return await q.answer("Error index.", show_alert=True)
            
        url = results[idx]["url"]
        
        # Check gate lagi
        ok, reason = _premium_link_allowed(url, q.from_user.id, q.message.chat.id, q.message.chat.type)
        if not ok:
            return await q.answer("Gate blocked: " + reason, show_alert=True)
            
        await q.answer("Mendownload...")
        
        # Hapus pencarian dari cache
        _SEARCH_CACHE.pop(search_id, None)
        
        # Edit pesan menu pencarian langsung menjadi status scraping/download (tanpa kirim pesan baru)
        try:
            await q.edit_message_text(
                text=_metadata_status(url),
                parse_mode="HTML"
            )
        except Exception as e:
            log.debug("Failed to edit search message directly | err=%r", e)
        
        reply_to_id = q.message.reply_to_message.message_id if q.message.reply_to_message else None
        
        dl_data = {
            "url": url,
            "user": q.from_user.id,
            "chat_id": q.message.chat.id,
            "chat_type": q.message.chat.type,
            "reply_to": reply_to_id,
            "message_thread_id": getattr(q.message, "message_thread_id", None),
            "ts": time.time(),
        }
        
        # Mulai download worker langsung memakai pesan yang sudah diedit
        await _start_dl_task(
            context=context,
            message=q.message,
            data=dl_data,
            fmt_key="video",
            status_ready=True,
        )
