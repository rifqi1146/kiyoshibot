import os
import time
import sqlite3

SHARE_DB = "data/shared_media.sqlite3"

def _connect():
    os.makedirs("data", exist_ok=True)
    con = sqlite3.connect(SHARE_DB)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_share_db():
    con = _connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS shared_media (
                share_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        con.commit()
    finally:
        con.close()

def save_share(share_id: str, chat_id: int, message_id: int):
    init_share_db()
    con = _connect()
    try:
        con.execute("""
            INSERT OR REPLACE INTO shared_media (share_id, chat_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (share_id, chat_id, message_id, time.time()))
        con.commit()
    finally:
        con.close()

def get_share(share_id: str):
    init_share_db()
    con = _connect()
    try:
        row = con.execute("SELECT chat_id, message_id FROM shared_media WHERE share_id=?", (share_id,)).fetchone()
        return row
    finally:
        con.close()
