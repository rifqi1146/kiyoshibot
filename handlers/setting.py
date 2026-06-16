from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from database.user_settings_db import (
    get_user_settings,
    set_force_autodl,
    set_autodl_format,
    set_youtube_resolution,
    set_music_format,
)
from database.download_db import is_premium_user

def _fmt_bool(v: int) -> str:
    return "ON" if int(v) else "OFF"

def _fmt_autodl_format(v: str) -> str:
    mapping = {
        "ask": "Ask",
        "video": "Video",
        "mp3": "MP3",
    }
    return mapping.get(str(v).lower(), "Ask")

def _fmt_res(v: int) -> str:
    v = int(v or 0)
    return "Ask" if v == 0 else f"{v}p"

def _fmt_music(v: str) -> str:
    mapping = {
        "flac": "FLAC",
        "mp3": "MP3",
    }
    return mapping.get(str(v).lower(), "FLAC")

def _cb(user_id: int, source: str, action: str, key: str, value: str | int | None = None) -> str:
    parts = ["setting", str(user_id), source, action, key]
    if value is not None:
        parts.append(str(value))
    return ":".join(parts)

def _settings_text(user_id: int) -> str:
    s = get_user_settings(user_id)
    return (
        "<b>User Settings</b>\n\n"
        f"<b>AutoDL in all groups:</b> <code>{_fmt_bool(s['force_autodl'])}</code>\n"
        f"<b>Default downloader format:</b> <code>{_fmt_autodl_format(s['autodl_format'])}</code>\n"
        f"<b>YouTube resolution:</b> <code>{_fmt_res(s['youtube_resolution'])}</code>\n"
        f"<b>Music output format:</b> <code>{_fmt_music(s['music_format'])}</code>"
    )

def _footer_buttons(user_id: int, source: str):
    if source == "help":
        return [[
            InlineKeyboardButton("Back", callback_data=f"help:{user_id}:settings"),
            InlineKeyboardButton("Close", callback_data=_cb(user_id, source, "close", "x")),
        ]]
    return [[
        InlineKeyboardButton("Close", callback_data=_cb(user_id, source, "close", "x")),
    ]]

def _main_keyboard(user_id: int, source: str = "direct") -> InlineKeyboardMarkup:
    s = get_user_settings(user_id)
    rows = [
        [
            InlineKeyboardButton(
                f"AutoDL All Groups: {_fmt_bool(s['force_autodl'])}",
                callback_data=_cb(user_id, source, "toggle", "force_autodl"),
            )
        ],
        [
            InlineKeyboardButton(
                f"Downloader Format: {_fmt_autodl_format(s['autodl_format'])}",
                callback_data=_cb(user_id, source, "menu", "autodl_format"),
            )
        ],
        [
            InlineKeyboardButton(
                f"YouTube Resolution: {_fmt_res(s['youtube_resolution'])}",
                callback_data=_cb(user_id, source, "menu", "youtube_resolution"),
            )
        ],
        [
            InlineKeyboardButton(
                f"Music Format: {_fmt_music(s['music_format'])}",
                callback_data=_cb(user_id, source, "menu", "music_format"),
            )
        ],
    ]
    rows.extend(_footer_buttons(user_id, source))
    return InlineKeyboardMarkup(rows)

def _autodl_format_keyboard(user_id: int, source: str = "direct") -> InlineKeyboardMarkup:
    s = get_user_settings(user_id)
    current = str(s["autodl_format"]).lower()
    def label(v: str, t: str) -> str:
        return f"• {t}" if current == v else t
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(label("ask", "Ask"), callback_data=_cb(user_id, source, "set", "autodl_format", "ask")),
            InlineKeyboardButton(label("video", "Video"), callback_data=_cb(user_id, source, "set", "autodl_format", "video")),
            InlineKeyboardButton(label("mp3", "MP3"), callback_data=_cb(user_id, source, "set", "autodl_format", "mp3")),
        ],
        [
            InlineKeyboardButton("Back", callback_data=_cb(user_id, source, "menu", "main"))
        ],
    ])

def _youtube_resolution_keyboard(user_id: int, source: str = "direct") -> InlineKeyboardMarkup:
    s = get_user_settings(user_id)
    current = int(s["youtube_resolution"] or 0)
    def label(v: int) -> str:
        text = "Ask" if v == 0 else f"{v}p"
        if v == 1080:
            text += " ⭐️"
        return f"• {text}" if current == v else text
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(label(0), callback_data=_cb(user_id, source, "set", "youtube_resolution", 0)),
            InlineKeyboardButton(label(360), callback_data=_cb(user_id, source, "set", "youtube_resolution", 360)),
            InlineKeyboardButton(label(480), callback_data=_cb(user_id, source, "set", "youtube_resolution", 480)),
        ],
        [
            InlineKeyboardButton(label(720), callback_data=_cb(user_id, source, "set", "youtube_resolution", 720)),
            InlineKeyboardButton(label(1080), callback_data=_cb(user_id, source, "set", "youtube_resolution", 1080)),
        ],
        [
            InlineKeyboardButton("Back", callback_data=_cb(user_id, source, "menu", "main"))
        ],
    ])

def _music_format_keyboard(user_id: int, source: str = "direct") -> InlineKeyboardMarkup:
    s = get_user_settings(user_id)
    current = str(s["music_format"]).lower()
    def label(v: str, t: str) -> str:
        return f"• {t}" if current == v else t
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(label("flac", "FLAC"), callback_data=_cb(user_id, source, "set", "music_format", "flac")),
            InlineKeyboardButton(label("mp3", "MP3"), callback_data=_cb(user_id, source, "set", "music_format", "mp3")),
        ],
        [
            InlineKeyboardButton("Back", callback_data=_cb(user_id, source, "menu", "main"))
        ],
    ])

async def render_settings_message(message, user_id: int, source: str = "direct"):
    return await message.edit_text(
        _settings_text(user_id),
        parse_mode="HTML",
        reply_markup=_main_keyboard(user_id, source),
    )

async def setting_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    await msg.reply_text(
        _settings_text(user.id),
        parse_mode="HTML",
        reply_markup=_main_keyboard(user.id, "direct"),
    )

async def setting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    parts = q.data.split(":")
    if len(parts) < 5 or parts[0] != "setting":
        return
    try:
        owner_id = int(parts[1])
    except Exception:
        return await q.answer("Invalid setting menu.", show_alert=True)
        
    if q.from_user.id != owner_id:
        return await q.answer("This is not your settings menu.", show_alert=True)
        
    source = parts[2]
    action = parts[3]
    key = parts[4]
    
    if action == "close":
        await q.answer()
        return await q.message.delete()
        
    if action == "toggle" and key == "force_autodl":
        current = get_user_settings(owner_id)
        set_force_autodl(owner_id, not bool(current["force_autodl"]))
        await q.answer("Setting updated.")
        return await q.message.edit_text(
            _settings_text(owner_id),
            parse_mode="HTML",
            reply_markup=_main_keyboard(owner_id, source),
        )
        
    if action == "menu":
        await q.answer()
        if key == "main":
            return await q.message.edit_text(
                _settings_text(owner_id),
                parse_mode="HTML",
                reply_markup=_main_keyboard(owner_id, source),
            )
        if key == "autodl_format":
            return await q.message.edit_text(
                _settings_text(owner_id),
                parse_mode="HTML",
                reply_markup=_autodl_format_keyboard(owner_id, source),
            )
        if key == "youtube_resolution":
            return await q.message.edit_text(
                _settings_text(owner_id),
                parse_mode="HTML",
                reply_markup=_youtube_resolution_keyboard(owner_id, source),
            )
        if key == "music_format":
            return await q.message.edit_text(
                _settings_text(owner_id),
                parse_mode="HTML",
                reply_markup=_music_format_keyboard(owner_id, source),
            )
        return
        
    if action == "set":
        if len(parts) < 6:
            return await q.answer("Invalid setting value.", show_alert=True)
        value = parts[5]
        
        if key == "autodl_format":
            set_autodl_format(owner_id, value)
            
        elif key == "youtube_resolution":
            try:
                res_val = int(value)
                if res_val > 720 and not is_premium_user(owner_id):
                    return await q.answer("You are not a premium user! Upgrade to select resolutions above 720p.", show_alert=True)
                set_youtube_resolution(owner_id, res_val)
            except Exception:
                set_youtube_resolution(owner_id, 0)
                
        elif key == "music_format":
            set_music_format(owner_id, value)
            
        else:
            return await q.answer("Unknown setting.", show_alert=True)
            
        await q.answer("Setting updated.")
        return await q.message.edit_text(
            _settings_text(owner_id),
            parse_mode="HTML",
            reply_markup=_main_keyboard(owner_id, source),
        )
