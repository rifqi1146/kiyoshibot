import time
import asyncio
import logging

from database.db import db_session

log = logging.getLogger(__name__)

CACA_DB_PATH = "data/caca.sqlite3"
_MODE_CACHE: dict[int, str] = {}
_MODE_CACHE_MAX = int(__import__("os").getenv("CACA_MODE_CACHE_MAX", "5000"))


def _mode_cache_set(user_id: int, mode: str):
    """Bounded insert into the in-memory Caca mode cache (LRU-ish eviction)."""
    _MODE_CACHE[int(user_id)] = str(mode)
    if len(_MODE_CACHE) > _MODE_CACHE_MAX:
        # Drop the oldest 10% (dicts preserve insertion order) to avoid a full clear.
        overflow = len(_MODE_CACHE) - _MODE_CACHE_MAX
        for k in list(_MODE_CACHE.keys())[: max(1, overflow + _MODE_CACHE_MAX // 10)]:
            _MODE_CACHE.pop(k, None)

def _caca_db_init():
    with db_session(CACA_DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_mode (
                user_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_groups (
                chat_id INTEGER PRIMARY KEY,
                added_at REAL NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_approved (
                user_id INTEGER PRIMARY KEY,
                added_at REAL NOT NULL
            )
            """
        )
        con.commit()

def _caca_db_load_modes() -> dict[int, str]:
    with db_session(CACA_DB_PATH) as con:
        rows = con.execute("SELECT user_id, mode FROM caca_mode").fetchall()
    out = {}
    for uid, mode in rows:
        try:
            out[int(uid)] = str(mode)
        except (TypeError, ValueError) as e:
            log.warning("Invalid Caca mode row skipped | user_id=%r mode=%r err=%r", uid, mode, e)
    return out

def _rollback(con, label: str):
    try:
        con.execute("ROLLBACK")
    except Exception as e:
        log.warning("%s rollback failed | err=%r", label, e)

def _caca_db_save_modes(modes: dict[int, str]):
    with db_session(CACA_DB_PATH) as con:
        try:
            con.execute("BEGIN")
            keys = [int(uid) for uid in modes.keys()]
            if not keys:
                con.execute("DELETE FROM caca_mode")
            else:
                placeholders = ",".join(["?"] * len(keys))
                con.execute(f"DELETE FROM caca_mode WHERE user_id NOT IN ({placeholders})", tuple(keys))
            now = time.time()
            con.executemany(
                """
                INSERT INTO caca_mode (user_id, mode, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  mode=excluded.mode,
                  updated_at=excluded.updated_at
                """,
                [(int(uid), str(modes[uid]), now) for uid in keys],
            )
            con.execute("COMMIT")
        except Exception:
            _rollback(con, "Caca save modes")
            raise

def _caca_db_upsert_mode(user_id: int, mode: str):
    with db_session(CACA_DB_PATH) as con:
        now = time.time()
        con.execute(
            """
            INSERT INTO caca_mode (user_id, mode, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              mode=excluded.mode,
              updated_at=excluded.updated_at
            """,
            (int(user_id), str(mode), now),
        )
        con.commit()

def _caca_db_remove_mode(user_id: int):
    with db_session(CACA_DB_PATH) as con:
        con.execute("DELETE FROM caca_mode WHERE user_id = ?", (int(user_id),))
        con.commit()

def _caca_db_load_groups() -> set[int]:
    with db_session(CACA_DB_PATH) as con:
        rows = con.execute("SELECT chat_id FROM caca_groups").fetchall()
    groups = set()
    for row in rows:
        try:
            if row and row[0] is not None:
                groups.add(int(row[0]))
        except (TypeError, ValueError) as e:
            log.warning("Invalid Caca group row skipped | row=%r err=%r", row, e)
    return groups

def _caca_db_save_groups(groups: set[int]):
    with db_session(CACA_DB_PATH) as con:
        try:
            con.execute("BEGIN")
            groups = {int(gid) for gid in groups}
            if not groups:
                con.execute("DELETE FROM caca_groups")
            else:
                placeholders = ",".join(["?"] * len(groups))
                con.execute(f"DELETE FROM caca_groups WHERE chat_id NOT IN ({placeholders})", tuple(groups))
            now = time.time()
            con.executemany(
                "INSERT OR IGNORE INTO caca_groups (chat_id, added_at) VALUES (?, ?)",
                [(int(gid), now) for gid in groups],
            )
            con.execute("COMMIT")
        except Exception:
            _rollback(con, "Caca save groups")
            raise

def _caca_db_add_group(chat_id: int):
    with db_session(CACA_DB_PATH) as con:
        now = time.time()
        con.execute(
            "INSERT OR IGNORE INTO caca_groups (chat_id, added_at) VALUES (?, ?)",
            (int(chat_id), now),
        )
        con.commit()

def _caca_db_remove_group(chat_id: int):
    with db_session(CACA_DB_PATH) as con:
        con.execute("DELETE FROM caca_groups WHERE chat_id = ?", (int(chat_id),))
        con.commit()

async def init():
    await asyncio.to_thread(_caca_db_init)
    await reload_modes()

async def reload_modes():
    global _MODE_CACHE
    try:
        _MODE_CACHE = await asyncio.to_thread(_caca_db_load_modes)
        log.info("Loaded Caca modes: %s users", len(_MODE_CACHE))
    except Exception as e:
        _MODE_CACHE = {}
        log.warning("Failed to load Caca modes | err=%r", e)

def get_mode(user_id: int) -> str:
    return _MODE_CACHE.get(int(user_id), "default")

async def set_mode(user_id: int, mode: str):
    user_id = int(user_id)
    mode = str(mode)
    _mode_cache_set(user_id, mode)
    await asyncio.to_thread(_caca_db_upsert_mode, user_id, mode)

async def remove_mode(user_id: int):
    user_id = int(user_id)
    _MODE_CACHE.pop(user_id, None)
    await asyncio.to_thread(_caca_db_remove_mode, user_id)

async def load_groups() -> set[int]:
    try:
        groups = await asyncio.to_thread(_caca_db_load_groups)
        return groups
    except Exception as e:
        log.warning("Failed to load Caca groups | err=%r", e)
        return set()

async def save_groups(groups: set[int]):
    await asyncio.to_thread(_caca_db_save_groups, groups)

async def add_group(chat_id: int):
    await asyncio.to_thread(_caca_db_add_group, chat_id)

async def remove_group(chat_id: int):
    await asyncio.to_thread(_caca_db_remove_group, chat_id)