"""Penyimpanan status keanggotaan channel support (SQLite).

Status di-update secara realtime oleh ``ChatMemberHandler`` sehingga saat
user keluar dari channel, barisnya langsung ditandai ``left`` dan akses bot
dicabut pada detik itu juga.

Baris tidak pernah dihapus: baca lewat PRIMARY KEY index O(log n), jadi
menyimpan seluruh riwayat user tidak berpengaruh ke performa.
"""

import time
import logging
from database.db import db_session

log = logging.getLogger(__name__)

JOIN_STATUS_DB = "data/join_status.sqlite3"

MEMBER_STATUSES = ("member", "administrator", "creator")


def _init(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS join_status (
            user_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            is_member INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_join_status_member ON join_status(is_member)")


def set_member_status(user_id: int, status: str) -> bool:
    """Simpan status keanggotaan terbaru user.

    Return ``True`` jika user dianggap member channel.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    status = (str(status or "").strip().lower()) or "unknown"
    is_member = 1 if status in MEMBER_STATUSES else 0
    try:
        with db_session(JOIN_STATUS_DB) as con:
            _init(con)
            con.execute(
                """
                INSERT INTO join_status (user_id, status, is_member, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    status=excluded.status,
                    is_member=excluded.is_member,
                    updated_at=excluded.updated_at
                """,
                (uid, status, is_member, time.time()),
            )
            con.commit()
        log.info("[JOIN DB] status recorded | user_id=%s status=%s member=%s", uid, status, bool(is_member))
        return bool(is_member)
    except Exception as e:
        log.warning("[JOIN DB] Failed to set status | user_id=%s err=%r", user_id, e)
        return bool(is_member)


def get_member_status(user_id: int):
    """Ambil ``(status, is_member)`` tersimpan, atau ``(None, None)`` bila belum ada."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None, None
    try:
        with db_session(JOIN_STATUS_DB) as con:
            _init(con)
            row = con.execute(
                "SELECT status, is_member FROM join_status WHERE user_id=?",
                (uid,),
            ).fetchone()
        if not row:
            return None, None
        return str(row[0]), bool(row[1])
    except Exception as e:
        log.warning("[JOIN DB] Failed to read status | user_id=%s err=%r", user_id, e)
        return None, None


def get_updated_at(user_id: int):
    """Ambil timestamp update terakhir user, atau ``None`` bila tidak ada."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    try:
        with db_session(JOIN_STATUS_DB) as con:
            _init(con)
            row = con.execute(
                "SELECT updated_at FROM join_status WHERE user_id=?",
                (uid,),
            ).fetchone()
        return float(row[0]) if row else None
    except Exception as e:
        log.warning("[JOIN DB] Failed to read updated_at | user_id=%s err=%r", user_id, e)
        return None
