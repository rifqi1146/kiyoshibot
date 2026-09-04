import asyncio, os, shutil, glob, html, tempfile, time, uuid
from urllib.parse import urlparse, parse_qs
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from database.user_settings_db import get_user_settings

try:
    from ytSearch import VideosSearch
except Exception:
    VideosSearch = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "data", "cookies.txt"))
DOWNLOADS_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "downloads"))
DENO_BIN = os.getenv("DENO_BIN") or shutil.which("deno") or "/root/.deno/bin/deno"
MUSIC_PAGE_SIZE = 5
MUSIC_MAX_RESULTS = 10
MUSIC_CACHE_TTL = 900

def _base_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["default", "web_embedded"],
                "player_skip": ["tv", "tv_downgraded"],
            }
        },
    }

    if DENO_BIN and os.path.exists(DENO_BIN):
        opts["js_runtimes"] = {"deno": {"path": DENO_BIN}}
    elif shutil.which("node"):
        opts["js_runtimes"] = {"node": {"path": shutil.which("node")}}

    if os.path.exists(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH
        
    return opts

def _video_id_from_url(url: str) -> str | None:
    try:
        p = urlparse(url or "")
        host = (p.hostname or "").lower()
        if host == "youtu.be":
            return p.path.strip("/").split("/")[0] or None
        if "youtube.com" in host:
            q = parse_qs(p.query)
            if q.get("v"):
                return q["v"][0]
            parts = [x for x in p.path.split("/") if x]
            if "shorts" in parts:
                idx = parts.index("shorts")
                if len(parts) > idx + 1:
                    return parts[idx + 1]
    except Exception:
        pass
    return None

def _video_id_from_entry(entry: dict) -> str | None:
    video_id = str(entry.get("id") or entry.get("videoId") or "").strip()
    return video_id or _video_id_from_url(str(entry.get("link") or entry.get("url") or ""))

def _duration_to_seconds(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    try:
        parts = [int(x) for x in text.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        return None
    return None

def _format_duration(value) -> str:
    seconds = _duration_to_seconds(value)
    if not seconds:
        return "?:??"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _entry_title(entry: dict) -> str:
    return str(entry.get("title") or "Untitled").strip() or "Untitled"

def _entry_uploader(entry: dict) -> str:
    channel = entry.get("channel")
    if isinstance(channel, dict):
        name = channel.get("name") or channel.get("title")
        if name:
            return str(name)
    return str(entry.get("uploader") or entry.get("channelName") or entry.get("author") or "Unknown")

def _music_store(context: ContextTypes.DEFAULT_TYPE) -> dict:
    store = context.application.bot_data.setdefault("_music_search_cache", {})
    now = time.time()
    for key, val in list(store.items()):
        if now - float(val.get("created_at", 0)) > MUSIC_CACHE_TTL:
            store.pop(key, None)
    return store

def _make_token() -> str:
    return uuid.uuid4().hex[:10]

def _get_cached_music(context, token):
    return _music_store(context).get(token)

def _build_results_message(payload: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    entries = payload["entries"]
    owner_id = payload["owner_id"]
    token = payload["token"]
    total = len(entries)
    pages = max((total + MUSIC_PAGE_SIZE - 1) // MUSIC_PAGE_SIZE, 1)
    page = max(0, min(page, pages - 1))
    start = page * MUSIC_PAGE_SIZE
    end = min(start + MUSIC_PAGE_SIZE, total)
    text = f"<b>Music Search Results</b>\n<i>Page {page+1}/{pages}</i>\n\n"
    keyboard = []
    for idx, entry in enumerate(entries[start:end], start=start):
        number = idx + 1
        title = html.escape(entry["title"])
        uploader = html.escape(entry["uploader"])
        duration = html.escape(_format_duration(entry.get("duration")))
        text += f"{number}. <b>{title}</b>\n   By: {uploader} ({duration})\n\n"
        keyboard.append([InlineKeyboardButton(f"Select {number}", callback_data=f"music_download:{owner_id}:{token}:{idx}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"music_page:{owner_id}:{token}:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"music_page:{owner_id}:{token}:{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data=f"music_cancel:{owner_id}:{token}")])
    return text, InlineKeyboardMarkup(keyboard)

async def _search_music(search_query: str, limit: int = MUSIC_MAX_RESULTS) -> list[dict]:
    if VideosSearch is None:
        raise RuntimeError("ytSearch module not found. Install yt-search-py or py-yt-search.")
    search = VideosSearch(search_query, limit=limit)
    result = await search.next()
    entries = (result or {}).get("result") or []
    out = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        video_id = _video_id_from_entry(item)
        if not video_id:
            continue
        out.append({
            "id": video_id,
            "title": _entry_title(item),
            "uploader": _entry_uploader(item),
            "duration": item.get("duration"),
            "link": item.get("link") or f"https://www.youtube.com/watch?v={video_id}"
        })
    return out[:limit]

def _download_music_sync(video_id: str, output_format: str):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed on the system.")
    output_format = str(output_format or "flac").lower().strip()
    if output_format not in ("flac", "mp3"):
        output_format = "flac"
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    job_dir = tempfile.mkdtemp(prefix="music_", dir=DOWNLOADS_DIR)
    
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bestaudio/best",
        "outtmpl": os.path.join(job_dir, "%(title).120s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": output_format,
            "preferredquality": "192"
        }]
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        
    pattern = "*.mp3" if output_format == "mp3" else "*.flac"
    files = glob.glob(os.path.join(job_dir, pattern))
    if not files:
        raise RuntimeError("Audio file not found.")
    file_path = max(files, key=os.path.getmtime)
    return info or {}, file_path, job_dir

async def music_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    msg = update.effective_message
    query = " ".join(context.args).strip()
    if not query:
        return await msg.reply_text("🎵 <b>Music Command</b>\n\n<code>/music &lt;song title or artist name&gt;</code>", parse_mode="HTML")
    status_msg = await msg.reply_text("⏳ <b>Searching music...</b>\n\nPlease wait 🎧", reply_to_message_id=msg.message_id, parse_mode="HTML")
    try:
        entries = await _search_music(query, limit=MUSIC_MAX_RESULTS)
        if not entries:
            raise RuntimeError("No matching songs or videos were found.")
        user_id = msg.from_user.id if msg.from_user else 0
        token = _make_token()
        payload = {"token": token, "owner_id": user_id, "query": query, "entries": entries, "created_at": time.time()}
        _music_store(context)[token] = payload
        text, markup = _build_results_message(payload, 0)
        await status_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"<b>Failed to search music</b>\n\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")

async def music_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3 or not data.startswith("music_"):
        return await query.answer("Invalid music request.", show_alert=True)
    action = parts[0]
    try:
        owner_id = int(parts[1])
        token = parts[2]
    except Exception:
        return await query.answer("Invalid music request.", show_alert=True)
    if query.from_user.id != owner_id:
        return await query.answer("Not your request.", show_alert=True)
    if not await require_join_or_block(update, context):
        return
    payload = _get_cached_music(context, token)
    if not payload:
        return await query.answer("Music request expired.", show_alert=True)
    if action == "music_page":
        try:
            page = int(parts[3])
        except Exception:
            page = 0
        text, markup = _build_results_message(payload, page)
        await query.answer()
        return await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    if action == "music_cancel":
        _music_store(context).pop(token, None)
        await query.answer("Cancelled.")
        try:
            return await query.message.delete()
        except Exception:
            return await query.edit_message_text("<b>Cancelled.</b>", parse_mode="HTML")
    if action != "music_download":
        return await query.answer("Invalid action.", show_alert=True)
    try:
        idx = int(parts[3])
        entry = payload["entries"][idx]
        video_id = entry["id"]
    except Exception:
        return await query.answer("Invalid selected music.", show_alert=True)
    await query.answer()
    chat_id = query.message.chat_id
    reply_to_id = query.message.reply_to_message.message_id if query.message and query.message.reply_to_message else None
    settings = get_user_settings(query.from_user.id)
    output_format = str(settings.get("music_format") or "flac").lower()
    job_dir = None
    try:
        await query.edit_message_text("⏳ <b>Downloading music...</b>\n\nOutput format: <b>{}</b>".format(html.escape(output_format.upper())), parse_mode="HTML")
        entry_info, file_path, job_dir = await asyncio.to_thread(_download_music_sync, video_id, output_format)
        title = str(entry_info.get("title") or entry.get("title") or "Audio")
        performer = str(entry_info.get("uploader") or entry_info.get("channel") or entry.get("uploader") or "Unknown")
        duration = _duration_to_seconds(entry_info.get("duration") or entry.get("duration"))
        with open(file_path, "rb") as audio_file:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                filename=os.path.basename(file_path),
                title=title[:64],
                performer=performer[:64],
                duration=duration,
                caption=f"🎵 <b>Download Successful</b>\n\n<b>Title:</b> {html.escape(title)}",
                reply_to_message_id=reply_to_id,
                parse_mode="HTML"
            )
        _music_store(context).pop(token, None)
        await query.message.delete()
    except Exception as e:
        await query.edit_message_text(f"<b>Failed to download music</b>\n\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")
    finally:
        if job_dir and os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
