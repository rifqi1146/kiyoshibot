"""Caca Credit Billing & Ledger Database (SQLite).

Architecture & Invariants:
1. Multi-lot Credit Ledger (FEFO - First Expiring, First Out).
   - Every top-up creates a distinct lot with its own `expires_at`.
   - A new top-up never extends previous lots.
   - Expired lots (expires_at <= now) are strictly excluded from available balance.
2. Safe Concurrent Usage (Atomic Reservation & Settlement).
   - Pre-reserve credits before calling LLM.
   - Settle actual usage on response; release remainder or on error.
3. Axiom QRIS Invoices.
   - Stores user invoices with preset or custom nominal & custom duration.
   - Enforces unique payment fulfillment (idempotent mark_paid).
4. Settings (`caca_settings`):
   - Stores global settings such as `freecaca` mode (1 = enabled, 0 = disabled).
   - Self-healing in-memory cache against importlib.reload().
5. WAL mode + NORMAL synchronous + 30000ms busy_timeout via `database.db.db_session`.
"""

import os
import time
import uuid
import logging
from typing import Any

from database.db import db_session

log = logging.getLogger(__name__)

DB_PATH = "data/caca_billing.sqlite3"

# Default rates (can be overridden via environment variables)
# Target 300 chat / Rp 3.000:
#   Rata-rata 1 chat ~900 token input + 50 output = 1.000 credit (pada 1 cr/t input, 2 cr/t output)
#   Rp 3.000 -> 300.000 credit -> 300.000 / 1.000 = 300 chat.
#   1 Credit = Rp 0.01  (Rp 1 = 100 Credits)
RATE_CREDIT_PER_RUPIAH = int(os.getenv("CACA_CREDIT_PER_RUPIAH", "100"))
RATE_INPUT_TOKEN = int(os.getenv("CACA_CREDIT_PER_INPUT_TOKEN", "1"))    # 1 credit / input token
RATE_OUTPUT_TOKEN = int(os.getenv("CACA_CREDIT_PER_OUTPUT_TOKEN", "2"))  # 2 credits / output token

# QRIS payment gateway fee passed through to user so the bot receives the
# exact target amount. 2% = 200 bps.
GATEWAY_FEE_BPS = int(os.getenv("AXIOM_FEE_BPS", "200"))


def calculate_gross_amount(target_net_rp: int, fee_bps: int = GATEWAY_FEE_BPS) -> tuple[int, int]:
    """Hitung total bayar (gross) agar setelah dipotong fee, penerimaan bersih == target_net_rp.

    Formula: gross = ceil(target_net * 10000 / (10000 - fee_bps))
    Fee dihitung dari gross (Axiom mengambil fee% dari total invoice QRIS).
    Mengembalikan (gross_amount, fee_amount).
    """
    target = int(target_net_rp)
    if target <= 0:
        return 0, 0
    bps = max(0, min(9900, int(fee_bps)))
    denom = 10000 - bps
    gross = (target * 10000 + denom - 1) // denom
    fee = gross - target
    return gross, fee

# Presets configuration: price_rp -> (credit_base, credit_bonus, duration_days)
# Skala 300 chat per Rp 3.000 (Rp 1 = 100 credit):
PRESET_PACKAGES: dict[int, dict[str, int]] = {
    3000:  {"price": 3000,  "base": 300000,  "bonus": 0,      "total": 300000,  "days": 7},
    5000:  {"price": 5000,  "base": 500000,  "bonus": 10000,  "total": 510000,  "days": 10},
    10000: {"price": 10000, "base": 1000000, "bonus": 50000,  "total": 1050000, "days": 14},
    20000: {"price": 20000, "base": 2000000, "bonus": 120000, "total": 2120000, "days": 21},
    30000: {"price": 30000, "base": 3000000, "bonus": 300000, "total": 3300000, "days": 30},
}

CUSTOM_MIN_AMOUNT = 3000
CUSTOM_MAX_AMOUNT = 200000
ALLOWED_CUSTOM_DAYS = (7, 14, 21, 30)

# Chat gate thresholds
MIN_CREDITS_TO_CHAT = int(os.getenv("CACA_MIN_CREDITS_TO_CHAT", "50"))
RESERVE_AMOUNT = int(os.getenv("CACA_RESERVE_AMOUNT", "2000"))

# In-memory cached freecaca state
_FREECACA_CACHE: bool | None = None


def init_db():
    """Create all billing tables and indexes atomically."""
    with db_session(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_invoices (
                short_id            TEXT PRIMARY KEY,
                qris_id             TEXT NOT NULL,
                trx_id              TEXT NOT NULL,
                user_id             INTEGER NOT NULL,
                chat_id             INTEGER NOT NULL,
                message_id          INTEGER,
                amount              INTEGER NOT NULL,
                base_amount         INTEGER NOT NULL DEFAULT 0,
                gateway_fee         INTEGER NOT NULL DEFAULT 0,
                fee_bps             INTEGER NOT NULL DEFAULT 0,
                credit_base         INTEGER NOT NULL,
                credit_bonus        INTEGER NOT NULL DEFAULT 0,
                credit_total        INTEGER NOT NULL,
                duration_days       INTEGER NOT NULL,
                is_custom           INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'PENDING',
                created_at          REAL NOT NULL,
                deadline            REAL NOT NULL,
                paid_at             REAL
            )
            """
        )
        # Safe migration for pre-existing installs: add new columns if missing.
        for col, ddl in (
            ("base_amount", "ALTER TABLE caca_invoices ADD COLUMN base_amount INTEGER NOT NULL DEFAULT 0"),
            ("gateway_fee", "ALTER TABLE caca_invoices ADD COLUMN gateway_fee INTEGER NOT NULL DEFAULT 0"),
            ("fee_bps", "ALTER TABLE caca_invoices ADD COLUMN fee_bps INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                con.execute(ddl)
            except Exception:
                pass
        con.execute(
            "UPDATE caca_invoices SET base_amount = amount WHERE base_amount = 0"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_credit_lots (
                lot_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL,
                invoice_short_id    TEXT,
                credit_base         INTEGER NOT NULL,
                credit_bonus        INTEGER NOT NULL DEFAULT 0,
                credit_total        INTEGER NOT NULL,
                credit_used         INTEGER NOT NULL DEFAULT 0,
                credit_remaining    INTEGER NOT NULL,
                issued_at           REAL NOT NULL,
                expires_at          REAL NOT NULL,
                duration_days       INTEGER NOT NULL,
                status              TEXT NOT NULL DEFAULT 'ACTIVE'
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_reservations (
                reservation_id      TEXT PRIMARY KEY,
                user_id             INTEGER NOT NULL,
                reserved_credits    INTEGER NOT NULL,
                status              TEXT NOT NULL DEFAULT 'PENDING',
                created_at          REAL NOT NULL,
                expires_at          REAL NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_usage_logs (
                log_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL,
                chat_id             INTEGER NOT NULL,
                reservation_id      TEXT,
                prompt_tokens       INTEGER NOT NULL DEFAULT 0,
                completion_tokens   INTEGER NOT NULL DEFAULT 0,
                total_tokens        INTEGER NOT NULL DEFAULT 0,
                cost_credits        INTEGER NOT NULL DEFAULT 0,
                is_billed           INTEGER NOT NULL DEFAULT 1,
                model               TEXT NOT NULL,
                created_at          REAL NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_audit_logs (
                audit_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id            INTEGER NOT NULL,
                target_user_id      INTEGER NOT NULL,
                action              TEXT NOT NULL,
                amount_credits      INTEGER NOT NULL,
                duration_days       INTEGER,
                reason              TEXT NOT NULL,
                created_at          REAL NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS caca_settings (
                key                 TEXT PRIMARY KEY,
                value               TEXT NOT NULL,
                updated_at          REAL NOT NULL
            )
            """
        )

        # Indices
        con.execute("CREATE INDEX IF NOT EXISTS idx_caca_lots_user ON caca_credit_lots(user_id, status, expires_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_caca_invoices_user ON caca_invoices(user_id, status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_caca_reservations_user ON caca_reservations(user_id, status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_caca_usage_user ON caca_usage_logs(user_id, created_at)")
        con.commit()


# ── Global Settings & /freecaca ──────────────────────────────────────────────

def is_freecaca_enabled() -> bool:
    """Return True if free Caca access is active for everyone. Self-healing DB check."""
    global _FREECACA_CACHE
    if _FREECACA_CACHE is not None:
        return _FREECACA_CACHE
    try:
        with db_session(DB_PATH) as con:
            row = con.execute("SELECT value FROM caca_settings WHERE key = 'freecaca' LIMIT 1").fetchone()
            val = (row[0] == "1") if row else False
            _FREECACA_CACHE = val
            return val
    except Exception as e:
        log.warning("Failed to read freecaca setting | err=%r", e)
        return False


def set_freecaca_enabled(enabled: bool) -> None:
    global _FREECACA_CACHE
    _FREECACA_CACHE = bool(enabled)
    with db_session(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO caca_settings (key, value, updated_at)
            VALUES ('freecaca', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value=excluded.value,
              updated_at=excluded.updated_at
            """,
            ("1" if enabled else "0", time.time()),
        )
        con.commit()


# ── Package Calculation ──────────────────────────────────────────────────────

def calculate_preset_package(amount: int) -> dict[str, Any] | None:
    """Return preset info or None if amount is not an exact preset."""
    pkg = PRESET_PACKAGES.get(int(amount))
    if not pkg:
        return None
    return dict(pkg)


def calculate_custom_package(amount: int, duration_days: int) -> dict[str, Any]:
    """Validate & calculate custom top-up package."""
    amount = int(amount)
    duration_days = int(duration_days)
    if not (CUSTOM_MIN_AMOUNT <= amount <= CUSTOM_MAX_AMOUNT):
        raise ValueError(f"Nominal custom harus antara Rp {CUSTOM_MIN_AMOUNT:,} dan Rp {CUSTOM_MAX_AMOUNT:,}.")
    if duration_days not in ALLOWED_CUSTOM_DAYS:
        raise ValueError(f"Durasi hari harus salah satu dari: {ALLOWED_CUSTOM_DAYS}.")

    base_credit = amount * RATE_CREDIT_PER_RUPIAH
    bonus_credit = 0  # Custom package never receives preset bonus
    total_credit = base_credit

    return {
        "price": amount,
        "base": base_credit,
        "bonus": bonus_credit,
        "total": total_credit,
        "days": duration_days,
        "is_custom": 1,
    }


# ── Invoices ─────────────────────────────────────────────────────────────────

def save_invoice(
    *,
    short_id: str,
    qris_id: str,
    trx_id: str,
    user_id: int,
    chat_id: int,
    amount: int,
    credit_base: int,
    credit_bonus: int,
    credit_total: int,
    duration_days: int,
    is_custom: bool,
    created_at: float,
    deadline: float,
    base_amount: int | None = None,
    gateway_fee: int = 0,
    fee_bps: int = 0,
) -> dict[str, Any]:
    """`amount` = total yang dibayar user (gross, sudah termasuk fee gateway).

    `base_amount` = harga paket/nominal yang owner tetapkan (net yang diterima).
    """
    base_target = int(base_amount) if base_amount is not None else int(amount)
    with db_session(DB_PATH) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO caca_invoices (
                short_id, qris_id, trx_id, user_id, chat_id, message_id,
                amount, base_amount, gateway_fee, fee_bps,
                credit_base, credit_bonus, credit_total, duration_days,
                is_custom, status, created_at, deadline, paid_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, NULL)
            """,
            (
                short_id, qris_id, trx_id, int(user_id), int(chat_id),
                int(amount), base_target, int(gateway_fee), int(fee_bps),
                int(credit_base), int(credit_bonus), int(credit_total),
                int(duration_days), 1 if is_custom else 0,
                float(created_at), float(deadline),
            ),
        )
        con.commit()
    return get_invoice(short_id) or {}


def get_invoice(short_id: str) -> dict[str, Any] | None:
    with db_session(DB_PATH) as con:
        con.row_factory = __import__("sqlite3").Row
        row = con.execute("SELECT * FROM caca_invoices WHERE short_id = ?", (short_id,)).fetchone()
        return dict(row) if row else None


def set_invoice_message_id(short_id: str, message_id: int | None):
    with db_session(DB_PATH) as con:
        con.execute("UPDATE caca_invoices SET message_id = ? WHERE short_id = ?", (message_id, short_id))
        con.commit()


def active_invoice_for_user(user_id: int) -> dict[str, Any] | None:
    """Return newest PENDING invoice still inside its deadline."""
    with db_session(DB_PATH) as con:
        con.row_factory = __import__("sqlite3").Row
        row = con.execute(
            """
            SELECT * FROM caca_invoices
            WHERE user_id = ? AND status = 'PENDING' AND deadline > ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (int(user_id), time.time()),
        ).fetchone()
        return dict(row) if row else None


def set_invoice_status(short_id: str, status: str):
    with db_session(DB_PATH) as con:
        con.execute("UPDATE caca_invoices SET status = ? WHERE short_id = ?", (status, short_id))
        con.commit()


def fulfill_payment(short_id: str, paid_at: float | None = None) -> bool:
    """Idempotently fulfill invoice: mark PAID and create FEFO credit lot.

    Returns True if newly fulfilled, False if already PAID or not found.
    """
    now = float(paid_at or time.time())
    with db_session(DB_PATH) as con:
        con.row_factory = __import__("sqlite3").Row
        inv = con.execute("SELECT * FROM caca_invoices WHERE short_id = ?", (short_id,)).fetchone()
        if not inv or inv["status"] == "PAID":
            return False

        duration_sec = int(inv["duration_days"]) * 86400
        expires_at = now + duration_sec

        try:
            con.execute("BEGIN IMMEDIATE")
            # Re-check status inside transaction
            check_inv = con.execute("SELECT status FROM caca_invoices WHERE short_id = ?", (short_id,)).fetchone()
            if not check_inv or check_inv[0] == "PAID":
                con.execute("ROLLBACK")
                return False

            con.execute(
                "UPDATE caca_invoices SET status = 'PAID', paid_at = ? WHERE short_id = ?",
                (now, short_id),
            )
            con.execute(
                """
                INSERT INTO caca_credit_lots (
                    user_id, invoice_short_id, credit_base, credit_bonus,
                    credit_total, credit_used, credit_remaining,
                    issued_at, expires_at, duration_days, status
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 'ACTIVE')
                """,
                (
                    int(inv["user_id"]), short_id, int(inv["credit_base"]),
                    int(inv["credit_bonus"]), int(inv["credit_total"]),
                    int(inv["credit_total"]), now, expires_at, int(inv["duration_days"]),
                ),
            )
            con.execute("COMMIT")
            log.info("Fulfill Caca invoice PAID | short_id=%s user_id=%s total=%s expires_in=%sdays",
                     short_id, inv["user_id"], inv["credit_total"], inv["duration_days"])
            return True
        except Exception as e:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            log.error("Failed to fulfill payment | short_id=%s err=%r", short_id, e)
            raise


# ── Credit Balances & Multi-Lot FEFO Ledger ──────────────────────────────────

def get_user_balance(user_id: int) -> dict[str, Any]:
    """Calculate active available credits, reserved credits, and expired lots."""
    now = time.time()
    with db_session(DB_PATH) as con:
        con.row_factory = __import__("sqlite3").Row

        # Expire any lots whose time is up
        con.execute(
            "UPDATE caca_credit_lots SET status = 'EXPIRED' WHERE user_id = ? AND status = 'ACTIVE' AND expires_at <= ?",
            (int(user_id), now),
        )
        con.commit()

        # Active lots
        rows = con.execute(
            """
            SELECT * FROM caca_credit_lots
            WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ? AND credit_remaining > 0
            ORDER BY expires_at ASC
            """,
            (int(user_id), now),
        ).fetchall()

        active_lots = [dict(r) for r in rows]
        available_credits = sum(lot["credit_remaining"] for lot in active_lots)

        # Expired credits sum
        row_exp = con.execute(
            """
            SELECT SUM(credit_remaining) as exp_sum FROM caca_credit_lots
            WHERE user_id = ? AND (status = 'EXPIRED' OR expires_at <= ?)
            """,
            (int(user_id), now),
        ).fetchone()
        expired_credits = int(row_exp["exp_sum"] or 0) if row_exp else 0

        # Pending reservations (clean stale reservations > 3 minutes)
        stale_cutoff = now - 180
        con.execute(
            "UPDATE caca_reservations SET status = 'RELEASED' WHERE user_id = ? AND status = 'PENDING' AND expires_at <= ?",
            (int(user_id), stale_cutoff),
        )
        con.commit()

        row_res = con.execute(
            "SELECT SUM(reserved_credits) as res_sum FROM caca_reservations WHERE user_id = ? AND status = 'PENDING'",
            (int(user_id),),
        ).fetchone()
        reserved_credits = int(row_res["res_sum"] or 0) if row_res else 0

        effective_available = max(0, available_credits - reserved_credits)
        nearest_expiry = active_lots[0]["expires_at"] if active_lots else None

        return {
            "user_id": int(user_id),
            "available_credits": available_credits,
            "reserved_credits": reserved_credits,
            "effective_available": effective_available,
            "expired_credits": expired_credits,
            "active_lots": active_lots,
            "nearest_expiry": nearest_expiry,
        }


def admin_add_credit(
    admin_id: int,
    target_user_id: int,
    credits: int,
    duration_days: int = 7,
    reason: str = "manual add",
    *,
    extend_latest_lot: bool = False,
) -> dict:
    """Beri credit ke user dengan audit log.

    `extend_latest_lot=True` (dipakai kompensasi tarif): kalau user punya lot
    AKTIF, credit ditambahkan ke lot itu (remaining + total naik, expiry
    diperpanjang bila perlu) supaya tidak bikin lot baru yang bikin bingung.
    Kalau tidak ada lot aktif, lot baru dibuat.
    """
    now = time.time()
    credits = int(credits)
    if credits <= 0:
        raise ValueError("credits must be positive")
    duration_days = int(duration_days or 0)
    expires_at = now + (duration_days * 86400)

    with db_session(DB_PATH) as con:
        con.row_factory = __import__("sqlite3").Row
        try:
            con.execute("BEGIN IMMEDIATE")
            target_lot = None
            if extend_latest_lot:
                target_lot = con.execute(
                    """
                    SELECT lot_id, credit_remaining, expires_at FROM caca_credit_lots
                    WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ?
                    ORDER BY expires_at ASC LIMIT 1
                    """,
                    (int(target_user_id), now),
                ).fetchone()

            if target_lot:
                new_expiry = max(float(target_lot["expires_at"]), expires_at)
                con.execute(
                    """
                    UPDATE caca_credit_lots
                    SET credit_remaining = credit_remaining + ?,
                        credit_total     = credit_total + ?,
                        expires_at       = ?,
                        status           = 'ACTIVE'
                    WHERE lot_id = ?
                    """,
                    (credits, credits, new_expiry, int(target_lot["lot_id"])),
                )
                lot_id = int(target_lot["lot_id"])
            else:
                cur = con.execute(
                    """
                    INSERT INTO caca_credit_lots (
                        user_id, invoice_short_id, credit_base, credit_bonus,
                        credit_total, credit_used, credit_remaining,
                        issued_at, expires_at, duration_days, status
                    ) VALUES (?, NULL, ?, 0, ?, 0, ?, ?, ?, ?, 'ACTIVE')
                    """,
                    (int(target_user_id), credits, credits, credits, now, expires_at, duration_days),
                )
                lot_id = cur.lastrowid

            con.execute(
                """
                INSERT INTO caca_audit_logs (
                    admin_id, target_user_id, action, amount_credits, duration_days, reason, created_at
                ) VALUES (?, ?, 'ADD', ?, ?, ?, ?)
                """,
                (int(admin_id), int(target_user_id), credits, duration_days, reason, now),
            )
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise

    return {
        "lot_id": lot_id,
        "extended_existing": bool(target_lot),
        "credits_added": credits,
    }


def paid_preset_user_ids(price: int | None = None) -> list[int]:
    """User id yang punya invoice PAID (opsional difilter per harga base_amount)."""
    with db_session(DB_PATH) as con:
        if price is None:
            rows = con.execute(
                "SELECT DISTINCT user_id FROM caca_invoices WHERE status='PAID'"
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT user_id FROM caca_invoices WHERE status='PAID' AND base_amount=?",
                (int(price),),
            ).fetchall()
    return [int(r[0]) for r in rows if r and r[0] is not None]


def has_active_credit(user_id: int, min_credits: int = 1) -> bool:
    """Quick check whether user has active, unexpired credits."""
    now = time.time()
    with db_session(DB_PATH) as con:
        row = con.execute(
            """
            SELECT SUM(credit_remaining) FROM caca_credit_lots
            WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ? AND credit_remaining > 0
            """,
            (int(user_id), now),
        ).fetchone()
        tot = int(row[0] or 0) if row else 0
        return tot >= min_credits


# ── Concurrent Reservation & Settlement ──────────────────────────────────────

def reserve_credits(user_id: int, amount: int = 2000, timeout_sec: int = 120) -> str | None:
    """Reserve up to `amount` credits atomically. Returns reservation_id or None if insufficient."""
    now = time.time()
    res_id = f"res_{uuid.uuid4().hex[:12]}"
    exp_time = now + timeout_sec

    with db_session(DB_PATH) as con:
        try:
            con.execute("BEGIN IMMEDIATE")
            # Clean stale reservations
            con.execute(
                "UPDATE caca_reservations SET status = 'RELEASED' WHERE user_id = ? AND status = 'PENDING' AND expires_at <= ?",
                (int(user_id), now),
            )
            # Check available
            row_avail = con.execute(
                """
                SELECT SUM(credit_remaining) FROM caca_credit_lots
                WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ? AND credit_remaining > 0
                """,
                (int(user_id), now),
            ).fetchone()
            avail = int(row_avail[0] or 0) if row_avail else 0

            row_res = con.execute(
                "SELECT SUM(reserved_credits) FROM caca_reservations WHERE user_id = ? AND status = 'PENDING'",
                (int(user_id),),
            ).fetchone()
            curr_res = int(row_res[0] or 0) if row_res else 0

            free = avail - curr_res
            if free <= 0:
                con.execute("ROLLBACK")
                return None

            reserve_amount = min(free, int(amount))
            con.execute(
                """
                INSERT INTO caca_reservations (reservation_id, user_id, reserved_credits, status, created_at, expires_at)
                VALUES (?, ?, ?, 'PENDING', ?, ?)
                """,
                (res_id, int(user_id), reserve_amount, now, exp_time),
            )
            con.execute("COMMIT")
            return res_id
        except Exception as e:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            log.warning("Reserve credits failed | user_id=%s err=%r", user_id, e)
            return None


def release_reservation(reservation_id: str | None) -> None:
    """Release reservation on error without deducting credits."""
    if not reservation_id:
        return
    with db_session(DB_PATH) as con:
        con.execute(
            "UPDATE caca_reservations SET status = 'RELEASED' WHERE reservation_id = ? AND status = 'PENDING'",
            (reservation_id,),
        )
        con.commit()


def settle_usage(
    *,
    user_id: int,
    chat_id: int,
    reservation_id: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    is_billed: bool = True,
) -> int:
    """Settle actual token usage via FEFO deduction from credit lots.

    Returns the actual cost in credits.
    """
    now = time.time()
    prompt_tokens = max(0, int(prompt_tokens))
    completion_tokens = max(0, int(completion_tokens))
    total_tokens = prompt_tokens + completion_tokens

    cost_credits = 0
    if is_billed:
        cost_credits = (prompt_tokens * RATE_INPUT_TOKEN) + (completion_tokens * RATE_OUTPUT_TOKEN)

    with db_session(DB_PATH) as con:
        con.row_factory = __import__("sqlite3").Row
        try:
            con.execute("BEGIN IMMEDIATE")

            # 1. Deduct credits via FEFO if billed
            remaining_to_deduct = cost_credits
            if remaining_to_deduct > 0:
                # Select unexpired lots ordered by closest expiration
                lots = con.execute(
                    """
                    SELECT lot_id, credit_remaining, credit_used, expires_at FROM caca_credit_lots
                    WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ? AND credit_remaining > 0
                    ORDER BY expires_at ASC
                    """,
                    (int(user_id), now),
                ).fetchall()

                for lot in lots:
                    lid = lot["lot_id"]
                    rem = lot["credit_remaining"]
                    used = lot["credit_used"]

                    take = min(rem, remaining_to_deduct)
                    new_rem = rem - take
                    new_used = used + take
                    new_status = "DEPLETED" if new_rem == 0 else "ACTIVE"

                    con.execute(
                        "UPDATE caca_credit_lots SET credit_remaining = ?, credit_used = ?, status = ? WHERE lot_id = ?",
                        (new_rem, new_used, new_status, lid),
                    )
                    remaining_to_deduct -= take
                    if remaining_to_deduct <= 0:
                        break

            # 2. Release reservation
            if reservation_id:
                con.execute(
                    "UPDATE caca_reservations SET status = 'SETTLED' WHERE reservation_id = ?",
                    (reservation_id,),
                )

            # 3. Log usage
            con.execute(
                """
                INSERT INTO caca_usage_logs (
                    user_id, chat_id, reservation_id, prompt_tokens,
                    completion_tokens, total_tokens, cost_credits, is_billed, model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id), int(chat_id), reservation_id, prompt_tokens,
                    completion_tokens, total_tokens, cost_credits, 1 if is_billed else 0,
                    str(model), now,
                ),
            )

            con.execute("COMMIT")
            return cost_credits
        except Exception as e:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            log.error("Settle usage failed | user_id=%s err=%r", user_id, e)
            raise


# ── Admin & Audit Operations ─────────────────────────────────────────────────

def admin_grant_credit(
    admin_id: int,
    target_user_id: int,
    credits: int,
    duration_days: int,
    reason: str,
) -> int:
    """Manually issue a credit lot with audit log."""
    now = time.time()
    credits = int(credits)
    duration_days = int(duration_days)
    expires_at = now + (duration_days * 86400)

    with db_session(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO caca_credit_lots (
                user_id, invoice_short_id, credit_base, credit_bonus,
                credit_total, credit_used, credit_remaining,
                issued_at, expires_at, duration_days, status
            ) VALUES (?, NULL, ?, 0, ?, 0, ?, ?, ?, ?, 'ACTIVE')
            """,
            (int(target_user_id), credits, credits, credits, now, expires_at, duration_days),
        )
        cur = con.execute("SELECT last_insert_rowid()")
        lot_id = cur.fetchone()[0]

        con.execute(
            """
            INSERT INTO caca_audit_logs (
                admin_id, target_user_id, action, amount_credits, duration_days, reason, created_at
            ) VALUES (?, ?, 'GRANT', ?, ?, ?, ?)
            """,
            (int(admin_id), int(target_user_id), credits, duration_days, reason, now),
        )
        con.commit()
        return lot_id


def get_admin_overview() -> dict[str, Any]:
    """Overview statistics for owner dashboard."""
    with db_session(DB_PATH) as con:
        # Invoices stats
        row_inv = con.execute(
            """
            SELECT
                COUNT(*) as total_invoices,
                SUM(CASE WHEN status='PAID' THEN 1 ELSE 0 END) as paid_invoices,
                SUM(CASE WHEN status='PAID' THEN amount ELSE 0 END) as total_gross,
                SUM(CASE WHEN status='PAID' THEN base_amount ELSE 0 END) as total_net,
                SUM(CASE WHEN status='PAID' THEN gateway_fee ELSE 0 END) as total_fees,
                SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) as pending_invoices
            FROM caca_invoices
            """
        ).fetchone()

        # Credits stats
        row_lots = con.execute(
            """
            SELECT
                SUM(credit_total) as issued_sum,
                SUM(credit_used) as used_sum
            FROM caca_credit_lots
            """
        ).fetchone()

        # Token stats
        row_usage = con.execute(
            """
            SELECT
                SUM(prompt_tokens) as p_tokens,
                SUM(completion_tokens) as c_tokens,
                SUM(total_tokens) as t_tokens,
                SUM(cost_credits) as billed_credits
            FROM caca_usage_logs
            """
        ).fetchone()

        return {
            "total_invoices": int(row_inv[0] or 0),
            "paid_invoices": int(row_inv[1] or 0),
            "total_gross": int(row_inv[2] or 0),
            "total_net": int(row_inv[3] or 0),
            "total_fees": int(row_inv[4] or 0),
            "pending_invoices": int(row_inv[5] or 0),
            "total_credits_issued": int(row_lots[0] or 0),
            "total_credits_used": int(row_lots[1] or 0),
            "total_tokens_consumed": int(row_usage[2] or 0),
            "total_billed_credits": int(row_usage[3] or 0),
            "freecaca_enabled": is_freecaca_enabled(),
        }


# Auto-initialize database on module load
try:
    init_db()
except Exception:
    pass
