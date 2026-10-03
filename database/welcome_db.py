import os
import time
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WELCOME_VERIFY_DB = os.path.abspath(
    os.path.join(BASE_DIR, "..", "data", "welcome_verify.sqlite3")
)


def _connect():
    os.makedirs(os.path.dirname(WELCOME_VERIFY_DB), exist_ok=True)
    # PRAGMAs WAJIB di setiap koneksi, bukan cuma saat init: `busy_timeout`
    # membuat writer menunggu 30s alih-alih langsung `database is locked`,
    # dan WAL membuat reader tidak memblokir writer.
    con = sqlite3.connect(WELCOME_VERIFY_DB, timeout=30.0)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=30000;")
    except sqlite3.Error:
        pass
    return con


def init_welcome_db():
    con = _connect()
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS welcome_chats (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                verified_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_welcome (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )

        con.commit()

        # migration lama untuk welcome_chats
        cur = con.execute("PRAGMA table_info(welcome_chats)")
        cols = cur.fetchall()
        pk_on_chat_id = False
        for c in cols:
            name = c[1]
            is_pk = c[5]
            if name == "chat_id" and int(is_pk) == 1:
                pk_on_chat_id = True
                break

        if not pk_on_chat_id:
            con.execute("ALTER TABLE welcome_chats RENAME TO welcome_chats_old")

            con.execute(
                """
                CREATE TABLE welcome_chats (
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                )
                """
            )

            con.execute(
                """
                INSERT OR REPLACE INTO welcome_chats (chat_id, enabled, updated_at)
                SELECT
                    COALESCE(chat_id, id) as chat_id,
                    COALESCE(enabled, 1) as enabled,
                    COALESCE(updated_at, strftime('%s','now')) as updated_at
                FROM welcome_chats_old
                """
            )

            con.execute("DROP TABLE welcome_chats_old")
            con.commit()

        # migration untuk pending_welcome.created_at
        cur = con.execute("PRAGMA table_info(pending_welcome)")
        pending_cols = {row[1] for row in cur.fetchall()}
        if "created_at" not in pending_cols:
            con.execute(
                "ALTER TABLE pending_welcome ADD COLUMN created_at REAL NOT NULL DEFAULT 0"
            )
            con.execute(
                "UPDATE pending_welcome SET created_at=? WHERE created_at=0",
                (time.time(),)
            )
            con.commit()

    finally:
        con.close()


def load_welcome_chats() -> set[int]:
    con = _connect()
    try:
        cur = con.execute("SELECT chat_id FROM welcome_chats WHERE enabled=1")
        return {int(r[0]) for r in cur.fetchall() if r and r[0] is not None}
    finally:
        con.close()


def save_welcome_chats(enabled_chats: set[int]):
    con = _connect()
    try:
        now = time.time()
        con.execute("BEGIN")
        con.execute("UPDATE welcome_chats SET enabled=0, updated_at=?", (now,))
        if enabled_chats:
            con.executemany(
                """
                INSERT INTO welcome_chats (chat_id, enabled, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  enabled=1,
                  updated_at=excluded.updated_at
                """,
                [(int(cid), now) for cid in enabled_chats],
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


def load_verified() -> dict[int, set[int]]:
    con = _connect()
    try:
        cur = con.execute("SELECT chat_id, user_id FROM verified_users")
        out = {}
        for chat_id, user_id in cur.fetchall():
            out.setdefault(int(chat_id), set()).add(int(user_id))
        return out
    finally:
        con.close()


def save_verified_user(chat_id: int, user_id: int):
    con = _connect()
    try:
        now = time.time()
        con.execute(
            """
            INSERT INTO verified_users (chat_id, user_id, verified_at)
            VALUES (?, ?, ?)
            """,
            (int(chat_id), int(user_id), now),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.execute(
            "UPDATE verified_users SET verified_at=? WHERE chat_id=? AND user_id=?",
            (now, int(chat_id), int(user_id)),
        )
        con.commit()
    finally:
        con.close()


def delete_verified_user(chat_id: int, user_id: int):
    con = _connect()
    try:
        con.execute(
            "DELETE FROM verified_users WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        con.commit()
    finally:
        con.close()


def save_pending_welcome(chat_id: int, user_id: int, message_id: int):
    con = _connect()
    try:
        now = time.time()
        con.execute(
            """
            INSERT INTO pending_welcome (chat_id, user_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(chat_id), int(user_id), int(message_id), now),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.execute(
            "UPDATE pending_welcome SET message_id=?, created_at=? WHERE chat_id=? AND user_id=?",
            (int(message_id), now, int(chat_id), int(user_id)),
        )
        con.commit()
    finally:
        con.close()


def load_pending_welcomes() -> list[dict]:
    con = _connect()
    try:
        cur = con.execute(
            """
            SELECT chat_id, user_id, message_id, created_at
            FROM pending_welcome
            """
        )
        rows = cur.fetchall()

        return [
            {
                "chat_id": int(chat_id),
                "user_id": int(user_id),
                "message_id": int(message_id),
                "created_at": float(created_at),
            }
            for chat_id, user_id, message_id, created_at in rows
        ]
    finally:
        con.close()


def pop_pending_welcome(chat_id: int, user_id: int) -> int | None:
    con = _connect()
    try:
        cur = con.execute(
            "SELECT message_id FROM pending_welcome WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        row = cur.fetchone()

        con.execute(
            "DELETE FROM pending_welcome WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        con.commit()

        if not row:
            return None
        return int(row[0])
    finally:
        con.close()