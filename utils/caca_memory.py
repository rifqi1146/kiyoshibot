import os
import time
import json
import sqlite3
import asyncio

MEMORY_EXPIRE = 60 * 60 * 24
CACA_DB_PATH = "data/caca_memory.sqlite3"
CACA_MAX_TURNS = int(os.getenv("CACA_MAX_TURNS", "8"))


def _caca_db_init():
    os.makedirs("data", exist_ok=True)
    legacy_path = "data/meta_memory.sqlite3"
    if os.path.exists(legacy_path):
        try:
            os.remove(legacy_path)
            for suf in ("-wal", "-shm"):
                p = legacy_path + suf
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass

    con = sqlite3.connect(CACA_DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_memory (
                user_id INTEGER PRIMARY KEY,
                history_json TEXT NOT NULL,
                last_used REAL NOT NULL,
                last_message_id INTEGER
            )
            """
        )
        con.commit()
    finally:
        con.close()


def _caca_db_get(user_id: int):
    con = sqlite3.connect(CACA_DB_PATH)
    try:
        cur = con.execute(
            "SELECT history_json, last_used, last_message_id FROM caca_memory WHERE user_id=?",
            (int(user_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        history = json.loads(row[0]) if row[0] else []
        last_used = float(row[1])
        last_message_id = int(row[2]) if row[2] is not None else None
        if not isinstance(history, list):
            history = []
        return history, last_used, last_message_id
    finally:
        con.close()


def _caca_db_set(user_id: int, history: list, last_message_id: int | None):
    if CACA_MAX_TURNS and CACA_MAX_TURNS > 0:
        max_msgs = CACA_MAX_TURNS * 2
        if len(history) > max_msgs:
            history = history[-max_msgs:]

    con = sqlite3.connect(CACA_DB_PATH)
    try:
        con.execute(
            """
            INSERT INTO caca_memory (user_id, history_json, last_used, last_message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              history_json=excluded.history_json,
              last_used=excluded.last_used,
              last_message_id=excluded.last_message_id
            """,
            (int(user_id), json.dumps(history, ensure_ascii=False), time.time(), last_message_id),
        )
        con.commit()
    finally:
        con.close()


def _caca_db_set_last_message_id(user_id: int, last_message_id: int | None):
    con = sqlite3.connect(CACA_DB_PATH)
    try:
        con.execute(
            """
            INSERT INTO caca_memory (user_id, history_json, last_used, last_message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              last_used=excluded.last_used,
              last_message_id=excluded.last_message_id
            """,
            (int(user_id), "[]", time.time(), last_message_id),
        )
        con.commit()
    finally:
        con.close()


def _caca_db_clear_last_message_id(user_id: int):
    con = sqlite3.connect(CACA_DB_PATH)
    try:
        con.execute(
            "UPDATE caca_memory SET last_message_id=NULL, last_used=? WHERE user_id=?",
            (time.time(), int(user_id)),
        )
        con.commit()
    finally:
        con.close()


def _caca_db_clear(user_id: int):
    con = sqlite3.connect(CACA_DB_PATH)
    try:
        con.execute("DELETE FROM caca_memory WHERE user_id=?", (int(user_id),))
        con.commit()
    finally:
        con.close()


def _caca_db_cleanup(expire_seconds: int):
    cutoff = time.time() - float(expire_seconds)
    con = sqlite3.connect(CACA_DB_PATH)
    try:
        con.execute("DELETE FROM caca_memory WHERE last_used < ?", (cutoff,))
        con.commit()
    finally:
        con.close()


def _caca_db_has_last_message_id(message_id: int) -> bool:
    con = sqlite3.connect(CACA_DB_PATH)
    try:
        cur = con.execute(
            "SELECT 1 FROM caca_memory WHERE last_message_id=? LIMIT 1",
            (int(message_id),),
        )
        return cur.fetchone() is not None
    finally:
        con.close()


async def init():
    await asyncio.to_thread(_caca_db_init)


async def get_history(user_id: int) -> list:
    res = await asyncio.to_thread(_caca_db_get, user_id)
    if not res:
        return []
    history, _, _ = res
    return history


async def set_history(user_id: int, history: list, last_message_id: int | None = None):
    await asyncio.to_thread(_caca_db_set, user_id, history, last_message_id)


async def set_last_message_id(user_id: int, last_message_id: int | None):
    await asyncio.to_thread(_caca_db_set_last_message_id, user_id, last_message_id)


async def get_last_message_id(user_id: int) -> int | None:
    res = await asyncio.to_thread(_caca_db_get, user_id)
    if not res:
        return None
    _, _, last_message_id = res
    return last_message_id


async def clear_last_message_id(user_id: int):
    await asyncio.to_thread(_caca_db_clear_last_message_id, user_id)


async def clear(user_id: int):
    await asyncio.to_thread(_caca_db_clear, user_id)


async def cleanup():
    await asyncio.to_thread(_caca_db_cleanup, MEMORY_EXPIRE)


async def has_last_message_id(message_id: int) -> bool:
    return await asyncio.to_thread(_caca_db_has_last_message_id, message_id)
