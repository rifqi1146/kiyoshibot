import time

from database.db import db_session

NSFW_DB = "data/nsfw.sqlite3"


def nsfw_db_init():
    with db_session(NSFW_DB) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS nsfw_groups (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            )
            """
        )
        con.commit()


def is_nsfw_allowed(chat_id: int, chat_type: str) -> bool:
    if chat_type == "private":
        return True

    with db_session(NSFW_DB) as con:
        cur = con.execute(
            "SELECT 1 FROM nsfw_groups WHERE chat_id=? AND enabled=1",
            (int(chat_id),),
        )
        return cur.fetchone() is not None


def set_nsfw(chat_id: int, enabled: bool):
    with db_session(NSFW_DB) as con:
        now = time.time()
        if enabled:
            con.execute(
                """
                INSERT INTO nsfw_groups (chat_id, enabled, updated_at)
                VALUES (?,1,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  enabled=1,
                  updated_at=excluded.updated_at
                """,
                (int(chat_id), now),
            )
        else:
            con.execute(
                """
                INSERT INTO nsfw_groups (chat_id, enabled, updated_at)
                VALUES (?,0,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  enabled=0,
                  updated_at=excluded.updated_at
                """,
                (int(chat_id), now),
            )
        con.commit()


def get_all_enabled():
    with db_session(NSFW_DB) as con:
        cur = con.execute(
            "SELECT chat_id FROM nsfw_groups WHERE enabled=1"
        )
        return [int(r[0]) for r in cur.fetchall()]
        