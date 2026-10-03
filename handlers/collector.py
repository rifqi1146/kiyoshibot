import os
import time
import asyncio
import logging
from database.db import db_session
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

BROADCAST_DB = "data/broadcast.sqlite3"

# Throttle: `updated_at` hanya bermakna "kapan terakhir chat/user terlihat".
# Menulis ulang tiap pesan (grup ramai) memboroskan disk I/O + lock SQLite
# tanpa menambah informasi. Simpan mtime terakhir di memori supaya satu chat
# atau satu user cukup ditulis sekali per COLLECT_TTL_SEC. Tabelnya sudah
# persisten dan entri cache hilang saat restart -> paling lambat langsung
# ditulis ulang setelah boot.
COLLECT_TTL_SEC = float(os.getenv("BROADCAST_COLLECT_TTL_SEC", "1800"))

_COLLECT_SEEN: dict[tuple, float] = {}
_MAX_COLLECT_SEEN = 20000


def _seen_recently(key: tuple, now: float) -> bool:
    ts = _COLLECT_SEEN.get(key)
    return bool(ts and (now - ts) < COLLECT_TTL_SEC)


def _mark_seen(key: tuple, now: float) -> None:
    if len(_COLLECT_SEEN) > _MAX_COLLECT_SEEN:
        _COLLECT_SEEN.clear()
    _COLLECT_SEEN[key] = now


def _db_init():
    with db_session(BROADCAST_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_users (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_groups (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_user_cache (
                username TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        con.commit()


def _collect_sync(chat_id: int, chat_type: str, usernames: list[tuple[int, str]]) -> None:
    """Persist chat membership + username cache in a single transaction.

    Runs the whole unit of work (open/execute/commit/close) inside one
    thread so the SQLite connection never crosses thread boundaries.
    DDL tidak dijalankan di sini lagi (dulu 3x CREATE TABLE tiap pesan);
    tabel dijamin sudah ada oleh `_db_init()` saat import.
    """
    chat_key = (chat_id, chat_type)
    user_keys = [(int(chat_id), u) for _, u in usernames]
    now = float(time.time())

    try:
        needs_chat = not _seen_recently(chat_key, now)
        needs_users = bool(usernames) and any(not _seen_recently(k, now) for k in user_keys)
        if not needs_chat and not needs_users:
            return

        with db_session(BROADCAST_DB) as con:
            if needs_chat:
                if chat_type == "private":
                    con.execute(
                        """
                        INSERT INTO broadcast_users (chat_id, enabled, updated_at)
                        VALUES (?, 1, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET
                          enabled=1,
                          updated_at=excluded.updated_at
                        """,
                        (int(chat_id), now),
                    )
                else:
                    con.execute(
                        """
                        INSERT INTO broadcast_groups (chat_id, enabled, updated_at)
                        VALUES (?, 1, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET
                          enabled=1,
                          updated_at=excluded.updated_at
                        """,
                        (int(chat_id), now),
                    )

            if needs_users:
                con.executemany(
                    """
                    INSERT INTO broadcast_user_cache (username, user_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                      user_id=excluded.user_id,
                      updated_at=excluded.updated_at
                    """,
                    [(u, int(uid), now) for uid, u in usernames],
                )
            con.commit()
    except Exception:
        # Jangan pernah bikin handler pesan gagal gara-gara cache collector.
        log.warning("Collector persist failed | chat_id=%s", chat_id, exc_info=True)
        return

    if needs_chat:
        _mark_seen(chat_key, now)
    if needs_users:
        for k in user_keys:
            _mark_seen(k, now)


def _normalize_username(username: str | None) -> str:
    return (username or "").strip().lstrip("@").lower()


async def collect_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return

    usernames: list[tuple[int, str]] = []
    seen: set[str] = set()
    for source in (update.effective_user, getattr(update.message, "reply_to_message", None) and update.message.reply_to_message.from_user):
        if not source or not getattr(source, "id", None):
            continue
        u = _normalize_username(getattr(source, "username", None))
        if u and u not in seen:
            seen.add(u)
            usernames.append((int(source.id), u))

    await asyncio.to_thread(_collect_sync, int(chat.id), chat.type, usernames)


try:
    _db_init()
except Exception:
    pass
