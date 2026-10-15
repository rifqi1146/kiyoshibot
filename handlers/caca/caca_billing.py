"""Caca Credit Billing: UI, top-up flow (preset + custom), balance, and owner controls.

FLOW SCRAPER / HANDLER:
1. `/cacatopup` (or `/buycaca`) -> preset package picker (Rp3k..Rp30k) +
   "Custom Amount & Duration" button + "My Balance".
2. Preset press -> order summary + Confirm/Cancel buttons.
3. Custom press -> prompt for amount (text input validated server-side) ->
   duration picker (7/14/21/30 days) -> order summary -> Confirm/Cancel.
4. Confirm -> create QRIS invoice via `utils.axiom.create_qris()`, persist the
   invoice (amount, credits, duration) in `data/caca_billing.sqlite3`, render the
   QR PNG locally, then poll payment status every 15s.
5. Payment PAID -> `fulfill_payment()` issues exactly one credit lot (idempotent).
6. `/cacasaldo` -> available credits, reserved, expired, and per-lot expiry.
7. `/freecaca enable|disable` -> owner-only global free-access switch.
8. `/cacaadmin` -> owner-only reconciliation dashboard + manual grant.

Callbacks: prefix `cacabill:` (bound to the invoker via `utils.menu_lock`).
"""

import asyncio
import html
import io
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import qrcode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import caca_billing_db as bill
from database import premium
from utils import axiom
from utils.config import AXIOM_BASE_URL, OWNER_ID
from utils.menu_lock import guard_callback_owner, register_message_owner

log = logging.getLogger(__name__)

PAYMENT_DEADLINE_SEC = 300          # 5-minute payment window (same as /buypremium)
POLL_INTERVAL_SEC = 15
WIB = timezone(timedelta(hours=7))
CUSTOM_FLOW_TTL = 300               # custom-input session lifetime

# chat_id -> {"user_id", "stage", "message_id", "amount", "days", "created_at"}
_CUSTOM_FLOW: dict[int, dict] = {}
_CHECK_LOCKS: dict[str, asyncio.Lock] = {}

_AMOUNT_RE = re.compile(r"^\s*(?:rp\.?\s*)?([\d.,]+)\s*$", re.IGNORECASE)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _lock(short_id: str) -> asyncio.Lock:
    lk = _CHECK_LOCKS.get(short_id)
    if lk is None:
        lk = asyncio.Lock()
        _CHECK_LOCKS[short_id] = lk
    return lk


def _fmt_credits(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")


def _fmt_wib(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=WIB).strftime("%Y-%m-%d %H:%M WIB")


def _fmt_days(epoch: float) -> str:
    remaining = max(0, int(epoch - time.time()))
    d, rem = divmod(remaining, 86400)
    h = rem // 3600
    if d > 0:
        return f"{d}d {h}h"
    m = rem // 60
    return f"{h}h {m}m" if h > 0 else f"{m}m"


def _render_qr_png(payload: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def parse_custom_amount(raw: str) -> int:
    """Parse a user-typed rupiah amount into an integer. Raises ValueError when invalid.

    Rejects: empty, non-numeric, decimals, zero, negative, and out-of-range values.
    Accepts: '7000', '7.000', 'Rp 7.000', 'Rp7000'.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Amount is empty.")
    m = _AMOUNT_RE.match(text)
    if not m:
        raise ValueError("Amount must be a whole number in rupiah (no letters or symbols).")
    digits = m.group(1)

    # Reject explicitly decimal input (comma with 2 digits after, or a dot
    # followed by exactly 1-2 digits when a thousands separator would not apply).
    if re.search(r",\d{1,2}$", digits):
        raise ValueError("Amount must be a whole number (no decimals).")
    if re.search(r"\.\d{1,2}$", digits) and len(digits.replace(".", "")) <= 6:
        # e.g. "7000.50" -> decimal, not a thousands separator
        if not re.search(r"\.\d{3}$", digits):
            raise ValueError("Amount must be a whole number (no decimals).")

    clean = digits.replace(".", "").replace(",", "")
    if not clean.isdigit():
        raise ValueError("Amount must contain digits only.")
    amount = int(clean)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    if amount < bill.CUSTOM_MIN_AMOUNT:
        raise ValueError(
            f"Minimum custom top-up is Rp {bill.CUSTOM_MIN_AMOUNT:,}.".replace(",", ".")
        )
    if amount > bill.CUSTOM_MAX_AMOUNT:
        raise ValueError(
            f"Maximum custom top-up is Rp {bill.CUSTOM_MAX_AMOUNT:,}.".replace(",", ".")
        )
    return amount


def _access_allowed(user_id: int) -> bool:
    """Owner always bypasses; /freecaca ON gives everyone free access."""
    if int(user_id) in OWNER_ID:
        return True
    return bill.is_freecaca_enabled()


# ── Keyboards ────────────────────────────────────────────────────────────────

def _packages_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for price, pkg in sorted(bill.PRESET_PACKAGES.items()):
        rows.append([
            InlineKeyboardButton(
                f"Rp {price//1000}rb -> {_fmt_credits(pkg['total'])} credit / {pkg['days']}d",
                callback_data=f"cacabill:pkg:{price}",
            )
        ])
    rows.append([InlineKeyboardButton("Custom Amount & Duration", callback_data="cacabill:custom")])
    rows.append([InlineKeyboardButton("My Balance", callback_data="cacabill:bal")])
    rows.append([InlineKeyboardButton("Close", callback_data="cacabill:close:0")])
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(amount: int, days: int, is_custom: bool) -> InlineKeyboardMarkup:
    flag = "c" if is_custom else "p"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirm & Pay", callback_data=f"cacabill:ok:{flag}:{amount}:{days}")],
        [InlineKeyboardButton("Cancel", callback_data="cacabill:cancel")],
    ])


def _duration_keyboard(amount: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(f"{d} days", callback_data=f"cacabill:dur:{amount}:{d}")
            for d in bill.ALLOWED_CUSTOM_DAYS[:2]
        ],
        [
            InlineKeyboardButton(f"{d} days", callback_data=f"cacabill:dur:{amount}:{d}")
            for d in bill.ALLOWED_CUSTOM_DAYS[2:]
        ],
        [InlineKeyboardButton("Cancel", callback_data="cacabill:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


def _invoice_keyboard(short_id: str, qr_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Refresh Status", callback_data=f"cacabill:ref:{short_id}"),
            InlineKeyboardButton("Open Payment Page", url=qr_url),
        ],
        [InlineKeyboardButton("Cancel", callback_data=f"cacabill:can:{short_id}")],
    ])


# ── Text builders ────────────────────────────────────────────────────────────

def _packages_text(user_id: int) -> str:
    free = bill.is_freecaca_enabled()
    is_owner = int(user_id) in OWNER_ID
    head = [
        "<b>CACA TOP-UP</b>",
        "",
        "Credit is used to pay for Caca's AI usage by actual tokens.",
        "Every top-up creates its own credit lot with its own expiry date.",
        "",
        "<b>Packages</b>",
    ]
    if free:
        head.extend([
            "",
            "<i>Caca is currently FREE for everyone (/freecaca is ON). Top-up is optional.</i>",
        ])
    elif is_owner:
        head.extend([
            "",
            "<i>You are the OWNER — you have unlimited free access. Top-up is optional for testing.</i>",
        ])
    return "\n".join(head)


def _summary_text(amount: int, base: int, bonus: int, total: int, days: int, is_custom: bool) -> str:
    gross, fee = bill.calculate_gross_amount(amount)
    fee_pct = bill.GATEWAY_FEE_BPS / 100
    lines = [
        "<b>CONFIRM CACA TOP-UP</b>",
        "",
        f"<b>Package price:</b> <code>Rp {amount:,}</code>".replace(",", "."),
        f"<b>QRIS admin fee ({fee_pct:g}%):</b> <code>Rp {fee:,}</code>".replace(",", "."),
        f"<b>Total you pay:</b> <code>Rp {gross:,}</code>".replace(",", "."),
        "",
        f"<b>Base credit:</b> <code>{_fmt_credits(base)}</code>",
        f"<b>Bonus:</b> <code>{_fmt_credits(bonus)}</code>" if bonus else "<b>Bonus:</b> <code>0</code> (custom has no bonus)",
        f"<b>Total credit:</b> <code>{_fmt_credits(total)}</code>",
        f"<b>Validity:</b> <code>{days} days</code>",
        "",
        "<i>The QRIS admin fee is charged on top of the package price so your "
        "full package value is delivered.</i>",
        "<i>Expiry is counted from the moment your payment is verified, not from now.</i>",
    ]
    if is_custom:
        lines.insert(2, "<i>Custom package \u2014 no preset bonus applied.</i>")
    return "\n".join(lines)


def _invoice_caption(inv: dict, status: str = "PENDING", last_checked: float | None = None) -> str:
    now = time.time()
    remaining = max(0, int(inv["deadline"] - now))
    rem_m, rem_s = divmod(remaining, 60)
    base_amt = int(inv.get("base_amount") or inv["amount"])
    fee_amt = int(inv.get("gateway_fee") or 0)
    fee_bps = int(inv.get("fee_bps") or 0)
    rows = [
        "<b>CACA TOP-UP — QRIS INVOICE</b>",
        "",
        f"<b>Status:</b> <code>{status}</code>",
        f"<b>Package price:</b> <code>Rp {base_amt:,}</code>".replace(",", "."),
    ]
    if fee_amt:
        rows.append(
            f"<b>QRIS admin fee ({fee_bps / 100:g}%):</b> <code>Rp {fee_amt:,}</code>".replace(",", ".")
        )
    rows.extend([
        f"<b>Total to pay:</b> <code>Rp {inv['amount']:,}</code>".replace(",", "."),
        f"<b>Credit:</b> <code>{_fmt_credits(inv['credit_total'])}</code> (base {_fmt_credits(inv['credit_base'])}"
        + (f" + bonus {_fmt_credits(inv['credit_bonus'])})" if inv["credit_bonus"] else ")"),
        f"<b>Validity:</b> <code>{inv['duration_days']} days</code>",
        f"<b>QRIS ID:</b> <code>{html.escape(str(inv['qris_id']))}</code>",
        f"<b>Trx ID:</b> <code>{html.escape(str(inv['trx_id']))}</code>",
        f"<b>Expires in:</b> <code>{rem_m:02d}:{rem_s:02d}</code> (max 5 minutes)",
        f"<b>Last checked:</b> <code>{datetime.fromtimestamp(last_checked or now, tz=WIB).strftime('%H:%M:%S')} WIB</code>",
        "",
        "Scan the QR above with GoPay, OVO, DANA, ShopeePay, or any bank app.",
        "Credit is issued only after payment is verified.",
    ])
    return "\n".join(rows)


def _success_text(inv: dict, expires_at: float) -> str:
    base_amt = int(inv.get("base_amount") or inv["amount"])
    fee_amt = int(inv.get("gateway_fee") or 0)
    rows = [
        "<b>PAYMENT SUCCESSFUL</b>",
        "",
        f"<b>Credit added:</b> <code>{_fmt_credits(inv['credit_total'])}</code>",
        f"<b>Total paid:</b> <code>Rp {inv['amount']:,}</code>".replace(",", "."),
    ]
    if fee_amt:
        rows.append(
            f"<b>Package value:</b> <code>Rp {base_amt:,}</code>".replace(",", ".")
        )
    rows.extend([
        f"<b>Valid for:</b> <code>{inv['duration_days']} days</code>",
        f"<b>Expires at:</b> <code>{_fmt_wib(expires_at)}</code>",
        "",
        "You can keep chatting with Caca now.",
    ])
    return "\n".join(rows)


def _expired_text(inv: dict) -> str:
    return "\n".join([
        "<b>INVOICE EXPIRED</b>",
        "",
        f"<b>Trx ID:</b> <code>{html.escape(str(inv['trx_id']))}</code>",
        f"<b>Amount:</b> <code>Rp {inv['amount']:,}</code>".replace(",", "."),
        "",
        "The 5-minute payment window has ended. No credit was issued.",
    ])


def _balance_text(user_id: int) -> str:
    bal = bill.get_user_balance(user_id)
    is_owner = int(user_id) in OWNER_ID
    free = bill.is_freecaca_enabled()

    lines = ["<b>CACA CREDIT BALANCE</b>", ""]

    # Access status line: never show "0 credits / top up" to a user who already
    # has working access (owner or global free mode).
    if is_owner:
        lines.append("<b>Status:</b> <code>OWNER — UNLIMITED ACCESS</code>")
        lines.append("<b>Available:</b> <code>Unlimited</code> (owner is never charged)")
    elif free:
        lines.append("<b>Status:</b> <code>FREE ACCESS — ACTIVE</code>")
        lines.append("<b>Available:</b> <code>Unlimited while free access is on</code>")
    else:
        lines.append(f"<b>Available:</b> <code>{_fmt_credits(bal['available_credits'])}</code> credits")
        if bal["reserved_credits"]:
            lines.append(f"<b>In use (reserved):</b> <code>{_fmt_credits(bal['reserved_credits'])}</code> credits")
        if bal["expired_credits"]:
            lines.append(f"<b>Expired:</b> <code>{_fmt_credits(bal['expired_credits'])}</code> credits (unusable)")
    lines.append("")

    lots = bal["active_lots"]
    if not lots:
        if is_owner:
            lines.append("<i>No purchased credit lots. Owner access does not need any.</i>")
        elif free:
            lines.append("<i>No purchased credit lots. Free access is currently active.</i>")
        else:
            lines.append("<i>No active credit. Top up to keep chatting with Caca.</i>")
    else:
        lines.append("<b>Active credit lots</b>")
        for lot in lots[:10]:
            lines.append(
                f"• <code>{_fmt_credits(lot['credit_remaining'])}</code> cr "
                f"— expires in <b>{_fmt_days(lot['expires_at'])}</b> "
                f"({_fmt_wib(lot['expires_at'])})"
            )
        if len(lots) > 10:
            lines.append(f"<i>... and {len(lots) - 10} more lots</i>")
        lines.append("")
        lines.append("<i>Each lot expires on its own date. Credits are used from the soonest-expiring lot first.</i>")

    lines.extend(["", f"<b>Rate:</b> <code>Rp 1 = {bill.RATE_CREDIT_PER_RUPIAH} credits</code>",
                  f"<b>AI usage:</b> input {bill.RATE_INPUT_TOKEN} cr/token, output {bill.RATE_OUTPUT_TOKEN} cr/token"])
    return "\n".join(lines)


# ── Commands ─────────────────────────────────────────────────────────────────

async def cacatopup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/cacatopup` or `/buycaca`: show the top-up picker."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    if chat.type in ("group", "supergroup") or int(chat.id) < 0:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Open Top-up in DM", callback_data=f"cacabill:open:{user.id}")
        ]])
        return await msg.reply_text(
            "<b>Buy Caca Credit</b>\n\n"
            "To create your private QRIS invoice safely, continue in the bot's direct message.",
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

    sent = await msg.reply_text(_packages_text(user.id), parse_mode="HTML", reply_markup=_packages_keyboard())
    try:
        register_message_owner(chat.id, sent.message_id, user.id)
    except Exception as e:
        log.debug("register owner failed | err=%r", e)


async def cacasaldo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/cacasaldo`: show balance & lot expiries."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    sent = await msg.reply_text(_balance_text(user.id), parse_mode="HTML", reply_markup=_packages_keyboard())
    try:
        register_message_owner(msg.chat_id, sent.message_id, user.id)
    except Exception as e:
        log.debug("register owner failed | err=%r", e)


async def freecaca_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/freecaca enable|disable`: owner-only global free-access switch."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.id not in OWNER_ID:
        return

    args = [a.lower() for a in (context.args or [])]
    current = bill.is_freecaca_enabled()

    if not args or args[0] not in ("enable", "disable", "status"):
        return await msg.reply_text(
            "<b>Free Caca Access</b>\n\n"
            f"<b>Status:</b> <code>{'ENABLED' if current else 'DISABLED'}</code>\n\n"
            "<code>/freecaca enable</code> \u2014 free access for everyone\n"
            "<code>/freecaca disable</code> \u2014 back to paid credit mode\n"
            "<code>/freecaca status</code> \u2014 show current status\n\n"
            "<i>When disabled, existing credit lots are untouched \u2014 only free access stops.</i>",
            parse_mode="HTML",
        )

    if args[0] == "status":
        return await msg.reply_text(
            f"<b>Free Caca access:</b> <code>{'ENABLED' if current else 'DISABLED'}</code>",
            parse_mode="HTML",
        )

    enabled = args[0] == "enable"
    try:
        bill.set_freecaca_enabled(enabled)
    except Exception as e:
        log.error("Failed to set freecaca | err=%r", e)
        return await msg.reply_text("<b>Failed to update the setting.</b>", parse_mode="HTML")

    log.info("Freecaca %s by owner | user_id=%s", "ENABLED" if enabled else "DISABLED", user.id)
    return await msg.reply_text(
        f"<b>Free Caca access is now {'ENABLED' if enabled else 'DISABLED'}.</b>\n\n"
        + ("Everyone can chat with Caca without spending credits."
           if enabled else
           "Caca is back to paid credit mode. Existing credit lots are untouched."),
        parse_mode="HTML",
    )


async def providercaca_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/providercaca [shiteru|codebuddy]`: owner-only command to switch Caca's provider."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.id not in OWNER_ID:
        return

    from handlers.caca.caca import get_provider, set_provider
    from handlers.caca import codebuddy

    args = [a.lower().strip() for a in (context.args or [])]
    current = get_provider()

    if not args or args[0] not in ("shiteru", "codebuddy", "status"):
        cb_ready = "ready" if codebuddy.configured() else "no tokens in .env"
        shiteru_ready = "ready" if bool(os.getenv("SHITERU_KEY")) else "no SHITERU_KEY in .env"
        return await msg.reply_text(
            "<b>Caca Provider Switch</b>\n\n"
            f"<b>Active provider:</b> <code>{html.escape(current)}</code>\n\n"
            "<b>Commands:</b>\n"
            "<code>/providercaca codebuddy</code> \u2014 switch to CodeBuddy intl (deepseek-v4.1-flash, thinking off)\n"
            "<code>/providercaca shiteru</code> \u2014 switch to Shiteru\n"
            "<code>/providercaca status</code> \u2014 show current provider status\n\n"
            f"<b>Backend readiness:</b>\n"
            f"\u2022 CodeBuddy: <code>{cb_ready}</code>\n"
            f"\u2022 Shiteru: <code>{shiteru_ready}</code>\n",
            parse_mode="HTML",
        )

    if args[0] == "status":
        cb_ready = "ready" if codebuddy.configured() else "no tokens"
        shiteru_ready = "ready" if bool(os.getenv("SHITERU_KEY")) else "no key"
        return await msg.reply_text(
            f"<b>Caca active provider:</b> <code>{html.escape(current)}</code>\n"
            f"\u2022 CodeBuddy: <code>{cb_ready}</code>\n"
            f"\u2022 Shiteru: <code>{shiteru_ready}</code>",
            parse_mode="HTML",
        )

    target = args[0]
    if target == "codebuddy" and not codebuddy.configured():
        return await msg.reply_text(
            "<b>Cannot switch to CodeBuddy:</b> no tokens in <code>.env</code>.\n"
            "Set <code>CODEBUDDY_REFRESH_TOKEN</code> or <code>CODEBUDDY_ACCESS_TOKEN</code> first.",
            parse_mode="HTML",
        )
    if target == "shiteru" and not os.getenv("SHITERU_KEY"):
        return await msg.reply_text(
            "<b>Cannot switch to Shiteru:</b> <code>SHITERU_KEY</code> is not set in <code>.env</code>.",
            parse_mode="HTML",
        )

    ok = set_provider(target)
    if not ok:
        return await msg.reply_text("<b>Failed to save provider choice.</b>", parse_mode="HTML")

    log.info("Caca provider switched | user_id=%s provider=%s", user.id, target)
    extra = " (thinking: disabled, stream-only)" if target == "codebuddy" else ""
    return await msg.reply_text(
        f"<b>Caca provider changed to:</b> <code>{html.escape(target)}</code>{extra}\n\n"
        f"Every <code>/caca</code> call now routes through <b>{html.escape(target)}</b>.",
        parse_mode="HTML",
    )


async def addcredit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/addcredit [@user/id] <credits> [days]`: owner-only command to add credits.

    Supports:
      - Reply to user: `/addcredit 30000` (optional days: `/addcredit 30000 30`)
      - By username/id: `/addcredit @username 30000` (or `/addcredit 12345 30000 30`)
    Defaults duration to 30 days if omitted. Extends user's active lot if present.
    """
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.id not in OWNER_ID:
        return

    from handlers.moderation.helpers import (
        resolve_target_user_id,
        resolve_target_user_obj_for_display,
        resolve_user_obj_for_display_by_id,
        display_name,
        display_name_from_token,
        mention_html,
        reply_in_topic,
    )

    args = context.args or []
    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)

    target_token: str | None = None
    amount_token: str | None = None
    days_token: str | None = None

    if has_reply:
        if not args:
            return await reply_in_topic(
                msg,
                "<b>Add Caca Credit (via Reply)</b>\n\n"
                "Usage:\n"
                "<code>/addcredit &lt;credits&gt; [days]</code>\n\n"
                "Example: <code>/addcredit 300000</code>",
                parse_mode="HTML",
            )
        amount_token = args[0]
        if len(args) > 1:
            days_token = args[1]
    else:
        if len(args) < 2:
            return await reply_in_topic(
                msg,
                "<b>Add Caca Credit</b>\n\n"
                "Usage:\n"
                "• Reply: <code>/addcredit &lt;credits&gt; [days]</code>\n"
                "• Target: <code>/addcredit &lt;@username | user_id&gt; &lt;credits&gt; [days]</code>\n\n"
                "Examples:\n"
                "<code>/addcredit @username 300000</code>\n"
                "<code>/addcredit 123456789 300000 30</code>",
                parse_mode="HTML",
            )
        target_token = args[0]
        amount_token = args[1]
        if len(args) > 2:
            days_token = args[2]

    # Resolve target user
    target_id = await resolve_target_user_id(update, context, target_token)
    if not target_id:
        return await reply_in_topic(
            msg,
            "Could not resolve the target user. Send numeric ID or @username, or reply to their message.",
            parse_mode="HTML",
        )

    # Parse amount
    try:
        clean_amt = str(amount_token).replace(".", "").replace(",", "").strip()
        credits = int(clean_amt)
        if credits <= 0:
            raise ValueError
    except Exception:
        return await reply_in_topic(
            msg,
            "Invalid credit amount. Must be a positive integer, e.g. <code>300000</code>.",
            parse_mode="HTML",
        )

    # Parse duration
    days = 30
    if days_token:
        try:
            days = int(days_token)
            if days <= 0:
                raise ValueError
        except Exception:
            return await reply_in_topic(
                msg,
                "Invalid duration days. Must be positive, e.g. <code>30</code>.",
                parse_mode="HTML",
            )

    try:
        res = bill.admin_add_credit(
            admin_id=user.id,
            target_user_id=target_id,
            credits=credits,
            duration_days=days,
            reason="owner /addcredit",
            extend_latest_lot=True,
        )
    except Exception as e:
        log.error("Failed /addcredit | err=%r", e)
        return await reply_in_topic(msg, f"Failed to add credit: {html.escape(str(e))}")

    # Build display
    obj = await resolve_target_user_obj_for_display(update, context, target_token)
    if not obj:
        obj = await resolve_user_obj_for_display_by_id(update, context, int(target_id))
    name = display_name(obj) or display_name_from_token(target_token) or str(target_id)
    who = mention_html(int(target_id), name)

    bal = bill.get_user_balance(target_id)
    action_note = "added to existing lot" if res.get("extended_existing") else f"new lot (ID: {res.get('lot_id')})"

    log.info("/addcredit success | admin=%s target=%s credits=%s lot=%s",
             user.id, target_id, credits, res.get("lot_id"))

    return await reply_in_topic(
        msg,
        "<b>Credit Added</b>\n\n"
        f"<b>User:</b> {who} (<code>{target_id}</code>)\n"
        f"<b>Added:</b> <code>{_fmt_credits(credits)}</code> credits\n"
        f"<b>Validity:</b> <code>{days} days</code> ({action_note})\n"
        f"<b>Total balance now:</b> <code>{_fmt_credits(bal['available_credits'])}</code> credits",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cacaadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/cacaadmin [grant <uid> <credits> <days> <reason>]`: owner dashboard."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.id not in OWNER_ID:
        return

    args = context.args or []
    if args and args[0].lower() == "grant":
        if len(args) < 5:
            return await msg.reply_text(
                "<b>Usage:</b> <code>/cacaadmin grant &lt;user_id&gt; &lt;credits&gt; &lt;days&gt; &lt;reason&gt;</code>",
                parse_mode="HTML",
            )
        try:
            target = int(args[1])
            credits = int(args[2])
            days = int(args[3])
            reason = " ".join(args[4:]).strip() or "manual grant"
            if credits <= 0 or days <= 0:
                raise ValueError("credits/days must be positive")
        except ValueError:
            return await msg.reply_text("<b>Invalid numbers.</b>", parse_mode="HTML")

        try:
            lot_id = bill.admin_grant_credit(user.id, target, credits, days, reason)
        except Exception as e:
            log.error("Admin grant failed | err=%r", e)
            return await msg.reply_text("<b>Grant failed.</b>", parse_mode="HTML")

        return await msg.reply_text(
            "<b>Credit granted</b>\n\n"
            f"<b>User:</b> <code>{target}</code>\n"
            f"<b>Credits:</b> <code>{_fmt_credits(credits)}</code>\n"
            f"<b>Days:</b> <code>{days}</code>\n"
            f"<b>Lot ID:</b> <code>{lot_id}</code>\n"
            f"<b>Reason:</b> {html.escape(reason)}",
            parse_mode="HTML",
        )

    ov = bill.get_admin_overview()
    return await msg.reply_text(
        "<b>CACA BILLING OVERVIEW</b>\n\n"
        f"<b>Free access:</b> <code>{'ENABLED' if ov['freecaca_enabled'] else 'DISABLED'}</code>\n\n"
        "<b>Invoices</b>\n"
        f"• Total: <code>{ov['total_invoices']}</code>\n"
        f"• Paid: <code>{ov['paid_invoices']}</code>\n"
        f"• Pending: <code>{ov['pending_invoices']}</code>\n\n"
        "<b>Revenue (paid)</b>\n"
        f"• Gross (user paid): <code>Rp {ov['total_gross']:,}</code>\n".replace(",", ".") +
        f"• Net (you receive): <code>Rp {ov['total_net']:,}</code>\n".replace(",", ".") +
        f"• Gateway fee: <code>Rp {ov['total_fees']:,}</code>\n\n".replace(",", ".") +
        "<b>Credits</b>\n"
        f"• Issued: <code>{_fmt_credits(ov['total_credits_issued'])}</code>\n"
        f"• Used: <code>{_fmt_credits(ov['total_credits_used'])}</code>\n"
        f"• Billed (usage log): <code>{_fmt_credits(ov['total_billed_credits'])}</code>\n\n"
        "<b>Tokens</b>\n"
        f"• Total consumed: <code>{_fmt_credits(ov['total_tokens_consumed'])}</code>\n\n"
        "<b>Manual grant</b>\n"
        "<code>/cacaadmin grant &lt;user_id&gt; &lt;credits&gt; &lt;days&gt; &lt;reason&gt;</code>",
        parse_mode="HTML",
    )


# ── Custom flow text input ───────────────────────────────────────────────────

async def cacabill_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a user-typed custom amount while the custom flow is armed.

    Raises `ApplicationHandlerStop` when it consumes the message so no other
    text handler (Caca chat, auto-download, prefix routers) also reacts to it.
    """
    from telegram.ext import ApplicationHandlerStop

    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return

    flow = _CUSTOM_FLOW.get(int(chat.id))
    if not flow or flow.get("user_id") != int(user.id) or flow.get("stage") != "amount":
        return

    if time.time() - flow.get("created_at", 0) > CUSTOM_FLOW_TTL:
        _CUSTOM_FLOW.pop(int(chat.id), None)
        await msg.reply_text("<b>Custom top-up timed out.</b> Start again with /cacatopup.", parse_mode="HTML")
        raise ApplicationHandlerStop

    try:
        amount = parse_custom_amount(msg.text or "")
    except ValueError as e:
        await msg.reply_text(
            f"<b>Invalid amount</b>\n\n{html.escape(str(e))}\n\n"
            f"Send a whole rupiah amount between <code>{bill.CUSTOM_MIN_AMOUNT:,}</code> and "
            f"<code>{bill.CUSTOM_MAX_AMOUNT:,}</code>, or /cacatopup to restart.".replace(",", "."),
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    flow.update({"stage": "duration", "amount": amount, "created_at": time.time()})
    base = amount * bill.RATE_CREDIT_PER_RUPIAH
    sent = await msg.reply_text(
        "<b>Choose validity</b>\n\n"
        f"<b>Amount:</b> <code>Rp {amount:,}</code>\n".replace(",", ".") +
        f"<b>Credit:</b> <code>{_fmt_credits(base)}</code> (no preset bonus)\n\n"
        "Pick how long this credit lot stays valid.",
        parse_mode="HTML",
        reply_markup=_duration_keyboard(amount),
    )
    try:
        register_message_owner(chat.id, sent.message_id, user.id)
    except Exception as e:
        log.debug("register owner failed | err=%r", e)
    raise ApplicationHandlerStop


# ── Invoice lifecycle ────────────────────────────────────────────────────────

def _job_name(short_id: str) -> str:
    return f"poll_cacabill:{short_id}"


def _stop_polling(context: ContextTypes.DEFAULT_TYPE, short_id: str):
    jq = getattr(context.application, "job_queue", None)
    if not jq:
        return
    removed = False
    for job in jq.get_jobs_by_name(_job_name(short_id)):
        job.schedule_removal()
        removed = True
    if removed:
        _CHECK_LOCKS.pop(short_id, None)


async def _close_as_paid(context: ContextTypes.DEFAULT_TYPE, inv: dict):
    short_id = inv["short_id"]
    newly = bill.fulfill_payment(short_id)
    _stop_polling(context, short_id)

    lot_expiry = time.time() + int(inv["duration_days"]) * 86400
    try:
        await context.bot.edit_message_caption(
            chat_id=inv["chat_id"],
            message_id=inv["message_id"],
            caption=_success_text(inv, lot_expiry),
            parse_mode="HTML",
        )
    except Exception as e:
        log.debug("Edit success caption failed | short_id=%s err=%r", short_id, e)

    if newly:
        log.info("Caca credit issued | user_id=%s trx_id=%s credits=%s days=%s",
                 inv["user_id"], inv["trx_id"], inv["credit_total"], inv["duration_days"])


async def _close_as_expired(context: ContextTypes.DEFAULT_TYPE, inv: dict):
    short_id = inv["short_id"]
    bill.set_invoice_status(short_id, "EXPIRED")
    _stop_polling(context, short_id)
    try:
        await context.bot.edit_message_caption(
            chat_id=inv["chat_id"],
            message_id=inv["message_id"],
            caption=_expired_text(inv),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Top up again", callback_data="cacabill:new")]
            ]),
        )
    except Exception as e:
        log.debug("Edit expired caption failed | short_id=%s err=%r", short_id, e)


async def _poll_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    short_id = data.get("short_id")
    if not short_id:
        context.job.schedule_removal()
        return

    inv = bill.get_invoice(short_id)
    if not inv or inv["status"] in ("PAID", "EXPIRED", "CANCELLED"):
        context.job.schedule_removal()
        return

    now = time.time()
    if now >= inv["deadline"]:
        await _close_as_expired(context, inv)
        return

    async with _lock(short_id):
        fresh = bill.get_invoice(short_id)
        if not fresh or fresh["status"] != "PENDING":
            context.job.schedule_removal()
            return
        try:
            res = await axiom.check_payment(qris_id=fresh["qris_id"], trx_id=fresh["trx_id"])
        except Exception as e:
            log.warning("Caca payment poll failed, keeping status | short_id=%s err=%r", short_id, e)
            return
        if res.get("paid") or res.get("status") == "PAID":
            await _close_as_paid(context, fresh)
            return
        if res.get("status") == "EXPIRED":
            await _close_as_expired(context, fresh)
            return
        try:
            await context.bot.edit_message_caption(
                chat_id=fresh["chat_id"],
                message_id=fresh["message_id"],
                caption=_invoice_caption(fresh, "PENDING", now),
                parse_mode="HTML",
                reply_markup=_invoice_keyboard(short_id, f"{AXIOM_BASE_URL}/qr/{fresh['qris_id']}"),
            )
        except Exception:
            pass


async def _create_invoice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    chat_id: int,
    amount: int,
    credit_base: int,
    credit_bonus: int,
    credit_total: int,
    duration_days: int,
    is_custom: bool,
    status_msg=None,
):
    """Buat invoice QRIS. `status_msg` dipakai ulang kalau karti pesanan sudah
    diedit jadi teks 'creating' supaya TIDAK ada dua pesan status."""
    msg = update.effective_message
    if not msg and status_msg is None:
        return

    if not axiom.configured():
        target = status_msg or msg
        return await target.edit_text(
            "<b>Payment gateway is not configured.</b>\nPlease contact the bot owner.",
            parse_mode="HTML",
        ) if status_msg else await msg.reply_text(
            "<b>Payment gateway is not configured.</b>\nPlease contact the bot owner.",
            parse_mode="HTML",
        )

    # Gross-up: the QRIS amount is raised so that after the gateway fee is
    # deducted, the bot still receives exactly `amount` (the package price the
    # owner configured). The fee is passed through to the user.
    gross_amount, gateway_fee = bill.calculate_gross_amount(amount, bill.GATEWAY_FEE_BPS)

    if status_msg is None:
        status_msg = await msg.reply_text("Creating your QRIS invoice...", parse_mode="HTML")

    try:
        data = await axiom.create_qris(gross_amount)
    except Exception as e:
        log.error("Failed to create Caca QRIS | err=%r", e)
        try:
            return await status_msg.edit_text(
                "<b>Failed to create the QRIS payment.</b>\n"
                f"<code>{html.escape(str(e))}</code>",
                parse_mode="HTML",
            )
        except Exception:
            return

    # Cross-check: use the gateway-reported fee when present, else our gross-up.
    real_fee = int(data.get("fee_amount") or gateway_fee)
    real_rate = int(data.get("fee_rate_bps") or bill.GATEWAY_FEE_BPS)
    charged = int(data.get("amount") or gross_amount)

    now = time.time()
    short_id = secrets.token_hex(6)
    created_at = data.get("created_at") or now
    deadline = now + PAYMENT_DEADLINE_SEC

    bill.save_invoice(
        short_id=short_id,
        qris_id=data["qris_id"],
        trx_id=data.get("trx_id") or f"CAC-{short_id.upper()}",
        user_id=user_id,
        chat_id=chat_id,
        amount=charged,
        base_amount=amount,
        gateway_fee=real_fee,
        fee_bps=real_rate,
        credit_base=credit_base,
        credit_bonus=credit_bonus,
        credit_total=credit_total,
        duration_days=duration_days,
        is_custom=is_custom,
        created_at=created_at,
        deadline=deadline,
    )

    inv = bill.get_invoice(short_id)
    caption = _invoice_caption(inv, "PENDING", now)
    markup = _invoice_keyboard(short_id, data["qr_url"])

    photo_input = None
    if data.get("qris_string"):
        try:
            photo_input = io.BytesIO(_render_qr_png(data["qris_string"]))
        except Exception as e:
            log.warning("Local QR render failed | err=%r", e)
    if photo_input is None:
        photo_input = f"{AXIOM_BASE_URL}/qr/{data['qris_id']}?format=raw"

    sent = None
    try:
        sent = await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo_input,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup,
        )
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        log.warning("send_photo failed, fallback to text | err=%r", e)
        try:
            sent = await status_msg.edit_text(
                f"<b>QR page:</b> {data['qr_url']}\n\n{caption}",
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=False,
            )
        except Exception as e2:
            log.error("Failed to send Caca payment message | err=%r", e2)
            return

    if sent:
        bill.set_invoice_message_id(short_id, sent.message_id)
        jq = getattr(context.application, "job_queue", None)
        if jq:
            for job in jq.get_jobs_by_name(_job_name(short_id)):
                job.schedule_removal()
            jq.run_repeating(
                _poll_job,
                interval=POLL_INTERVAL_SEC,
                first=POLL_INTERVAL_SEC,
                name=_job_name(short_id),
                data={"short_id": short_id, "chat_id": chat_id, "message_id": sent.message_id, "user_id": user_id},
            )


# ── Callbacks ────────────────────────────────────────────────────────────────

async def cacabill_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    user = update.effective_user
    if not user:
        return

    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "open":
        target = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if int(user.id) != target:
            return await q.answer("This is not your button.", show_alert=True)
        me = await context.bot.get_me()
        return await q.answer(url=f"https://t.me/{me.username or ''}?start=buycaca")

    if action in ("new", "menu"):
        await q.answer()
        chat = update.effective_chat
        if chat and (chat.type in ("group", "supergroup") or int(chat.id) < 0):
            return await q.edit_message_text(
                "<b>Buy Caca Credit</b>\n\nOpen the bot in DM to top up: /cacatopup",
                parse_mode="HTML",
            )
        try:
            await q.edit_message_text(_packages_text(user.id), parse_mode="HTML", reply_markup=_packages_keyboard())
        except Exception:
            sent = await context.bot.send_message(
                chat_id=chat.id, text=_packages_text(user.id),
                parse_mode="HTML", reply_markup=_packages_keyboard(),
            )
            register_message_owner(chat.id, sent.message_id, user.id)
        return

    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            pass
        return await q.answer()

    if action == "cancel":
        chat = update.effective_chat
        if chat:
            _CUSTOM_FLOW.pop(int(chat.id), None)
        await q.answer("Cancelled.")
        try:
            await q.edit_message_text("<b>Top-up cancelled.</b> No invoice was created.", parse_mode="HTML")
        except Exception:
            pass
        return

    # Everything below must be the menu owner's click
    if not await guard_callback_owner(q, expected_user_id=user.id):
        return

    if action == "bal":
        await q.answer()
        try:
            await q.edit_message_text(_balance_text(user.id), parse_mode="HTML", reply_markup=_packages_keyboard())
        except Exception as e:
            log.debug("balance edit failed | err=%r", e)
        return

    if action == "custom":
        chat = update.effective_chat
        if not chat:
            return
        if chat.type in ("group", "supergroup") or int(chat.id) < 0:
            return await q.answer("Custom top-up must be done in private chat.", show_alert=True)
        _CUSTOM_FLOW[int(chat.id)] = {
            "user_id": int(user.id),
            "stage": "amount",
            "created_at": time.time(),
        }
        await q.answer()
        try:
            await q.edit_message_text(
                "<b>CUSTOM TOP-UP</b>\n\n"
                f"Send the top-up amount in rupiah (whole number).\n\n"
                f"<b>Minimum:</b> <code>Rp {bill.CUSTOM_MIN_AMOUNT:,}</code>\n".replace(",", ".") +
                f"<b>Maximum:</b> <code>Rp {bill.CUSTOM_MAX_AMOUNT:,}</code>\n".replace(",", ".") +
                f"<b>Rate:</b> <code>Rp 1 = {bill.RATE_CREDIT_PER_RUPIAH} credit</code>\n\n"
                "<i>Custom packages never receive preset bonuses.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cacabill:cancel")]]),
            )
        except Exception as e:
            log.debug("custom prompt edit failed | err=%r", e)
        return

    if action == "pkg":
        if len(parts) < 3:
            return await q.answer()
        try:
            price = int(parts[2])
        except ValueError:
            return await q.answer("Invalid package.", show_alert=True)
        pkg = bill.calculate_preset_package(price)
        if not pkg:
            return await q.answer("Unknown package.", show_alert=True)
        await q.answer()
        try:
            await q.edit_message_text(
                _summary_text(pkg["price"], pkg["base"], pkg["bonus"], pkg["total"], pkg["days"], False),
                parse_mode="HTML",
                reply_markup=_confirm_keyboard(pkg["price"], pkg["days"], False),
            )
        except Exception as e:
            log.debug("package summary edit failed | err=%r", e)
        return

    if action == "dur":
        if len(parts) < 4:
            return await q.answer()
        try:
            amount = int(parts[2])
            days = int(parts[3])
        except ValueError:
            return await q.answer("Invalid selection.", show_alert=True)
        try:
            pkg = bill.calculate_custom_package(amount, days)
        except ValueError as e:
            return await q.answer(str(e), show_alert=True)
        await q.answer()
        try:
            await q.edit_message_text(
                _summary_text(pkg["price"], pkg["base"], pkg["bonus"], pkg["total"], pkg["days"], True),
                parse_mode="HTML",
                reply_markup=_confirm_keyboard(pkg["price"], pkg["days"], True),
            )
        except Exception as e:
            log.debug("custom summary edit failed | err=%r", e)
        return

    if action == "ok":
        # Confirm & pay. ALWAYS re-validate server-side; never trust callback text.
        if len(parts) < 5:
            return await q.answer("Invalid order.", show_alert=True)
        kind, amount_s, days_s = parts[2], parts[3], parts[4]
        try:
            amount = int(amount_s)
            days = int(days_s)
        except ValueError:
            return await q.answer("Invalid order.", show_alert=True)

        if kind == "p":
            pkg = bill.calculate_preset_package(amount)
            if not pkg:
                return await q.answer("Unknown package.", show_alert=True)
            # Preset must keep its own canonical duration
            if days != pkg["days"]:
                return await q.answer("Package duration mismatch.", show_alert=True)
            base, bonus, total, duration = pkg["base"], pkg["bonus"], pkg["total"], pkg["days"]
            is_custom = False
        elif kind == "c":
            try:
                pkg = bill.calculate_custom_package(amount, days)
            except ValueError as e:
                return await q.answer(str(e), show_alert=True)
            base, bonus, total, duration = pkg["base"], pkg["bonus"], pkg["total"], pkg["days"]
            is_custom = True
        else:
            return await q.answer("Invalid order kind.", show_alert=True)

        chat = update.effective_chat
        if not chat:
            return await q.answer()
        if chat.type in ("group", "supergroup") or int(chat.id) < 0:
            return await q.answer("Payment must be created in private chat.", show_alert=True)

        _CUSTOM_FLOW.pop(int(chat.id), None)
        await q.answer()
        status_msg = None
        try:
            status_msg = await q.edit_message_text("<b>Creating your QRIS invoice...</b>", parse_mode="HTML")
        except Exception:
            pass
        return await _create_invoice(
            update, context,
            user_id=int(user.id), chat_id=int(chat.id),
            amount=amount, credit_base=base, credit_bonus=bonus,
            credit_total=total, duration_days=duration, is_custom=is_custom,
            status_msg=status_msg,
        )

    if action in ("ref", "can"):
        if len(parts) < 3:
            return await q.answer()
        short_id = parts[2]
        inv = bill.get_invoice(short_id)
        if not inv:
            return await q.answer("Invoice not found.", show_alert=True)
        if int(user.id) != int(inv["user_id"]):
            return await q.answer("This payment does not belong to you.", show_alert=True)

        if action == "can":
            _stop_polling(context, short_id)
            if inv["status"] == "PENDING":
                bill.set_invoice_status(short_id, "CANCELLED")
            await q.answer("Invoice cancelled.")
            try:
                await q.edit_message_caption(
                    caption="<b>PAYMENT CANCELLED</b>\n\nNo credit was issued.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        if inv["status"] == "PAID":
            return await q.answer("Payment already confirmed. Credit issued.", show_alert=True)

        now = time.time()
        if now >= inv["deadline"]:
            await _close_as_expired(context, inv)
            return await q.answer("Invoice expired.", show_alert=True)

        async with _lock(short_id):
            try:
                res = await axiom.check_payment(qris_id=inv["qris_id"], trx_id=inv["trx_id"])
            except Exception as e:
                log.warning("Manual Caca check failed | short_id=%s err=%r", short_id, e)
                return await q.answer(f"Could not check the payment status: {e}", show_alert=True)

            if res.get("paid") or res.get("status") == "PAID":
                await _close_as_paid(context, inv)
                return await q.answer("Payment received. Credit has been added.", show_alert=True)
            if res.get("status") == "EXPIRED":
                await _close_as_expired(context, inv)
                return await q.answer("Invoice expired.", show_alert=True)

            remaining = max(0, int(inv["deadline"] - now))
            rem_m, rem_s = divmod(remaining, 60)
            try:
                await q.edit_message_caption(
                    caption=_invoice_caption(inv, "PENDING", now),
                    parse_mode="HTML",
                    reply_markup=_invoice_keyboard(short_id, f"{AXIOM_BASE_URL}/qr/{inv['qr_id'] if inv.get('qr_id') else inv['qris_id']}"),
                )
            except Exception:
                pass
            return await q.answer(f"Status: PENDING. Time left {rem_m:02d}:{rem_s:02d}.", show_alert=False)

    return await q.answer()
