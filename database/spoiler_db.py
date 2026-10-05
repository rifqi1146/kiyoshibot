import time

from database.db import db_session

SPOILER_DB = "data/spoiler.sqlite3"


def spoiler_db_init():
    with db_session(SPOILER_DB) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS spoiler_groups (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            )
            """
        )
        con.commit()


def is_spoiler_enabled(chat_id: int, chat_type: str) -> bool:
    """Spoiler setting bersifat per grup.

    Private chat tidak punya setting grup, jadi spoiler selalu nonaktif
    (media dikirim normal) kecuali grup mengaktifkannya.
    """
    if chat_type == "private":
        return False
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return False
    with db_session(SPOILER_DB) as con:
        cur = con.execute(
            "SELECT 1 FROM spoiler_groups WHERE chat_id=? AND enabled=1",
            (cid,),
        )
        return cur.fetchone() is not None


def set_spoiler(chat_id: int, enabled: bool):
    with db_session(SPOILER_DB) as con:
        now = time.time()
        con.execute(
            """
            INSERT INTO spoiler_groups (chat_id, enabled, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
              enabled=excluded.enabled,
              updated_at=excluded.updated_at
            """,
            (int(chat_id), 1 if enabled else 0, now),
        )
        con.commit()
