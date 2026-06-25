import os
import time
from database.db import db_session

USER_SETTINGS_DB = "data/user_settings.sqlite3"

DEFAULT_SETTINGS = {
    "force_autodl": 0,
    "autodl_format": "ask",
    "youtube_resolution": 0,
    "youtube_download_engine": "sonzai",
    "music_format": "flac",
    "silent_download": 0,
    "tiktok_slideshow": "ask",
}

_INIT_DONE = False

def init_user_settings_db():
    global _INIT_DONE
    if _INIT_DONE:
        return
    with db_session(USER_SETTINGS_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                force_autodl INTEGER NOT NULL DEFAULT 0,
                autodl_format TEXT NOT NULL DEFAULT 'ask',
                youtube_resolution INTEGER NOT NULL DEFAULT 0,
                youtube_download_engine TEXT NOT NULL DEFAULT 'sonzai',
                music_format TEXT NOT NULL DEFAULT 'flac',
                silent_download INTEGER NOT NULL DEFAULT 0,
                tiktok_slideshow TEXT NOT NULL DEFAULT 'ask',
                updated_at REAL NOT NULL
            )
        """)
        try:
            cols = [row[1] for row in con.execute("PRAGMA table_info(user_settings)").fetchall()]
            if "youtube_download_engine" not in cols:
                con.execute("ALTER TABLE user_settings ADD COLUMN youtube_download_engine TEXT NOT NULL DEFAULT 'sonzai'")
            if "silent_download" not in cols:
                con.execute("ALTER TABLE user_settings ADD COLUMN silent_download INTEGER NOT NULL DEFAULT 0")
            if "tiktok_slideshow" not in cols:
                con.execute("ALTER TABLE user_settings ADD COLUMN tiktok_slideshow TEXT NOT NULL DEFAULT 'ask'")
        except Exception:
            pass
        con.commit()
    _INIT_DONE = True

def _ensure_user(con, user_id: int):
    now = float(time.time())
    con.execute("""
        INSERT OR IGNORE INTO user_settings
        (user_id, force_autodl, autodl_format, youtube_resolution, youtube_download_engine, music_format, silent_download, tiktok_slideshow, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        int(user_id),
        DEFAULT_SETTINGS["force_autodl"],
        DEFAULT_SETTINGS["autodl_format"],
        DEFAULT_SETTINGS["youtube_resolution"],
        DEFAULT_SETTINGS["youtube_download_engine"],
        DEFAULT_SETTINGS["music_format"],
        DEFAULT_SETTINGS["silent_download"],
        DEFAULT_SETTINGS["tiktok_slideshow"],
        now,
    ))
    con.commit()

def get_user_settings(user_id: int) -> dict:
    init_user_settings_db()
    with db_session(USER_SETTINGS_DB) as con:
        _ensure_user(con, user_id)
        row = con.execute("""
            SELECT force_autodl, autodl_format, youtube_resolution, youtube_download_engine, music_format, silent_download, tiktok_slideshow
            FROM user_settings
            WHERE user_id=?
            LIMIT 1
        """, (int(user_id),)).fetchone()
        
        if not row:
            return dict(DEFAULT_SETTINGS)
        return {
            "force_autodl": int(row[0] or 0),
            "autodl_format": str(row[1] or "ask"),
            "youtube_resolution": int(row[2] or 0),
            "youtube_download_engine": str(row[3] or "sonzai"),
            "music_format": str(row[4] or "flac"),
            "silent_download": int(row[5] or 0),
            "tiktok_slideshow": str(row[6] or "ask"),
        }

def set_force_autodl(user_id: int, enabled: bool):
    init_user_settings_db()
    with db_session(USER_SETTINGS_DB) as con:
        _ensure_user(con, user_id)
        con.execute("""
            UPDATE user_settings
            SET force_autodl=?, updated_at=?
            WHERE user_id=?
        """, (1 if enabled else 0, float(time.time()), int(user_id)))
        con.commit()

def set_autodl_format(user_id: int, value: str):
    value = str(value or "ask").lower().strip()
    if value not in ("ask", "video", "mp3"):
        value = "ask"
    init_user_settings_db()
    with db_session(USER_SETTINGS_DB) as con:
        _ensure_user(con, user_id)
        con.execute("""
            UPDATE user_settings
            SET autodl_format=?, updated_at=?
            WHERE user_id=?
        """, (value, float(time.time()), int(user_id)))
        con.commit()

def set_youtube_resolution(user_id: int, value: int):
    try:
        value = int(value)
    except Exception:
        value = 0
    if value not in (0, 360, 480, 720, 1080):
        value = 0
    init_user_settings_db()
    with db_session(USER_SETTINGS_DB) as con:
        _ensure_user(con, user_id)
        con.execute("""
            UPDATE user_settings
            SET youtube_resolution=?, updated_at=?
            WHERE user_id=?
        """, (value, float(time.time()), int(user_id)))
        con.commit()

def set_youtube_download_engine(user_id: int, value: str):
    value = str(value or "sonzai").lower().strip()
    if value not in ("sonzai", "ytdlp"):
        value = "sonzai"
    init_user_settings_db()
    with db_session(USER_SETTINGS_DB) as con:
        _ensure_user(con, user_id)
        con.execute("""
            UPDATE user_settings
            SET youtube_download_engine=?, updated_at=?
            WHERE user_id=?
        """, (value, float(time.time()), int(user_id)))
        con.commit()

def set_music_format(user_id: int, value: str):
    value = str(value or "flac").lower().strip()
    if value not in ("flac", "mp3"):
        value = "flac"
    init_user_settings_db()
    with db_session(USER_SETTINGS_DB) as con:
        _ensure_user(con, user_id)
        con.execute("""
            UPDATE user_settings
            SET music_format=?, updated_at=?
            WHERE user_id=?
        """, (value, float(time.time()), int(user_id)))
        con.commit()

def set_silent_download(user_id: int, enabled: bool):
    init_user_settings_db()
    with db_session(USER_SETTINGS_DB) as con:
        _ensure_user(con, user_id)
        con.execute("""
            UPDATE user_settings
            SET silent_download=?, updated_at=?
            WHERE user_id=?
        """, (1 if enabled else 0, float(time.time()), int(user_id)))
        con.commit()

def set_tiktok_slideshow(user_id: int, value: str):
    value = str(value or "ask").lower().strip()
    if value not in ("ask", "images", "video", "audio"):
        value = "ask"
    init_user_settings_db()
    with db_session(USER_SETTINGS_DB) as con:
        _ensure_user(con, user_id)
        con.execute("""
            UPDATE user_settings
            SET tiktok_slideshow=?, updated_at=?
            WHERE user_id=?
        """, (value, float(time.time()), int(user_id)))
        con.commit()
