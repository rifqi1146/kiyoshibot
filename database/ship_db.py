import time
from database.db import db_session

SHIP_DB = "data/ship.sqlite3"
_INIT_DONE = False
_RECENT_UPDATES: dict[tuple[int, int], tuple[str, float]] = {}
_MAX_RECENT_CACHE = 5000


def _ship_db_init():
    global _INIT_DONE
    if _INIT_DONE:
        return
    with db_session(SHIP_DB) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ship_state (
                chat_id INTEGER PRIMARY KEY,
                last_time INTEGER NOT NULL
            )
            """
        )
        # Bersihkan sisa data DM (id positif) yang pernah masuk pool ship
        con.execute("DELETE FROM users WHERE chat_id > 0")
        con.execute("DELETE FROM ship_state WHERE chat_id > 0")
        con.commit()
    _INIT_DONE = True


def add_user(chat_id: int, user):
    if not user or getattr(user, "is_bot", False):
        return

    cid = int(chat_id)
    if cid > 0:
        # Private chat (DM) bukan anggota grup, jangan masuk pool ship
        return

    uid = int(user.id)
    name = str(getattr(user, "first_name", "") or "")
    now = time.time()

    # Throttle frequent updates for same user in same chat if name unchanged
    cached = _RECENT_UPDATES.get((cid, uid))
    if cached and cached[0] == name and (now - cached[1]) < 1800:
        return

    _ship_db_init()
    with db_session(SHIP_DB) as con:
        con.execute(
            """
            INSERT INTO users (chat_id, user_id, name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
              name=excluded.name,
              updated_at=excluded.updated_at
            """,
            (cid, uid, name, float(now)),
        )
        con.commit()

    if len(_RECENT_UPDATES) > _MAX_RECENT_CACHE:
        _RECENT_UPDATES.clear()
    _RECENT_UPDATES[(cid, uid)] = (name, now)


def _ship_state_has_updated_at(con) -> bool:
    try:
        cur = con.execute("PRAGMA table_info(ship_state)")
        cols = {row[1] for row in cur.fetchall() if row and len(row) > 1}
        return "updated_at" in cols
    except Exception:
        return False


def get_ship_last_time(chat_id: int) -> int:
    _ship_db_init()
    with db_session(SHIP_DB) as con:
        cur = con.execute(
            "SELECT last_time FROM ship_state WHERE chat_id=?",
            (int(chat_id),),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0


def set_ship_last_time(chat_id: int, last_time: int):
    _ship_db_init()
    with db_session(SHIP_DB) as con:
        now_ts = time.time()
        has_updated_at = _ship_state_has_updated_at(con)

        if has_updated_at:
            con.execute(
                """
                INSERT INTO ship_state (chat_id, last_time, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  last_time=excluded.last_time,
                  updated_at=excluded.updated_at
                """,
                (int(chat_id), int(last_time), float(now_ts)),
            )
        else:
            con.execute(
                """
                INSERT INTO ship_state (chat_id, last_time)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  last_time=excluded.last_time
                """,
                (int(chat_id), int(last_time)),
            )

        con.commit()


def get_users_pool(chat_id: int, limit: int = 150) -> list[dict]:
    """Ambil kandidat ship, diurutkan dari member yang paling baru aktif."""
    _ship_db_init()
    limit = max(10, int(limit))
    with db_session(SHIP_DB) as con:
        cur = con.execute(
            """
            SELECT user_id, name
            FROM users
            WHERE chat_id=?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (int(chat_id), limit),
        )
        rows = cur.fetchall()
        return [
            {"id": int(uid), "name": str(name) or "Unknown"}
            for (uid, name) in rows
            if uid is not None
        ]


def touch_user(chat_id: int, user_id: int) -> None:
    """Perbarui updated_at tanpa mengubah nama (nandai user aktif)."""
    cid = int(chat_id)
    if cid > 0:
        return
    _ship_db_init()
    with db_session(SHIP_DB) as con:
        con.execute(
            "UPDATE users SET updated_at=? WHERE chat_id=? AND user_id=?",
            (float(time.time()), cid, int(user_id)),
        )
        con.commit()
