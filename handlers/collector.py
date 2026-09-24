import time
import asyncio
from database.db import db_session
from telegram import Update
from telegram.ext import ContextTypes

BROADCAST_DB = "data/broadcast.sqlite3"


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
    """
    _db_init()
    now = float(time.time())
    with db_session(BROADCAST_DB) as con:
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

        if usernames:
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
