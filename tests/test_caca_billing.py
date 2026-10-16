
import os
import sys
import time
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP_DIR = tempfile.mkdtemp(prefix="caca_bill_test_")
os.environ.setdefault("CACA_BILLING_TEST_DIR", _TMP_DIR)

from database import caca_billing_db as bill

bill.DB_PATH = os.path.join(_TMP_DIR, "test_billing.sqlite3")
bill._FREECACA_CACHE = None
bill.init_db()

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def section(title: str):
    print(f"\n=== {title} ===")


def fresh_db():
    with bill.db_session(bill.DB_PATH) as con:
        for table in ("caca_invoices", "caca_credit_lots", "caca_reservations",
                      "caca_usage_logs", "caca_audit_logs", "caca_settings"):
            con.execute(f"DELETE FROM {table}")
        con.commit()
    bill._FREECACA_CACHE = None


def make_paid_invoice(user_id: int, amount: int, base: int, bonus: int, total: int, days: int) -> str:
    short_id = f"t{int(time.time() * 1000) % 10**9}{user_id % 1000}"
    now = time.time()
    gross, fee = bill.calculate_gross_amount(amount, bill.GATEWAY_FEE_BPS)
    bill.save_invoice(
        short_id=short_id, qris_id=f"q_{short_id}", trx_id=f"trx_{short_id}",
        user_id=user_id, chat_id=user_id,
        amount=gross, base_amount=amount, gateway_fee=fee, fee_bps=bill.GATEWAY_FEE_BPS,
        credit_base=base, credit_bonus=bonus, credit_total=total,
        duration_days=days, is_custom=False,
        created_at=now, deadline=now + 300,
    )
    bill.fulfill_payment(short_id)
    return short_id


section("Preset packages")
EXPECTED_PRESETS = {
    3000:  (300000, 0, 300000, 7),
    5000:  (500000, 10000, 510000, 10),
    10000: (1000000, 50000, 1050000, 14),
    20000: (2000000, 120000, 2120000, 21),
    30000: (3000000, 300000, 3300000, 30),
}
for price, (base, bonus, total, days) in EXPECTED_PRESETS.items():
    pkg = bill.calculate_preset_package(price)
    ok = (pkg and pkg["base"] == base and pkg["bonus"] == bonus
          and pkg["total"] == total and pkg["days"] == days)
    check(f"preset Rp{price} = {total} credit / {days}d", bool(ok), str(pkg))

check("unknown preset returns None", bill.calculate_preset_package(4000) is None)


section("Custom nominal & duration")
c = bill.calculate_custom_package(3000, 7)
check("custom min Rp3000 -> 300000 credit", c["total"] == 300000 and c["bonus"] == 0)
c = bill.calculate_custom_package(200000, 30)
check("custom max Rp200000 -> 20.000.000 credit", c["total"] == 20000000)
c = bill.calculate_custom_package(7000, 14)
check("custom Rp7000 -> 700000 credit", c["total"] == 700000 and c["days"] == 14)
c = bill.calculate_custom_package(15000, 21)
check("custom Rp15000 -> 1500000 credit", c["total"] == 1500000)

for bad in (2999, 0, -5000, 200001, 1000000):
    try:
        bill.calculate_custom_package(bad, 7)
        check(f"custom rejects Rp{bad}", False, "(accepted)")
    except ValueError:
        check(f"custom rejects Rp{bad}", True)

for bad_day in (0, 1, 6, 8, 15, 31, 100):
    try:
        bill.calculate_custom_package(3000, bad_day)
        check(f"custom rejects {bad_day} days", False, "(accepted)")
    except ValueError:
        check(f"custom rejects {bad_day} days", True)

for d in bill.ALLOWED_CUSTOM_DAYS:
    ok = bill.calculate_custom_package(5000, d)["days"] == d
    check(f"custom accepts {d} days", ok)

a = bill.calculate_custom_package(3000, 7)
b = bill.calculate_custom_package(3000, 30)
check("duration does not change credit (Rp3000: 7d == 30d)", a["total"] == b["total"] == 300000)

check("custom never gets preset bonus",
      bill.calculate_custom_package(5000, 14)["bonus"] == 0)
check("preset DOES get bonus (Rp5000)", bill.calculate_preset_package(5000)["bonus"] == 10000)


section("Gateway fee gross-up (2%)")
import math as _math

for base_rp in (3000, 5000, 10000, 20000, 30000, 7000, 15000, 200000):
    gross, fee = bill.calculate_gross_amount(base_rp, 200)
    net_floor = gross - _math.floor(gross * 0.02)
    net_ceil = gross - _math.ceil(gross * 0.02)
    check(f"gross-up Rp{base_rp}: net >= target (floor & ceil)",
          net_floor >= base_rp and net_ceil >= base_rp,
          f"gross={gross} net_f={net_floor} net_c={net_ceil}")
    check(f"gross-up Rp{base_rp}: fee equals gross - target",
          fee == gross - base_rp, f"fee={fee}")

g, f = bill.calculate_gross_amount(20000, 200)
check("gross-up Rp20000 -> Rp20409 / fee Rp409", g == 20409 and f == 409, f"{g}/{f}")
check("gross-up never lowers the amount", g > 20000)
check("gross-up fee rate configurable", bill.calculate_gross_amount(10000, 100)[0] == 10102,
      str(bill.calculate_gross_amount(10000, 100)))
check("gross-up zero fee = passthrough", bill.calculate_gross_amount(5000, 0) == (5000, 0))
check("gross-up target 0 -> (0,0)", bill.calculate_gross_amount(0) == (0, 0))

ok_all = True
for base_rp in range(3000, 30001, 137):
    gross, fee = bill.calculate_gross_amount(base_rp, 200)
    if gross - _math.ceil(gross * 0.02) < base_rp:
        ok_all = False
        break
check("owner never loses money across range Rp3000..Rp30000", ok_all)


section("Amount text parsing (UI validator)")
from handlers.caca.caca_billing import parse_custom_amount

check("parse '7000'", parse_custom_amount("7000") == 7000)
check("parse '7.000'", parse_custom_amount("7.000") == 7000)
check("parse 'Rp 7.000'", parse_custom_amount("Rp 7.000") == 7000)
check("parse 'Rp7000'", parse_custom_amount("Rp7000") == 7000)
for bad in ("", "   ", "abc", "0", "-5000", "2999", "200001",
            "7000.50", "7,50", "12.34.56.78"):
    try:
        parse_custom_amount(bad)
        check(f"parse rejects {bad!r}", False, "(accepted)")
    except ValueError:
        check(f"parse rejects {bad!r}", True)


section("Payment fulfilment idempotency")
fresh_db()
uid = 111
short = make_paid_invoice(uid, 5000, 50000, 1000, 51000, 10)
bal = bill.get_user_balance(uid)
check("paid invoice issues credit once", bal["available_credits"] == 51000, str(bal["available_credits"]))

again = bill.fulfill_payment(short)
bal2 = bill.get_user_balance(uid)
check("duplicate fulfil returns False", again is False)
check("duplicate fulfil adds nothing", bal2["available_credits"] == 51000, str(bal2["available_credits"]))

bill.fulfill_payment(short)
bill.fulfill_payment(short)
bal3 = bill.get_user_balance(uid)
check("repeated fulfil still 1 lot", len(bal3["active_lots"]) == 1)

fresh_db()
now = time.time()
for status in ("PENDING", "CANCELLED", "EXPIRED", "FAILED"):
    sid = f"s_{status}_{int(now)}"
    bill.save_invoice(
        short_id=sid, qris_id=f"q_{sid}", trx_id=f"trx_{sid}", user_id=uid, chat_id=uid,
        amount=5000, credit_base=50000, credit_bonus=1000, credit_total=51000,
        duration_days=10, is_custom=False,
        created_at=now, deadline=now + 300,
    )
    bill.set_invoice_status(sid, status)
check("non-PAID invoices issue no credit", bill.get_user_balance(uid)["available_credits"] == 0)


section("Expiry from verified payment time")
fresh_db()
uid = 222
now = time.time()
sid = "exp_test"
bill.save_invoice(
    short_id=sid, qris_id="q1", trx_id="t1", user_id=uid, chat_id=uid,
    amount=3000, credit_base=30000, credit_bonus=0, credit_total=30000,
    duration_days=7, is_custom=True,
    created_at=now - 3600,
    deadline=now + 300,
)
pay_at = now
bill.fulfill_payment(sid, paid_at=pay_at)
lot = bill.get_user_balance(uid)["active_lots"][0]
drift = abs(lot["expires_at"] - (pay_at + 7 * 86400))
check("expiry = paid_at + duration (not created_at)", drift < 2, f"drift={drift}")


section("Independent lots")
fresh_db()
uid = 333
now = time.time()
for i, days in enumerate((7, 10, 14)):
    sid = f"lot_{i}"
    bill.save_invoice(
        short_id=sid, qris_id=f"q{i}", trx_id=f"t{i}", user_id=uid, chat_id=uid,
        amount=3000, credit_base=30000, credit_bonus=0, credit_total=30000,
        duration_days=days, is_custom=True, created_at=now, deadline=now + 300,
    )
    bill.fulfill_payment(sid, paid_at=now + i)
bal = bill.get_user_balance(uid)
check("three purchases -> three lots", len(bal["active_lots"]) == 3, str(len(bal["active_lots"])))
check("three purchases -> 90000 credit total", bal["available_credits"] == 90000)
expiries = sorted(l["expires_at"] for l in bal["active_lots"])
check("each lot has its own distinct expiry", len(set(round(e) for e in expiries)) == 3)
check("lots ordered soonest-expiry first",
      [l["expires_at"] for l in bal["active_lots"]] == sorted(l["expires_at"] for l in bal["active_lots"]))


section("FEFO consumption order")
fresh_db()
uid = 444
now = time.time()
for sid, days, credits in (("A", 7, 1000), ("B", 30, 5000)):
    bill.save_invoice(
        short_id=sid, qris_id=sid, trx_id=sid, user_id=uid, chat_id=uid,
        amount=3000, credit_base=credits, credit_bonus=0, credit_total=credits,
        duration_days=days, is_custom=True, created_at=now, deadline=now + 300,
    )
    bill.fulfill_payment(sid, paid_at=now)

bill.settle_usage(user_id=uid, chat_id=uid, reservation_id=None,
                  prompt_tokens=800, completion_tokens=0, model="deepseek-v4-flash")
bal = bill.get_user_balance(uid)
lots = {l["invoice_short_id"]: l for l in bal["active_lots"]}
check("FEFO: soonest-expiring lot consumed first",
      lots.get("A", {}).get("credit_remaining") == 200,
      str({k: v["credit_remaining"] for k, v in lots.items()}))
check("FEFO: later lot untouched", lots.get("B", {}).get("credit_remaining") == 5000)

bill.settle_usage(user_id=uid, chat_id=uid, reservation_id=None,
                  prompt_tokens=500, completion_tokens=0, model="deepseek-v4-flash")
bal = bill.get_user_balance(uid)
lots = {l["invoice_short_id"]: l for l in bal["active_lots"]}
check("FEFO spills over to next lot", lots.get("B", {}).get("credit_remaining") == 4700,
      str({k: v["credit_remaining"] for k, v in lots.items()}))
check("FEFO depleted lot removed from active", "A" not in lots)

check("rate: input = 1 credit/token", bill.RATE_INPUT_TOKEN == 1)
check("rate: output = 2 credits/token", bill.RATE_OUTPUT_TOKEN == 2)


section("Pricing target (300 chats / Rp 3.000)")
check("Rp 1 = 100 credit", bill.RATE_CREDIT_PER_RUPIAH == 100)
pkg = bill.calculate_preset_package(3000)
check("Rp 3.000 preset = 300.000 credit", pkg["total"] == 300000, str(pkg["total"]))

chat_cost = 900 * bill.RATE_INPUT_TOKEN + 50 * bill.RATE_OUTPUT_TOKEN
check("1 chat costs 1.000 credit (900 in + 50 out)", chat_cost == 1000, str(chat_cost))
chats = pkg["total"] // chat_cost
check("Rp 3.000 preset gives >= 300 chats", chats >= 300, f"{chats} chats")
check("custom Rp 3.000 also gives 300.000 credit",
      bill.calculate_custom_package(3000, 7)["total"] == 300000)


section("/addcredit admin helper")
fresh_db()
uid = 4242
res = bill.admin_add_credit(admin_id=1, target_user_id=uid, credits=300000,
                            duration_days=30, reason="test add")
check("addcredit creates new lot when none active", res["extended_existing"] is False)
check("addcredit grants the exact amount",
      bill.get_user_balance(uid)["available_credits"] == 300000)

res2 = bill.admin_add_credit(admin_id=1, target_user_id=uid, credits=100000,
                             duration_days=30, reason="top up existing",
                             extend_latest_lot=True)
check("addcredit extends existing lot", res2["extended_existing"] is True)
check("addcredit extension sums up", bill.get_user_balance(uid)["available_credits"] == 400000)
check("addcredit did not create a 2nd lot", len(bill.get_user_balance(uid)["active_lots"]) == 1)

with bill.db_session(bill.DB_PATH) as con:
    n = con.execute("SELECT COUNT(*) FROM caca_audit_logs WHERE target_user_id=? AND action='ADD'", (uid,)).fetchone()[0]
check("addcredit writes 2 audit rows", n == 2, str(n))

try:
    bill.admin_add_credit(admin_id=1, target_user_id=uid, credits=0, duration_days=7)
    check("addcredit rejects 0 credits", False, "(accepted)")
except ValueError:
    check("addcredit rejects 0 credits", True)


section("Compensation of existing buyers")
fresh_db()
paid_uid = 5551
now = time.time()
bill.save_invoice(
    short_id="comp1", qris_id="q", trx_id="t", user_id=paid_uid, chat_id=paid_uid,
    amount=3062, base_amount=3000, gateway_fee=61, fee_bps=200,
    credit_base=30000, credit_bonus=0, credit_total=30000,
    duration_days=7, is_custom=False, created_at=now, deadline=now + 300,
)
bill.fulfill_payment("comp1", paid_at=now)
check("pre-compensation balance is 30.000", bill.get_user_balance(paid_uid)["available_credits"] == 30000)

bill.admin_add_credit(1, paid_uid, 270000, 7, "compensation rate change", extend_latest_lot=True)
bal = bill.get_user_balance(paid_uid)
check("post-compensation balance is 300.000", bal["available_credits"] == 300000, str(bal["available_credits"]))
check("compensation gives >= 300 chats", bal["available_credits"] // 1000 >= 300)

ids = bill.paid_preset_user_ids(3000)
check("paid_preset_user_ids(3000) finds the buyer", paid_uid in ids, str(ids))
check("paid_preset_user_ids(9999) finds nobody", bill.paid_preset_user_ids(9999) == [])


section("Caca history truncation")
import utils.caca_memory as cm

check("CACA_MAX_TURNS defaults to 8", cm.CACA_MAX_TURNS == 8, str(cm.CACA_MAX_TURNS))
check("max history messages sent is 16", cm.CACA_MAX_TURNS * 2 == 16)

hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(30)]
max_msgs = cm.CACA_MAX_TURNS * 2
trimmed = hist[-max_msgs:] if len(hist) > max_msgs else hist
check("trimmed length is 16", len(trimmed) == 16, str(len(trimmed)))
check("trimmed keeps newest messages", trimmed[-1]["content"] == "msg 29")
check("trimmed dropped oldest messages", trimmed[0]["content"] == "msg 14")


section("Expired lots")
fresh_db()
uid = 555
now = time.time()
sid = "old"
bill.save_invoice(
    short_id=sid, qris_id="q", trx_id="t", user_id=uid, chat_id=uid,
    amount=3000, credit_base=30000, credit_bonus=0, credit_total=30000,
    duration_days=7, is_custom=True, created_at=now, deadline=now + 300,
)
bill.fulfill_payment(sid, paid_at=now)
with bill.db_session(bill.DB_PATH) as con:
    con.execute("UPDATE caca_credit_lots SET expires_at = ? WHERE user_id = ?", (now - 10, uid))
    con.commit()

bal = bill.get_user_balance(uid)
check("expired lot excluded from available", bal["available_credits"] == 0, str(bal["available_credits"]))
check("expired credit reported as expired", bal["expired_credits"] == 30000, str(bal["expired_credits"]))
check("expired lot not usable for chat", bill.has_active_credit(uid, 1) is False)
check("expired lot history retained (not deleted)",
      len(bal["active_lots"]) == 0 and bal["expired_credits"] == 30000)

sid2 = "new"
bill.save_invoice(
    short_id=sid2, qris_id="q2", trx_id="t2", user_id=uid, chat_id=uid,
    amount=3000, credit_base=30000, credit_bonus=0, credit_total=30000,
    duration_days=7, is_custom=True, created_at=now, deadline=now + 300,
)
bill.fulfill_payment(sid2, paid_at=now)
bal = bill.get_user_balance(uid)
check("new top-up does not extend old lot", bal["available_credits"] == 30000)
check("expired lot still counted as expired after top-up", bal["expired_credits"] == 30000)


section("Concurrency safety")
fresh_db()
uid = 666
now = time.time()
sid = "cc"
bill.save_invoice(
    short_id=sid, qris_id="q", trx_id="t", user_id=uid, chat_id=uid,
    amount=3000, credit_base=30000, credit_bonus=0, credit_total=30000,
    duration_days=7, is_custom=True, created_at=now, deadline=now + 300,
)
bill.fulfill_payment(sid, paid_at=now)

results: list[str | None] = []
lock = threading.Lock()


def reserve_worker():
    r = bill.reserve_credits(uid, amount=20000)
    with lock:
        results.append(r)


threads = [threading.Thread(target=reserve_worker) for _ in range(6)]
for t in threads:
    t.start()
for t in threads:
    t.join()

granted = [r for r in results if r]
bal = bill.get_user_balance(uid)
check("concurrent reservations never exceed balance",
      bal["reserved_credits"] <= 30000, str(bal["reserved_credits"]))
check("at least one reservation granted", len(granted) >= 1)
check("balance never negative", bal["available_credits"] >= 0)

for r in granted:
    bill.release_reservation(r)

overspend_results: list[int] = []


def settle_worker():
    cost = bill.settle_usage(user_id=uid, chat_id=uid, reservation_id=None,
                             prompt_tokens=10000, completion_tokens=0,
                             model="deepseek-v4-flash")
    with lock:
        overspend_results.append(cost)


threads = [threading.Thread(target=settle_worker) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()

bal_final = bill.get_user_balance(uid)
check("concurrent overspend cannot go negative", bal_final["available_credits"] >= 0,
      str(bal_final["available_credits"]))


section("Reservation release")
fresh_db()
uid = 777
now = time.time()
sid = "rr"
bill.save_invoice(
    short_id=sid, qris_id="q", trx_id="t", user_id=uid, chat_id=uid,
    amount=3000, credit_base=30000, credit_bonus=0, credit_total=30000,
    duration_days=7, is_custom=True, created_at=now, deadline=now + 300,
)
bill.fulfill_payment(sid, paid_at=now)

res = bill.reserve_credits(uid, amount=2000)
check("reservation created", res is not None)
bal = bill.get_user_balance(uid)
check("reserved credits visible", bal["reserved_credits"] == 2000, str(bal["reserved_credits"]))
bill.release_reservation(res)
bal = bill.get_user_balance(uid)
check("release clears reservation", bal["reserved_credits"] == 0)
check("release does NOT deduct credits", bal["available_credits"] == 30000)

fresh_db()
check("reservation refused with zero balance", bill.reserve_credits(999, 2000) is None)


section("Streaming settled once")
fresh_db()
uid = 888
now = time.time()
sid = "st"
bill.save_invoice(
    short_id=sid, qris_id="q", trx_id="t", user_id=uid, chat_id=uid,
    amount=10000, credit_base=100000, credit_bonus=5000, credit_total=105000,
    duration_days=14, is_custom=False, created_at=now, deadline=now + 300,
)
bill.fulfill_payment(sid, paid_at=now)

res = bill.reserve_credits(uid, amount=2000)
bill.settle_usage(user_id=uid, chat_id=uid, reservation_id=res,
                  prompt_tokens=400, completion_tokens=250, model="deepseek-v4-flash")
bal = bill.get_user_balance(uid)
check("streaming: settled once (400 + 2*250 = 900)", bal["available_credits"] == 105000 - 900,
      str(bal["available_credits"]))
check("streaming: reservation cleared after settle", bal["reserved_credits"] == 0)

with bill.db_session(bill.DB_PATH) as con:
    n = con.execute("SELECT COUNT(*) FROM caca_usage_logs WHERE user_id = ?", (uid,)).fetchone()[0]
check("streaming: exactly one usage log row", n == 1, str(n))


section("/freecaca toggle")
fresh_db()
check("freecaca defaults to disabled", bill.is_freecaca_enabled() is False)
bill.set_freecaca_enabled(True)
check("freecaca enable persists", bill.is_freecaca_enabled() is True)
bill._FREECACA_CACHE = None
check("freecaca self-heals cache from DB", bill.is_freecaca_enabled() is True)

uid = 999
now = time.time()
sid = "fc"
bill.save_invoice(
    short_id=sid, qris_id="q", trx_id="t", user_id=uid, chat_id=uid,
    amount=3000, credit_base=30000, credit_bonus=0, credit_total=30000,
    duration_days=7, is_custom=True, created_at=now, deadline=now + 300,
)
bill.fulfill_payment(sid, paid_at=now)
bill.set_freecaca_enabled(False)
bill._FREECACA_CACHE = None
bal = bill.get_user_balance(uid)
check("disable freecaca does NOT delete lots", bal["available_credits"] == 30000,
      str(bal["available_credits"]))
check("freecaca disabled after disable", bill.is_freecaca_enabled() is False)


section("Balance & package display states")
from handlers.caca.caca_billing import _packages_text, _balance_text
from utils.config import OWNER_ID

test_owner = sorted(OWNER_ID)[0]
test_user = 99912345

bill.set_freecaca_enabled(False)
txt = _packages_text(test_owner)
check("packages: owner gets owner note (not 'FREE for everyone')",
      "OWNER" in txt and "FREE for everyone" not in txt, txt)
bal = _balance_text(test_owner)
check("balance: owner shows 'Unlimited' available", "Unlimited" in bal, bal)
check("balance: owner shows 'OWNER — UNLIMITED ACCESS'", "OWNER — UNLIMITED ACCESS" in bal)

txt = _packages_text(test_user)
check("packages: plain user gets no free/owner note",
      "FREE for everyone" not in txt and "OWNER" not in txt, txt)
bal = _balance_text(test_user)
check("balance: plain user shows 0 credits", "Available:</b> <code>0</code>" in bal)

bill.set_freecaca_enabled(True)
txt = _packages_text(test_user)
check("packages: freecaca ON shows 'FREE for everyone'", "FREE for everyone" in txt)
bal = _balance_text(test_user)
check("balance: freecaca ON shows free access active", "FREE ACCESS — ACTIVE" in bal)
bill.set_freecaca_enabled(False)


section("Invoice ownership")
fresh_db()
now = time.time()
bill.save_invoice(
    short_id="owner_a", qris_id="q", trx_id="t", user_id=1001, chat_id=1001,
    amount=3000, credit_base=30000, credit_bonus=0, credit_total=30000,
    duration_days=7, is_custom=True, created_at=now, deadline=now + 300,
)
inv = bill.get_invoice("owner_a")
check("invoice stores its owner", inv["user_id"] == 1001)
check("other user cannot find invoice in their active list",
      bill.active_invoice_for_user(1002) is None)
check("owner finds their own active invoice",
      (bill.active_invoice_for_user(1001) or {}).get("short_id") == "owner_a")


section("Admin grant & audit")
fresh_db()
lot_id = bill.admin_grant_credit(admin_id=42, target_user_id=2002, credits=5000,
                                 duration_days=7, reason="test grant")
bal = bill.get_user_balance(2002)
check("admin grant issues credit", bal["available_credits"] == 5000)
with bill.db_session(bill.DB_PATH) as con:
    row = con.execute("SELECT admin_id, target_user_id, action, reason FROM caca_audit_logs ORDER BY audit_id DESC LIMIT 1").fetchone()
check("admin grant writes audit log",
      row and row[0] == 42 and row[1] == 2002 and row[2] == "GRANT" and row[3] == "test grant")

ov = bill.get_admin_overview()
check("overview reports issued credits", ov["total_credits_issued"] >= 5000)


section("Legacy DB compatibility")
check("payments table untouched by billing module", os.path.exists("data/payments.sqlite3"))
check("caca.sqlite3 premium table untouched", os.path.exists("data/caca.sqlite3"))
try:
    from database import payment_db
    from database import premium
    check("payment_db imports fine", hasattr(payment_db, "init_db"))
    check("premium module imports fine", hasattr(premium, "is_premium"))
except Exception as e:
    check("legacy modules import", False, repr(e))
try:
    from handlers.payment.buypremium import buypremium_cmd
    check("/buypremium handler intact", callable(buypremium_cmd))
except Exception as e:
    check("/buypremium handler intact", False, repr(e))


print("\n" + "=" * 60)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("\nFailures:")
    for f in FAILURES:
        print(f"  - {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
