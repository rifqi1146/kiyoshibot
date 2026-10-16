import sqlite3
import os
import logging
import threading
from contextlib import contextmanager
from typing import Generator

log = logging.getLogger(__name__)


_local = threading.local()


def _get_thread_pool() -> dict[str, sqlite3.Connection]:
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = {}
        _local.pool = pool
    return pool


class _PooledConnection(sqlite3.Connection):

    def close(self):
        try:
            if self.in_transaction:
                self.rollback()
        except sqlite3.Error:
            pass
        try:
            self.row_factory = None
        except Exception:
            pass


def _open_pooled(db_path: str) -> sqlite3.Connection:
    pool = _get_thread_pool()
    con = pool.get(db_path)
    if con is not None:
        try:
            con.execute("SELECT 1")
            return con
        except sqlite3.Error:
            pool.pop(db_path, None)
            try:
                sqlite3.Connection.close(con)
            except sqlite3.Error:
                pass
    con = sqlite3.connect(db_path, timeout=30.0, factory=_PooledConnection)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=30000;")
    except sqlite3.Error as e:
        log.warning(f"Failed to set PRAGMA for {db_path}: {e}")
    pool[db_path] = con
    return con


def get_connection(db_path: str) -> sqlite3.Connection:
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return _open_pooled(db_path)


@contextmanager
def db_session(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    con = get_connection(db_path)
    try:
        yield con
    finally:
        con.close()
