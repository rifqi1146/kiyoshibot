"""Penyimpanan status keanggotaan channel support (SQLite).

Status di-update secara realtime oleh ``ChatMemberHandler`` sehingga saat
user keluar dari channel, barisnya langsung ditandai ``left`` dan akses bot
dicabut pada detik itu juga.
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
    """Ambil (status, is_member) tersimpan, atau ``(None, None)`` bila belum ada."""
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


def get_member_status_many(user_ids: list[int]) -> dict[int, bool]:
    """Ambil peta ``{user_id: is_member}`` untuk banyak user sekaligus."""
    out: dict[int, bool] = {}
    try:
        uids = []
        for u in user_ids or []:
            try:
                uids.append(int(u))
            except (TypeError, ValueError):
                continue
        if not uids:
            return out
        with db_session(JOIN_STATUS_DB) as con:
            _init(con)
            placeholders = ",".join("?" * len(uids))
            cur = con.execute(
                f"SELECT user_id, is_member FROM join_status WHERE user_id IN ({placeholders})",
                uids,
            )
            for uid, is_member in cur.fetchall():
                out[int(uid)] = bool(is_member)
    except Exception as e:
        log.warning("[JOIN DB] Failed to read many statuses | err=%r", e)
    return out


def forget_member(user_id: int):
    """Hapus cache user sehingga status akan diverifikasi ulang ke Telegram."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    try:
        with db_session(JOIN_STATUS_DB) as con:
            _init(con)
            con.execute("DELETE FROM join_status WHERE user_id=?", (uid,))
            con.commit()
        log.info("[JOIN DB] cache forgotten | user_id=%s", uid)
    except Exception as e:
        log.warning("[JOIN DB] Failed to forget | user_id=%s err=%r", user_id, e)


def purge_non_members(older_than_days: int = 30) -> int:
    """Hapus baris non-member yang sudah lama (housekeeping ringan)."""
    try:
        cutoff = time.time() - (older_than_days * 86400)
        with db_session(JOIN_STATUS_DB) as con:
            _init(con)
            cur = con.execute(
                "DELETE FROM join_status WHERE is_member=0 AND updated_at < ?",
                (cutoff,),
            )
            removed = cur.rowcount or 0
            con.commit()
        if removed:
            log.info("[JOIN DB] purged %s stale non-member row(s)", removed)
        return int(removed)
    except Exception as e:
        log.warning("[JOIN DB] purge failed | err=%r", e)
        return 0


def count_members() -> int:
    try:
        with db_session(JOIN_STATUS_DB) as con:
            _init(con)
            row = con.execute("SELECT COUNT(*) FROM join_status WHERE is_member=1").fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        log.debug("[JOIN DB] count failed | err=%r", e)
        return 0
