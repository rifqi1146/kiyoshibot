import os
import sqlite3
import time

DB_PATH = "data/vps.sqlite3"

DEFAULT_PRICING = {
    "vps_1gb": {"price_30d": 25000, "price_14d": 15000, "price_7d": 10000},
    "vps_2gb": {"price_30d": 35000, "price_14d": 21000, "price_7d": 14000},
    "vps_4gb": {"price_30d": 45000, "price_14d": 27000, "price_7d": 18000},
    "vps_8gb": {"price_30d": 55000, "price_14d": 33000, "price_7d": 22000},
}


def _db():
    os.makedirs("data", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = _db()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS vps_instances (
                vps_id             TEXT PRIMARY KEY,
                user_id            INTEGER NOT NULL,
                instance_id        TEXT NOT NULL,
                instance_type      TEXT NOT NULL,
                region             TEXT NOT NULL,
                tier               TEXT NOT NULL,
                os_image           TEXT NOT NULL,
                disk_gb            INTEGER NOT NULL,
                ip_address         TEXT,
                root_password      TEXT,
                price              INTEGER NOT NULL,
                days               INTEGER NOT NULL,
                created_at         REAL NOT NULL,
                expires_at         REAL NOT NULL,
                warranty_until     REAL NOT NULL,
                status             TEXT NOT NULL DEFAULT 'active',
                warranty_claimed   INTEGER NOT NULL DEFAULT 0,
                replaced_by        TEXT,
                terminated_at      REAL,
                termination_reason TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_vps_user ON vps_instances(user_id, status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_vps_expires ON vps_instances(status, expires_at)")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS vps_invoices (
                short_id       TEXT PRIMARY KEY,
                vps_id         TEXT,
                user_id        INTEGER NOT NULL,
                chat_id        INTEGER NOT NULL,
                message_id     INTEGER,
                qris_id        TEXT NOT NULL,
                trx_id         TEXT NOT NULL,
                tier           TEXT NOT NULL,
                os_image       TEXT NOT NULL,
                region         TEXT NOT NULL DEFAULT 'ap-southeast-5',
                days           INTEGER NOT NULL,
                base_amount    INTEGER NOT NULL,
                gross_amount   INTEGER NOT NULL,
                status         TEXT NOT NULL DEFAULT 'PENDING',
                created_at     REAL NOT NULL,
                paid_at        REAL,
                deadline       REAL NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_vps_inv_user ON vps_invoices(user_id, status)")
        if con.execute(
            "SELECT 1 FROM pragma_table_info('vps_invoices') WHERE name = 'has_photo'"
        ).fetchone() is None:
            con.execute("ALTER TABLE vps_invoices ADD COLUMN has_photo INTEGER NOT NULL DEFAULT 1")
        con.execute(
            "SELECT 1 FROM pragma_table_info('vps_invoices') WHERE name = 'region'"
        )
        if con.execute(
            "SELECT 1 FROM pragma_table_info('vps_invoices') WHERE name = 'region'"
        ).fetchone() is None:
            con.execute("ALTER TABLE vps_invoices ADD COLUMN region TEXT NOT NULL DEFAULT 'ap-southeast-5'")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS vps_pricing (
                tier       TEXT PRIMARY KEY,
                price_30d  INTEGER NOT NULL,
                price_14d  INTEGER NOT NULL DEFAULT 0,
                price_7d   INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        if con.execute(
            "SELECT 1 FROM pragma_table_info('vps_pricing') WHERE name = 'price_14d'"
        ).fetchone() is None:
            con.execute("ALTER TABLE vps_pricing ADD COLUMN price_14d INTEGER NOT NULL DEFAULT 0")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS vps_audit (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                vps_id     TEXT,
                action     TEXT NOT NULL,
                actor_id   INTEGER,
                detail     TEXT,
                created_at REAL NOT NULL
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS vps_regions (
                region      TEXT PRIMARY KEY,
                vpc_id      TEXT,
                subnet_ids  TEXT,
                sg_id       TEXT,
                ami_cache   TEXT,
                checked_at  REAL NOT NULL,
                available   INTEGER NOT NULL DEFAULT 1,
                last_error  TEXT
            )
            """
        )

        now = time.time()
        for tier, vals in DEFAULT_PRICING.items():
            con.execute(
                """
                INSERT OR IGNORE INTO vps_pricing (tier, price_30d, price_14d, price_7d, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tier, vals["price_30d"], vals["price_14d"], vals["price_7d"], now),
            )
            con.execute(
                """
                UPDATE vps_pricing SET price_14d = ?
                WHERE tier = ? AND (price_14d IS NULL OR price_14d <= 0)
                """,
                (vals["price_14d"], tier),
            )
        con.commit()
    finally:
        con.close()


def get_all_pricing() -> dict[str, dict[str, int]]:
    con = _db()
    try:
        rows = con.execute(
            "SELECT tier, price_30d, price_14d, price_7d FROM vps_pricing"
        ).fetchall()
        out = {}
        for r in rows:
            p14 = int(r["price_14d"] or 0)
            if p14 <= 0:
                p14 = DEFAULT_PRICING.get(r["tier"], {}).get("price_14d", 0)
            out[r["tier"]] = {
                "price_30d": int(r["price_30d"]),
                "price_14d": p14,
                "price_7d": int(r["price_7d"]),
            }
        for tier, vals in DEFAULT_PRICING.items():
            if tier not in out:
                out[tier] = dict(vals)
        return out
    finally:
        con.close()


def set_pricing(tier: str, price_30d: int, price_7d: int, price_14d: int | None = None):
    con = _db()
    try:
        if price_14d is None:
            cur = con.execute(
                "SELECT price_14d FROM vps_pricing WHERE tier = ?", (tier,)
            ).fetchone()
            if cur and int(cur["price_14d"] or 0) > 0:
                price_14d = int(cur["price_14d"])
            else:
                price_14d = DEFAULT_PRICING.get(tier, {}).get("price_14d", 0)
        con.execute(
            """
            INSERT OR REPLACE INTO vps_pricing (tier, price_30d, price_14d, price_7d, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tier, int(price_30d), int(price_14d), int(price_7d), time.time()),
        )
        con.commit()
    finally:
        con.close()


def save_invoice(
    *,
    short_id: str,
    user_id: int,
    chat_id: int,
    qris_id: str,
    trx_id: str,
    tier: str,
    os_image: str,
    region: str = "ap-southeast-5",
    days: int,
    base_amount: int,
    gross_amount: int,
    created_at: float,
    deadline: float,
) -> dict:
    con = _db()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO vps_invoices (
                short_id, vps_id, user_id, chat_id, message_id,
                qris_id, trx_id, tier, os_image, region, days,
                base_amount, gross_amount, status, created_at, paid_at, deadline
            ) VALUES (?, NULL, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, NULL, ?)
            """,
            (
                short_id, int(user_id), int(chat_id), qris_id, trx_id,
                tier, os_image, region, int(days), int(base_amount), int(gross_amount),
                float(created_at), float(deadline),
            ),
        )
        con.commit()
        return get_invoice(short_id)
    finally:
        con.close()


def get_invoice(short_id: str) -> dict | None:
    con = _db()
    try:
        r = con.execute("SELECT * FROM vps_invoices WHERE short_id = ?", (short_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def set_invoice_message_id(short_id: str, message_id: int, has_photo: bool = True):
    con = _db()
    try:
        con.execute(
            """
            UPDATE vps_invoices
            SET message_id = ?, has_photo = ?
            WHERE short_id = ?
            """,
            (int(message_id), 1 if has_photo else 0, short_id),
        )
        con.commit()
    finally:
        con.close()


def mark_invoice_paid(short_id: str, vps_id: str | None = None):
    con = _db()
    try:
        con.execute(
            """
            UPDATE vps_invoices
            SET status = 'PAID', paid_at = ?, vps_id = COALESCE(?, vps_id)
            WHERE short_id = ?
            """,
            (time.time(), vps_id, short_id),
        )
        con.commit()
    finally:
        con.close()


def set_invoice_status(short_id: str, status: str):
    con = _db()
    try:
        con.execute("UPDATE vps_invoices SET status = ? WHERE short_id = ?", (status, short_id))
        con.commit()
    finally:
        con.close()


def active_invoice_for_user(user_id: int) -> dict | None:
    con = _db()
    try:
        now = time.time()
        r = con.execute(
            """
            SELECT * FROM vps_invoices
            WHERE user_id = ? AND status = 'PENDING' AND deadline > ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (int(user_id), now),
        ).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def save_instance(
    *,
    vps_id: str,
    user_id: int,
    instance_id: str,
    instance_type: str,
    region: str,
    tier: str,
    os_image: str,
    disk_gb: int,
    ip_address: str,
    root_password: str,
    price: int,
    days: int,
    created_at: float,
    expires_at: float,
    warranty_until: float,
) -> dict:
    con = _db()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO vps_instances (
                vps_id, user_id, instance_id, instance_type, region,
                tier, os_image, disk_gb, ip_address, root_password,
                price, days, created_at, expires_at, warranty_until,
                status, warranty_claimed, replaced_by, terminated_at, termination_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, NULL, NULL, NULL)
            """,
            (
                vps_id, int(user_id), instance_id, instance_type, region,
                tier, os_image, int(disk_gb), ip_address, root_password,
                int(price), int(days), float(created_at), float(expires_at), float(warranty_until),
            ),
        )
        con.commit()
        return get_instance(vps_id)
    finally:
        con.close()


def get_instance(vps_id: str) -> dict | None:
    con = _db()
    try:
        r = con.execute("SELECT * FROM vps_instances WHERE vps_id = ?", (vps_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def list_user_instances(user_id: int) -> list[dict]:
    con = _db()
    try:
        rows = con.execute(
            """
            SELECT * FROM vps_instances
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (int(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def list_all_instances(status: str | None = None) -> list[dict]:
    con = _db()
    try:
        if status:
            rows = con.execute(
                "SELECT * FROM vps_instances WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM vps_instances ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def list_expired_instances() -> list[dict]:
    con = _db()
    try:
        now = time.time()
        rows = con.execute(
            """
            SELECT * FROM vps_instances
            WHERE status = 'active' AND expires_at <= ?
            """,
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def terminate_instance_db(vps_id: str, reason: str = "manual"):
    con = _db()
    try:
        con.execute(
            """
            UPDATE vps_instances
            SET status = 'terminated', terminated_at = ?, termination_reason = ?
            WHERE vps_id = ?
            """,
            (time.time(), reason, vps_id),
        )
        con.commit()
    finally:
        con.close()


def renew_instance_db(vps_id: str, additional_days: int) -> dict | None:
    con = _db()
    try:
        cur = get_instance(vps_id)
        if not cur:
            return None
        new_expires = max(cur["expires_at"], time.time()) + (additional_days * 86400)
        new_days = cur["days"] + additional_days
        con.execute(
            """
            UPDATE vps_instances
            SET expires_at = ?, days = ?, status = 'active'
            WHERE vps_id = ?
            """,
            (new_expires, new_days, vps_id),
        )
        con.commit()
        return get_instance(vps_id)
    finally:
        con.close()


def mark_warranty_claimed(vps_id: str, replaced_by: str | None = None):
    con = _db()
    try:
        con.execute(
            """
            UPDATE vps_instances
            SET warranty_claimed = 1, replaced_by = ?, status = 'replaced',
                terminated_at = COALESCE(terminated_at, ?)
            WHERE vps_id = ?
            """,
            (replaced_by, time.time(), vps_id),
        )
        con.commit()
    finally:
        con.close()


def log_audit(vps_id: str | None, action: str, actor_id: int | None, detail: str | None = None):
    con = _db()
    try:
        con.execute(
            """
            INSERT INTO vps_audit (vps_id, action, actor_id, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (vps_id, action, int(actor_id) if actor_id else None, detail, time.time()),
        )
        con.commit()
    finally:
        con.close()


def claim_invoice_for_provision(short_id: str) -> dict | None:
    con = _db()
    try:
        cur = con.execute(
            """
            UPDATE vps_invoices
            SET status = 'PROVISIONING'
            WHERE short_id = ? AND status = 'PENDING'
            """,
            (short_id,),
        )
        con.commit()
        if cur.rowcount == 0:
            return None
        r = con.execute("SELECT * FROM vps_invoices WHERE short_id = ?", (short_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def get_stats() -> dict:
    con = _db()
    try:
        total = con.execute("SELECT COUNT(*) FROM vps_instances").fetchone()[0]
        active = con.execute("SELECT COUNT(*) FROM vps_instances WHERE status = 'active'").fetchone()[0]
        rev = con.execute("SELECT SUM(price) FROM vps_instances WHERE status != 'replaced'").fetchone()[0] or 0
        warr_claims = con.execute("SELECT COUNT(*) FROM vps_instances WHERE warranty_claimed = 1").fetchone()[0]
        return {
            "total_instances": total,
            "active_instances": active,
            "total_revenue": rev,
            "warranty_claims": warr_claims,
        }
    finally:
        con.close()


def next_vps_id(user_id: int) -> str:
    con = _db()
    try:
        base = str(int(user_id))
        if not con.execute("SELECT 1 FROM vps_instances WHERE vps_id = ?", (base,)).fetchone():
            return base
        n = 2
        while con.execute("SELECT 1 FROM vps_instances WHERE vps_id = ?", (f"{base}-{n}",)).fetchone():
            n += 1
        return f"{base}-{n}"
    finally:
        con.close()
    con = _db()
    try:
        total = con.execute("SELECT COUNT(*) FROM vps_instances").fetchone()[0]
        active = con.execute("SELECT COUNT(*) FROM vps_instances WHERE status = 'active'").fetchone()[0]
        rev = con.execute("SELECT SUM(price) FROM vps_instances WHERE status != 'replaced'").fetchone()[0] or 0
        warr_claims = con.execute("SELECT COUNT(*) FROM vps_instances WHERE warranty_claimed = 1").fetchone()[0]
        return {
            "total_instances": total,
            "active_instances": active,
            "total_revenue": rev,
            "warranty_claims": warr_claims,
        }
    finally:
        con.close()


def purge_old_terminated(retention_days: int = 7) -> dict:
    cutoff = time.time() - (int(retention_days) * 86400)
    con = _db()
    try:
        inst_cur = con.execute(
            """
            DELETE FROM vps_instances
            WHERE status IN ('terminated', 'replaced')
              AND COALESCE(terminated_at, expires_at, created_at) <= ?
            """,
            (cutoff,),
        )
        inv_cur = con.execute(
            """
            DELETE FROM vps_invoices
            WHERE status IN ('EXPIRED', 'CANCELLED')
              AND created_at <= ?
            """,
            (cutoff,),
        )
        con.commit()
        return {
            "instances": inst_cur.rowcount or 0,
            "invoices": inv_cur.rowcount or 0,
        }
    finally:
        con.close()


def get_region_stats() -> dict[str, dict]:
    con = _db()
    try:
        rows = con.execute(
            """
            SELECT region,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                   SUM(price) AS revenue
            FROM vps_instances
            GROUP BY region
            """
        ).fetchall()
        out = {}
        for r in rows:
            rev = con.execute(
                "SELECT SUM(price) FROM vps_instances WHERE region = ? AND status != 'replaced'",
                (r["region"],),
            ).fetchone()[0] or 0
            out[r["region"]] = {
                "total": int(r["total"] or 0),
                "active": int(r["active"] or 0),
                "revenue": int(rev),
            }
        return out
    finally:
        con.close()


def list_active_regions() -> list[str]:
    con = _db()
    try:
        rows = con.execute(
            "SELECT DISTINCT region FROM vps_instances WHERE status = 'active'"
        ).fetchall()
        return [r["region"] for r in rows]
    finally:
        con.close()


def save_region_cache(
    region: str,
    *,
    vpc_id: str | None,
    subnet_ids: list[str] | None,
    sg_id: str | None,
    available: bool = True,
    last_error: str | None = None,
):
    import json

    con = _db()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO vps_regions
                (region, vpc_id, subnet_ids, sg_id, ami_cache, checked_at, available, last_error)
            VALUES (
                ?, ?, ?, ?,
                COALESCE((SELECT ami_cache FROM vps_regions WHERE region = ?), NULL),
                ?, ?, ?
            )
            """,
            (
                region,
                vpc_id,
                json.dumps(subnet_ids) if subnet_ids is not None else None,
                sg_id,
                region,
                time.time(),
                1 if available else 0,
                last_error,
            ),
        )
        con.commit()
    finally:
        con.close()


def get_region_cache(region: str) -> dict | None:
    import json

    con = _db()
    try:
        r = con.execute("SELECT * FROM vps_regions WHERE region = ?", (region,)).fetchone()
        if not r:
            return None
        d = dict(r)
        raw = d.get("subnet_ids")
        try:
            d["subnet_ids"] = json.loads(raw) if raw else []
        except Exception:
            d["subnet_ids"] = []
        raw_amis = d.get("ami_cache")
        try:
            d["amis"] = json.loads(raw_amis) if raw_amis else {}
        except Exception:
            d["amis"] = {}
        return d
    finally:
        con.close()


def set_region_ami(region: str, os_id: str, ami_id: str):
    import json

    con = _db()
    try:
        r = con.execute("SELECT ami_cache FROM vps_regions WHERE region = ?", (region,)).fetchone()
        amis = {}
        if r and r["ami_cache"]:
            try:
                amis = json.loads(r["ami_cache"])
            except Exception:
                amis = {}
        amis[os_id] = ami_id
        if r:
            con.execute(
                "UPDATE vps_regions SET ami_cache = ? WHERE region = ?",
                (json.dumps(amis), region),
            )
        else:
            con.execute(
                """
                INSERT INTO vps_regions (region, vpc_id, subnet_ids, sg_id, ami_cache, checked_at, available, last_error)
                VALUES (?, NULL, NULL, NULL, ?, ?, 1, NULL)
                """,
                (region, json.dumps(amis), time.time()),
            )
        con.commit()
    finally:
        con.close()
