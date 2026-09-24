import sqlite3
import os
import logging
from contextlib import contextmanager
from typing import Generator

log = logging.getLogger(__name__)

def get_connection(db_path: str) -> sqlite3.Connection:
    """
    Creates a connection to the SQLite database with standard performance settings.
    Ensures the directory for db_path exists.
    """
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    con = sqlite3.connect(db_path, timeout=30.0)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=30000;")
    except sqlite3.Error as e:
        log.warning(f"Failed to set PRAGMA for {db_path}: {e}")
    return con

@contextmanager
def db_session(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager for database connections.
    Closes the connection automatically.
    """
    con = get_connection(db_path)
    try:
        yield con
    finally:
        con.close()
