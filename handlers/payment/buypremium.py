"""Command /buypremium & Axiom QRIS payment handler.

FLOW:
1. User types `/buypremium` in a group/supergroup:
   - Bot replies with an inline button pointing to direct PM:
     `https://t.me/<bot_username>?start=buypremium`.
2. User types `/buypremium` in private chat OR clicks the deep link:
   - If the user is already premium, notify them and stop.
   - If the user already has a PENDING invoice still inside its 5-minute
     deadline, resend that invoice instead of creating a duplicate bill.
   - Otherwise call `utils.axiom.create_qris(amount=AXIOM_PREMIUM_AMOUNT)`
     (default Rp 20.000) and render the QRIS payload to PNG locally.
   - Save the invoice in `data/payments.sqlite3` with a hard 5-minute deadline.
   - Send the QR photo with a caption (status, amount, IDs, fee, net, created,
     live countdown) and inline buttons: Refresh / Open page / Cancel.
3. Auto-poll every 15 seconds via `job_queue.run_repeating`:
   - Past the 5-minute deadline -> mark EXPIRED, close the job.
   - Gateway reports PAID -> activate premium, close the job, send the success
     card and a private confirmation message.
   - Still PENDING -> refresh the countdown in the caption.
   - Gateway errors (429/503/network) -> keep the last known status.
4. Manual Refresh button: validates ownership, checks the gateway immediately.
5. Cancel button: stops polling and marks the invoice CANCELLED.
"""

import html
import io
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import qrcode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import payment_db, premium
from utils import axiom
from utils.config import AXIOM_BASE_URL, AXIOM_PREMIUM_AMOUNT, OWNER_ID

log = logging.getLogger(__name__)

PAYMENT_DEADLINE_SEC = 300  # 5-minute payment window
POLL_INTERVAL_SEC = 15      # auto refresh interval
WIB = timezone(timedelta(hours=7))

_CHECK_LOCKS: dict[str, "asyncio.Lock"] = {}


def _get_lock(short_id: str):
    import asyncio
    lock = _CHECK_LOCKS.get(short_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHECK_LOCKS[short_id] = lock
    return lock


def _format_time_wib(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=WIB).strftime("%H:%M:%S")


def _format_datetime_wib(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=WIB).strftime("%Y-%m-%d %H:%M:%S")


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


def _build_caption(
    *,
    status: str,
    amount: int,
    qris_id: str,
    trx_id: str,
    fee_amount: int,
    net_amount: int,
    created_at: float,
    deadline: float,
    last_checked: float | None = None,
) -> str:
    now = time.time()
    remaining = max(0, int(deadline - now))
    rem_m, rem_s = divmod(remaining, 60)

    return "\n".join([
        "<b>BUY PREMIUM - QRIS INVOICE</b>",
        "",
        f"<b>Status:</b> <code>{status}</code>",
        f"<b>Amount:</b> <code>Rp {amount:,}</code>",
        f"<b>QRIS ID:</b> <code>{html.escape(qris_id)}</code>",
        f"<b>Trx ID:</b> <code>{html.escape(trx_id)}</code>",
        f"<b>Platform fee (1%):</b> <code>Rp {fee_amount:,}</code>",
        f"<b>Net amount:</b> <code>Rp {net_amount:,}</code>",
        f"<b>Created:</b> <code>{_format_datetime_wib(created_at)} WIB</code>",
        f"<b>Expires in:</b> <code>{rem_m:02d}:{rem_s:02d}</code> (max 5 minutes)",
        f"<b>Last checked:</b> <code>{_format_time_wib(last_checked or now)} WIB</code>",
        "",
        "Scan the QR above with GoPay, OVO, DANA, ShopeePay, or any bank app.",
        "The bot refreshes the status automatically every 15 seconds.",
    ])


def _build_keyboard(short_id: str, qr_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Refresh Status", callback_data=f"buyprem:ref:{short_id}"),
            InlineKeyboardButton("Open Payment Page", url=qr_url),
        ],
        [
            InlineKeyboardButton("Cancel", callback_data=f"buyprem:can:{short_id}"),
        ],
    ])


def _build_expired_caption(amount: int, trx_id: str) -> str:
    return "\n".join([
        "<b>PAYMENT EXPIRED</b>",
        "",
        "<b>Status:</b> <code>EXPIRED</code>",
        f"<b>Trx ID:</b> <code>{html.escape(trx_id)}</code>",
        f"<b>Amount:</b> <code>Rp {amount:,}</code>",
        "",
        "The 5-minute payment window has ended.",
        "Use /buypremium to create a new invoice.",
    ])


def _build_success_caption(amount: int, trx_id: str) -> str:
    return "\n".join([
        "<b>PAYMENT SUCCESSFUL</b>",
        "",
        "<b>Status:</b> <code>PAID</code>",
        f"<b>Trx ID:</b> <code>{html.escape(trx_id)}</code>",
        f"<b>Amount:</b> <code>Rp {amount:,}</code>",
        "",
        "<b>Your account is now PREMIUM.</b>",
        "You have unlimited downloads and full access to every premium feature.",
        "Use /premiumbenefit to see the full list.",
    ])


def _success_pm_text() -> str:
    return (
        "<b>Payment successful</b>\n\n"
        "Thank you for your purchase. Your account is now <b>PREMIUM</b>.\n"
        "Use /premiumbenefit to see every premium benefit."
    )


def _job_name(short_id: str) -> str:
    return f"poll_payment:{short_id}"


def _stop_polling_job(context: ContextTypes.DEFAULT_TYPE, short_id: str):
    jq = getattr(context.application, "job_queue", None)
    if not jq:
        return
    for job in jq.get_jobs_by_name(_job_name(short_id)):
        job.schedule_removal()


def _schedule_polling_job(app, short_id: str, chat_id: int, message_id: int, user_id: int):
    jq = getattr(app, "job_queue", None)
    if not jq:
        return
    name = _job_name(short_id)
    for job in jq.get_jobs_by_name(name):
        job.schedule_removal()

    jq.run_repeating(
        _poll_payment_job,
        interval=POLL_INTERVAL_SEC,
        first=POLL_INTERVAL_SEC,
        name=name,
        data={
            "short_id": short_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "user_id": user_id,
        },
    )


async def _close_as_paid(context: ContextTypes.DEFAULT_TYPE, inv: dict):
    """Idempotent: mark PAID, grant premium, update card, notify the user."""
    short_id = inv["short_id"]
    user_id = int(inv["user_id"])

    payment_db.mark_paid(short_id)
    premium.premium_add(user_id)
    _stop_polling_job(context, short_id)

    try:
        await context.bot.edit_message_caption(
            chat_id=inv["chat_id"],
            message_id=inv["message_id"],
            caption=_build_success_caption(inv["amount"], inv["trx_id"]),
            parse_mode="HTML",
        )
    except Exception as e:
        log.debug("Failed to edit success caption | short_id=%s err=%r", short_id, e)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=_success_pm_text(),
            parse_mode="HTML",
        )
    except Exception as e:
        log.debug("Failed to send success PM | user_id=%s err=%r", user_id, e)

    log.info("Premium payment success | user_id=%s trx_id=%s amount=%s",
             user_id, inv["trx_id"], inv["amount"])


async def _close_as_expired(context: ContextTypes.DEFAULT_TYPE, inv: dict):
    short_id = inv["short_id"]
    payment_db.set_status(short_id, "EXPIRED")
    _stop_polling_job(context, short_id)
    try:
        await context.bot.edit_message_caption(
            chat_id=inv["chat_id"],
            message_id=inv["message_id"],
            caption=_build_expired_caption(inv["amount"], inv["trx_id"]),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Buy Premium Again", callback_data="buyprem:new")]
            ]),
        )
    except Exception as e:
        log.debug("Failed to edit expired caption | short_id=%s err=%r", short_id, e)


async def _poll_payment_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    short_id = data.get("short_id")
    if not short_id:
        context.job.schedule_removal()
        return

    inv = payment_db.get_by_short(short_id)
    if not inv or inv["status"] in ("PAID", "EXPIRED", "CANCELLED"):
        context.job.schedule_removal()
        return

    now = time.time()
    if now >= inv["deadline"]:
        await _close_as_expired(context, inv)
        return

    async with _get_lock(short_id):
        fresh = payment_db.get_by_short(short_id)
        if not fresh or fresh["status"] != "PENDING":
            context.job.schedule_removal()
            return

        try:
            res = await axiom.check_payment(qris_id=fresh["qris_id"], trx_id=fresh["trx_id"])
        except Exception as e:
            log.warning("Payment poll failed, keeping last status | short_id=%s err=%r", short_id, e)
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
                caption=_build_caption(
                    status="PENDING",
                    amount=fresh["amount"],
                    qris_id=fresh["qris_id"],
                    trx_id=fresh["trx_id"],
                    fee_amount=fresh["fee_amount"],
                    net_amount=fresh["net_amount"],
                    created_at=fresh["created_at"],
                    deadline=fresh["deadline"],
                    last_checked=now,
                ),
                parse_mode="HTML",
                reply_markup=_build_keyboard(short_id, f"{AXIOM_BASE_URL}/qr/{fresh['qris_id']}"),
            )
        except Exception:
            # Telegram rejects a caption edit when the text is unchanged.
            pass


async def buypremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /buypremium in groups and private chat."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    # Group / supergroup: route the user to a private chat for privacy.
    if chat.type in ("group", "supergroup") or int(chat.id) < 0:
        bot_user = await context.bot.get_me()
        bot_username = bot_user.username or ""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "Buy Premium via QRIS",
                url=f"https://t.me/{bot_username}?start=buypremium",
            )]
        ])
        return await msg.reply_text(
            "<b>Buy Premium</b>\n\n"
            "To create your private QRIS invoice safely, continue in the bot's direct message.\n\n"
            "Tap the button below to start.",
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    await _start_buypremium_pm(update, context, user_id=user.id, chat_id=chat.id)


async def buypremium_pm_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the deep link `/start buypremium`."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    await _start_buypremium_pm(update, context, user_id=user.id, chat_id=chat.id)


async def _resend_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, inv: dict) -> int | None:
    """Resend an existing invoice card. Returns the new message_id."""
    short_id = inv["short_id"]
    qr_url = f"{AXIOM_BASE_URL}/qr/{inv['qris_id']}"
    caption = _build_caption(
        status="PENDING",
        amount=inv["amount"],
        qris_id=inv["qris_id"],
        trx_id=inv["trx_id"],
        fee_amount=inv["fee_amount"],
        net_amount=inv["net_amount"],
        created_at=inv["created_at"],
        deadline=inv["deadline"],
        last_checked=time.time(),
    )
    markup = _build_keyboard(short_id, qr_url)
    raw_qr = f"{qr_url}?format=raw"

    try:
        sent = await context.bot.send_photo(
            chat_id=chat_id,
            photo=raw_qr,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return sent.message_id
    except Exception as e:
        log.debug("Resend photo failed, falling back to text | err=%r", e)

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=f"<b>QR page:</b> {qr_url}\n\n{caption}",
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=False,
        )
        return sent.message_id
    except Exception as e:
        log.error("Failed to resend invoice | short_id=%s err=%r", short_id, e)
        return None


async def _start_buypremium_pm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    chat_id: int,
):
    msg = update.effective_message
    if not msg:
        return

    if premium.is_premium(user_id):
        extra = " (Owner)" if user_id in OWNER_ID else ""
        return await msg.reply_text(
            f"<b>Your account is already PREMIUM{extra}.</b>\n\n"
            "You already have unlimited access to every feature.\n"
            "Use /premiumbenefit to see the full list.",
            parse_mode="HTML",
        )

    if not axiom.configured():
        return await msg.reply_text(
            "<b>Payment gateway is not configured.</b>\n"
            "Please contact the bot owner to enable payments.",
            parse_mode="HTML",
        )

    now = time.time()
    existing = payment_db.active_for_user(user_id)
    if existing and (existing["deadline"] - now) > 20:
        message_id = await _resend_invoice(context, chat_id, existing)
        if message_id:
            payment_db.set_message_id(existing["short_id"], message_id)
            _schedule_polling_job(context.application, existing["short_id"], chat_id, message_id, user_id)
        return

    status_msg = await msg.reply_text("Creating your QRIS invoice...", parse_mode="HTML")

    try:
        data = await axiom.create_qris(AXIOM_PREMIUM_AMOUNT)
    except Exception as e:
        log.error("Failed to create QRIS invoice | err=%r", e)
        return await status_msg.edit_text(
            "<b>Failed to create the QRIS payment.</b>\n"
            f"<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )

    short_id = secrets.token_hex(6)
    created_at = data.get("created_at") or now
    deadline = now + PAYMENT_DEADLINE_SEC

    payment_db.save_invoice(
        short_id=short_id,
        qris_id=data["qris_id"],
        trx_id=data.get("trx_id") or f"AXP-{short_id.upper()}",
        user_id=user_id,
        chat_id=chat_id,
        amount=data["amount"],
        fee_amount=data.get("fee_amount") or 0,
        net_amount=data.get("net_amount") or data["amount"],
        created_at=created_at,
        provider_expires_at=data.get("expires_at"),
        deadline=deadline,
    )

    caption = _build_caption(
        status="PENDING",
        amount=data["amount"],
        qris_id=data["qris_id"],
        trx_id=data.get("trx_id") or "-",
        fee_amount=data.get("fee_amount") or 0,
        net_amount=data.get("net_amount") or data["amount"],
        created_at=created_at,
        deadline=deadline,
        last_checked=now,
    )
    markup = _build_keyboard(short_id, data["qr_url"])

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
        log.warning("send_photo failed, falling back to text | err=%r", e)
        try:
            sent = await status_msg.edit_text(
                f"<b>QR page:</b> {data['qr_url']}\n\n{caption}",
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=False,
            )
        except Exception as e2:
            log.error("Failed to send payment message | err=%r", e2)
            return

    if sent:
        payment_db.set_message_id(short_id, sent.message_id)
        _schedule_polling_job(context.application, short_id, chat_id, sent.message_id, user_id)


async def buyprem_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline buttons: refresh status, cancel, buy again."""
    q = update.callback_query
    if not q or not q.data:
        return
    user = update.effective_user
    if not user:
        return

    parts = q.data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]

    if action == "new":
        await q.answer()
        return await _start_buypremium_pm(
            update, context, user_id=user.id, chat_id=update.effective_chat.id
        )

    if len(parts) < 3:
        return
    short_id = parts[2]

    inv = payment_db.get_by_short(short_id)
    if not inv:
        return await q.answer("Invoice data not found.", show_alert=True)

    if int(user.id) != int(inv["user_id"]):
        return await q.answer("This payment does not belong to you.", show_alert=True)

    if action == "can":
        _stop_polling_job(context, short_id)
        payment_db.set_status(short_id, "CANCELLED")
        try:
            await q.edit_message_caption(
                caption=(
                    "<b>PAYMENT CANCELLED</b>\n\n"
                    f"<b>Trx ID:</b> <code>{html.escape(inv['trx_id'])}</code>\n"
                    f"<b>Amount:</b> <code>Rp {inv['amount']:,}</code>\n\n"
                    "This invoice has been cancelled."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Buy Premium", callback_data="buyprem:new")]
                ]),
            )
        except Exception:
            pass
        return await q.answer("Invoice cancelled.", show_alert=False)

    if action != "ref":
        return

    if inv["status"] == "PAID":
        return await q.answer("Payment already confirmed. Premium is active.", show_alert=True)

    now = time.time()
    if now >= inv["deadline"]:
        await _close_as_expired(context, inv)
        return await q.answer("Invoice expired.", show_alert=True)

    async with _get_lock(short_id):
        try:
            res = await axiom.check_payment(qris_id=inv["qris_id"], trx_id=inv["trx_id"])
        except Exception as e:
            log.warning("Manual payment check failed | short_id=%s err=%r", short_id, e)
            return await q.answer(f"Could not check the payment status: {e}", show_alert=True)

        if res.get("paid") or res.get("status") == "PAID":
            await _close_as_paid(context, inv)
            return await q.answer("Payment received. Premium is now active.", show_alert=True)

        if res.get("status") == "EXPIRED":
            await _close_as_expired(context, inv)
            return await q.answer("Invoice expired.", show_alert=True)

        remaining = max(0, int(inv["deadline"] - now))
        rem_m, rem_s = divmod(remaining, 60)
        try:
            await q.edit_message_caption(
                caption=_build_caption(
                    status="PENDING",
                    amount=inv["amount"],
                    qris_id=inv["qris_id"],
                    trx_id=inv["trx_id"],
                    fee_amount=inv["fee_amount"],
                    net_amount=inv["net_amount"],
                    created_at=inv["created_at"],
                    deadline=inv["deadline"],
                    last_checked=now,
                ),
                parse_mode="HTML",
                reply_markup=_build_keyboard(short_id, f"{AXIOM_BASE_URL}/qr/{inv['qris_id']}"),
            )
        except Exception:
            pass

        return await q.answer(f"Status: PENDING. Time left {rem_m:02d}:{rem_s:02d}.", show_alert=False)
