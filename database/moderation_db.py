import os
import time
import sqlite3

MODERATION_DB = "data/moderation.sqlite3"
BROADCAST_DB = "data/broadcast.sqlite3"
SUDO_DB = "data/sudouser.sqlite3"


def _ensure_data_dir():
    os.makedirs("data", exist_ok=True)


def _init_db(path: str, ddl: str):
    _ensure_data_dir()
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute(ddl)
        con.commit()
    finally:
        con.close()


def init_moderation_db():
    _init_db(
        MODERATION_DB,
        """
        CREATE TABLE IF NOT EXISTS moderation_groups (
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )
        """,
    )


def init_sudo_db():
    _init_db(
        SUDO_DB,
        """
        CREATE TABLE IF NOT EXISTS sudo_users (
            user_id INTEGER PRIMARY KEY,
            added_at REAL NOT NULL
        )
        """,
    )


def init_moderation_storage():
    init_moderation_db()
    init_sudo_db()


def _moderation_db():
    init_moderation_db()
    return _open(MODERATION_DB)


def _sudo_db():
    init_sudo_db()
    return _open(SUDO_DB)


def _open(path: str) -> sqlite3.Connection:
    """Koneksi dengan PRAGMAs yang sama seperti helper init.

    Tanpa `busy_timeout`, query yang bertabrakan dengan writer lain langsung
    melempar `sqlite3.OperationalError: database is locked` (error ke user),
    padahal menunggu 30 detik sudah cukup untuk menyelesaikannya.
    """
    con = sqlite3.connect(path, timeout=30.0)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=30000;")
    except sqlite3.Error:
        pass
    return con


def moderation_is_enabled(chat_id: int) -> bool:
    con = _moderation_db()
    try:
        row = con.execute(
            "SELECT enabled FROM moderation_groups WHERE chat_id=? LIMIT 1",
            (int(chat_id),),
        ).fetchone()
        return bool(row and int(row[0]) == 1)
    finally:
        con.close()


def moderation_set(chat_id: int, enabled: bool):
    con = _moderation_db()
    try:
        now = float(time.time())
        con.execute("BEGIN")
        con.execute(
            """
            INSERT INTO moderation_groups (chat_id, enabled, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
              enabled=excluded.enabled,
              updated_at=excluded.updated_at
            """,
            (int(chat_id), 1 if enabled else 0, now),
        )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


def sudo_is(user_id: int) -> bool:
    con = _sudo_db()
    try:
        row = con.execute(
            "SELECT 1 FROM sudo_users WHERE user_id=? LIMIT 1",
            (int(user_id),),
        ).fetchone()
        return bool(row)
    finally:
        con.close()


def sudo_add(user_id: int):
    con = _sudo_db()
    try:
        now = float(time.time())
        con.execute("BEGIN")
        con.execute(
            """
            INSERT INTO sudo_users (user_id, added_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              added_at=excluded.added_at
            """,
            (int(user_id), now),
        )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


def sudo_remove(user_id: int):
    con = _sudo_db()
    try:
        con.execute("BEGIN")
        con.execute("DELETE FROM sudo_users WHERE user_id=?", (int(user_id),))
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


def sudo_list() -> list[int]:
    con = _sudo_db()
    try:
        rows = con.execute(
            "SELECT user_id FROM sudo_users ORDER BY added_at ASC"
        ).fetchall()
        return [int(x[0]) for x in rows if x and x[0] is not None]
    finally:
        con.close()


def lookup_user_id(username: str) -> int | None:
    u = (username or "").strip().lstrip("@").lower()
    if not u:
        return None

    con = sqlite3.connect(BROADCAST_DB)
    try:
        row = con.execute(
            "SELECT user_id FROM broadcast_user_cache WHERE username=? LIMIT 1",
            (u,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None
    finally:
        con.close()