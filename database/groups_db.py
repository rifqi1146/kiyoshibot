from database.db import db_session

BROADCAST_DB = "data/broadcast.sqlite3"
_INIT_DONE = False

def _db_init():
    global _INIT_DONE
    if _INIT_DONE:
        return
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
        con.commit()
    _INIT_DONE = True


def _load_groups() -> list[int]:
    _db_init()
    with db_session(BROADCAST_DB) as con:
        rows = con.execute(
            "SELECT chat_id FROM broadcast_groups WHERE enabled=1"
        ).fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]