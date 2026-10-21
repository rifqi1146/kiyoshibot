import os
import time
import sqlite3
from utils.config import OWNER_ID

CACA_DB_PATH = "data/caca.sqlite3"
_PREMIUM_USERS = set()

def _db():
    os.makedirs("data", exist_ok=True)
    con = sqlite3.connect(CACA_DB_PATH, timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    return con

def init_premium_db():
    global _PREMIUM_USERS
    con = _db()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS premium_users (
                user_id INTEGER PRIMARY KEY,
                added_at REAL NOT NULL
            )
        """)
        con.commit()
        cur = con.execute("SELECT user_id FROM premium_users")
        rows = cur.fetchall()
        _PREMIUM_USERS = {int(r[0]) for r in rows if r and r[0] is not None}
    finally:
        con.close()

def _table_exists() -> bool:
    con = _db()
    try:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='premium_users' LIMIT 1")
        return cur.fetchone() is not None
    finally:
        con.close()

def init_if_needed():
    if not _PREMIUM_USERS:
        init_premium_db()

def premium_add(user_id: int):
    global _PREMIUM_USERS
    uid = int(user_id)
    init_if_needed()
    if uid in OWNER_ID:
        return
    con = _db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO premium_users (user_id, added_at) VALUES (?, ?)",
            (uid, float(time.time())),
        )
        con.commit()
        _PREMIUM_USERS.add(uid)
    finally:
        con.close()

def premium_del(user_id: int):
    global _PREMIUM_USERS
    uid = int(user_id)
    init_if_needed()
    if uid in OWNER_ID:
        return
    con = _db()
    try:
        con.execute("DELETE FROM premium_users WHERE user_id=?", (uid,))
        con.commit()
        _PREMIUM_USERS.discard(uid)
    finally:
        con.close()

def premium_list() -> list[int]:
    init_if_needed()
    con = _db()
    try:
        cur = con.execute("SELECT user_id FROM premium_users ORDER BY added_at DESC")
        rows = cur.fetchall()
        db_ids = [int(r[0]) for r in rows if r and r[0] is not None and int(r[0]) not in OWNER_ID]
        return list(dict.fromkeys([*OWNER_ID, *db_ids]))
    finally:
        con.close()

def premium_load_set() -> set[int]:
    init_if_needed()
    con = _db()
    try:
        cur = con.execute("SELECT user_id FROM premium_users")
        rows = cur.fetchall()
        db_ids = {int(r[0]) for r in rows if r and r[0] is not None}
        return db_ids | {int(x) for x in OWNER_ID}
    finally:
        con.close()

def is_premium(user_id: int, cache: set[int] | None = None) -> bool:
    uid = int(user_id)
    if uid in OWNER_ID:
        return True
    init_if_needed()
    active_cache = cache if (cache is not None and len(cache) > 0) else _PREMIUM_USERS
    return uid in active_cache

def init():
    init_premium_db()

def add(uid: int):
    premium_add(uid)

def remove(uid: int):
    premium_del(uid)

def list_users() -> list[int]:
    return premium_list()

def check(uid: int) -> bool:
    init_if_needed()
    return is_premium(uid, _PREMIUM_USERS)

def cache_set() -> set[int]:
    init_if_needed()
    return set(_PREMIUM_USERS) | {int(x) for x in OWNER_ID}

def load_set() -> set[int]:
    return premium_load_set()