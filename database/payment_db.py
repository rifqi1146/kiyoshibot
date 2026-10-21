
import os
import sqlite3
import time

DB_PATH = "data/payments.sqlite3"


def _db():
    os.makedirs("data", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    return con


def init_db():
    con = _db()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                short_id            TEXT PRIMARY KEY,
                qris_id             TEXT NOT NULL,
                trx_id              TEXT NOT NULL,
                user_id             INTEGER NOT NULL,
                chat_id             INTEGER NOT NULL,
                message_id          INTEGER,
                amount              INTEGER NOT NULL,
                fee_amount          INTEGER NOT NULL DEFAULT 0,
                net_amount          INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'PENDING',
                created_at          REAL NOT NULL,
                provider_expires_at REAL,
                deadline            REAL NOT NULL,
                paid_at             REAL,
                qr_url              TEXT
            )
            """
        )
        if con.execute(
            "SELECT 1 FROM pragma_table_info('payments') WHERE name = 'qr_url'"
        ).fetchone() is None:
            con.execute("ALTER TABLE payments ADD COLUMN qr_url TEXT")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_user_status "
            "ON payments(user_id, status)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_qris ON payments(qris_id)"
        )
        con.commit()
    finally:
        con.close()


def save_invoice(
    *,
    short_id: str,
    qris_id: str,
    trx_id: str,
    user_id: int,
    chat_id: int,
    amount: int,
    fee_amount: int,
    net_amount: int,
    created_at: float,
    provider_expires_at: float | None,
    deadline: float,
    qr_url: str | None = None,
) -> dict:
    con = _db()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO payments (
                short_id, qris_id, trx_id, user_id, chat_id, message_id,
                amount, fee_amount, net_amount, status, created_at,
                provider_expires_at, deadline, paid_at, qr_url
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'PENDING', ?, ?, ?, NULL, ?)
            """,
            (
                short_id, qris_id, trx_id, int(user_id), int(chat_id),
                int(amount), int(fee_amount), int(net_amount),
                float(created_at),
                provider_expires_at,
                float(deadline),
                qr_url,
            ),
        )
        con.commit()
    finally:
        con.close()
    return get_by_short(short_id) or {}


def set_message_id(short_id: str, message_id: int | None):
    con = _db()
    try:
        con.execute(
            "UPDATE payments SET message_id=? WHERE short_id=?",
            (message_id, short_id),
        )
        con.commit()
    finally:
        con.close()


def get_by_short(short_id: str) -> dict | None:
    con = _db()
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM payments WHERE short_id=?", (short_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def get_by_qris(qris_id: str) -> dict | None:
    con = _db()
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM payments WHERE qris_id=? ORDER BY created_at DESC LIMIT 1",
            (qris_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def active_for_user(user_id: int) -> dict | None:
    con = _db()
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM payments WHERE user_id=? AND status='PENDING' "
            "AND deadline>? ORDER BY created_at DESC LIMIT 1",
            (int(user_id), time.time()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def set_status(short_id: str, status: str):
    con = _db()
    try:
        con.execute(
            "UPDATE payments SET status=? WHERE short_id=?", (status, short_id)
        )
        con.commit()
    finally:
        con.close()


def mark_paid(short_id: str, paid_at: float | None = None):
    con = _db()
    try:
        con.execute(
            "UPDATE payments SET status='PAID', paid_at=? WHERE short_id=?",
            (float(paid_at or time.time()), short_id),
        )
        con.commit()
    finally:
        con.close()


def is_paid(short_id: str) -> bool:
    inv = get_by_short(short_id)
    return bool(inv and inv.get("status") == "PAID")


try:
    init_db()
except Exception:
    pass
