import asyncio
import io
import logging
import math
import time
from telegram.ext import ContextTypes

import qrcode
from database import vps_db
from handlers.payment import notify as pay_notify
from utils import nasionalpay

from . import aws, notify, render
from .rich import send_rich
from .constants import (
    DEFAULT_REGION,
    FEE_BPS,
    POLL_INTERVAL_SEC,
    WARRANTY_DAYS,
    get_os,
    get_tier,
    normalize_tier,
)
from .state import clear_provisioning, drop_lock, get_lock, is_provisioning, mark_provisioning

log = logging.getLogger(__name__)


def calculate_gross_amount(base: int, fee_bps: int = FEE_BPS) -> int:
    denom = 10000 - fee_bps
    if denom <= 0:
        return base
    return int(math.ceil(base * 10000 / denom))


def qr_url_for(inv: dict) -> str:
    return str(inv.get("qr_url") or inv.get("qris_id") or "")


async def create_payment_qris(amount: int, user_id: int, *, tier: str = "", days: int = 0) -> dict:
    try:
        data = await nasionalpay.create_qris(amount, username=str(user_id))
    except Exception as e:
        log.error(
            "VPS payment create failed | tier=%s days=%s amount=%s user=%s err=%r",
            tier, days, amount, user_id, e,
        )
        raise
    log.info(
        "VPS payment created | trx=%s amount=%s fee=%s net=%s tier=%s days=%s user=%s",
        data.get("trx_id"), data.get("amount"), data.get("fee_amount"),
        data.get("net_amount"), tier, days, user_id,
    )
    return data


async def check_payment(inv: dict) -> dict:
    short_id = inv.get("short_id") or ""
    res = await nasionalpay.check_payment(trx_id=inv.get("trx_id") or inv.get("qris_id"))
    log.info(
        "VPS payment check | short_id=%s trx=%s status=%s amount=%s",
        short_id, inv.get("trx_id"), res.get("status"), res.get("amount"),
    )
    return res


def _vps_details(inv: dict) -> dict:
    t = get_tier(inv.get("tier")) or {}
    o = get_os(inv.get("os_image")) or {}
    return {
        "Plan": t.get("label") or normalize_tier(inv.get("tier")),
        "Duration": f"{inv.get('days')} days",
        "Region": inv.get("region") or "-",
        "Operating System": o.get("label") or inv.get("os_image"),
        "Reference": inv.get("short_id"),
    }


async def notify_owner(bot, inv: dict, event: str, **extra):
    try:
        details = _vps_details(inv)
        details.update(extra)
        await pay_notify.notify_owner_event(
            bot,
            "vps",
            event,
            user_id=inv.get("user_id"),
            trx_id=inv.get("trx_id"),
            amount=inv.get("gross_amount"),
            base_amount=inv.get("base_amount"),
            fee_amount=max(0, int(inv.get("gross_amount") or 0) - int(inv.get("base_amount") or 0)),
            details=details,
        )
    except Exception as e:
        log.warning("VPS owner notify failed | short_id=%s event=%s err=%r", inv.get("short_id"), event, e)


def render_qr_png(payload: str) -> bytes:
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


def _job_name(short_id: str) -> str:
    return f"poll_vps_payment:{short_id}"


_REGION_LIST_CACHE: dict[str, object] = {"ts": 0.0, "regions": []}
_REGION_LIST_TTL = 3600.0


async def get_available_regions() -> list[str]:
    now = time.time()
    cached = _REGION_LIST_CACHE.get("regions") or []
    ts = float(_REGION_LIST_CACHE.get("ts") or 0.0)
    if cached and (now - ts) < _REGION_LIST_TTL:
        return list(cached)
    try:
        regions = await asyncio.to_thread(aws.list_regions)
    except Exception as e:
        log.warning("List AWS regions failed, using last known | err=%r", e)
        return list(cached)
    if not regions:
        return list(cached)
    _REGION_LIST_CACHE["regions"] = regions
    _REGION_LIST_CACHE["ts"] = now
    return list(regions)


SCAN_CACHE_TTL = 300.0


def scan_is_cached() -> bool:
    from . import state

    cached, ts = state.get_scan_cache()
    return bool(cached) and (time.time() - ts) < SCAN_CACHE_TTL


async def get_account_scan(force: bool = False) -> tuple[dict, bool]:
    from . import state

    cached, ts = state.get_scan_cache()
    now = time.time()
    if not force and cached and (now - ts) < SCAN_CACHE_TTL:
        return cached, False

    async with state.get_scan_lock():
        cached, ts = state.get_scan_cache()
        now = time.time()
        if not force and cached and (now - ts) < SCAN_CACHE_TTL:
            return cached, False
        scan = await aws.scan_account_instances()
        state.set_scan_cache(scan, now)
        return scan, True


def invalidate_account_scan():
    from . import state

    state.clear_scan_cache()


def stop_polling_job(context: ContextTypes.DEFAULT_TYPE, short_id: str):
    jq = getattr(context.application, "job_queue", None)
    if not jq:
        return
    for job in jq.get_jobs_by_name(_job_name(short_id)):
        job.schedule_removal()
    drop_lock(short_id)


def schedule_polling_job(app, short_id: str, chat_id: int, message_id: int, user_id: int):
    jq = getattr(app, "job_queue", None)
    if not jq:
        return
    name = _job_name(short_id)
    for job in jq.get_jobs_by_name(name):
        job.schedule_removal()

    jq.run_repeating(
        poll_payment_job,
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


async def provision_instance_for_invoice(context: ContextTypes.DEFAULT_TYPE, inv: dict) -> dict | None:
    short_id = inv["short_id"]
    if is_provisioning(short_id):
        return None
    mark_provisioning(short_id)
    try:
        claimed = vps_db.claim_invoice_for_provision(short_id)
        if not claimed:
            log.info("Provision skipped, invoice already claimed | short_id=%s", short_id)
            return None
        inv = claimed

        t = get_tier(inv["tier"])
        o = get_os(inv["os_image"])
        if not t or not o:
            log.error("Invalid tier or os in invoice | short_id=%s tier=%s os=%s", short_id, inv["tier"], inv["os_image"])
            return None

        if inv.get("message_id"):
            try:
                rich, plain = render.provisioning_content(inv)
                if inv.get("has_photo"):
                    await context.bot.edit_message_caption(
                        chat_id=inv["chat_id"],
                        message_id=inv["message_id"],
                        caption=plain,
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=inv["chat_id"],
                        message_id=inv["message_id"],
                        text=plain,
                        parse_mode="HTML",
                        reply_markup=None,
                    )
            except Exception as e:
                log.debug("Provisioning caption edit failed | short_id=%s err=%r", short_id, e)

        region = inv.get("region") or DEFAULT_REGION
        vps_id = vps_db.next_vps_id(inv["user_id"])
        tag_name = vps_id

        res, password = await aws.launch_vps(
            region=region,
            os_id=o["id"],
            instance_type=t["instance_type"],
            disk_gb=t["disk_gb"],
            tag_name=tag_name,
            account=inv.get("account"),
        )

        instance_id = res["instance_id"]
        account_used = res.get("account") or inv.get("account")
        ip = await aws.wait_for_public_ip(
            instance_id, region=region, max_attempts=25, delay=3.0, account=account_used
        )

        if ip:
            await aws.wait_for_ssh(ip, max_wait_sec=180, interval=5.0)

        now = time.time()
        expires_at = now + (int(inv["days"]) * 86400)
        warranty_until = now + (WARRANTY_DAYS * 86400)

        inst = vps_db.save_instance(
            vps_id=vps_id,
            user_id=inv["user_id"],
            instance_id=instance_id,
            instance_type=t["instance_type"],
            region=region,
            tier=normalize_tier(inv["tier"]),
            os_image=inv["os_image"],
            disk_gb=t["disk_gb"],
            ip_address=ip or "",
            root_password=password,
            price=inv["base_amount"],
            days=inv["days"],
            account=account_used,
            sg_id=res.get("sg_id"),
            created_at=now,
            expires_at=expires_at,
            warranty_until=warranty_until,
        )
        vps_db.mark_invoice_paid(short_id, vps_id=vps_id)
        vps_db.log_audit(
            vps_id,
            "PROVISION",
            inv["user_id"],
            f"tier={inv['tier']} region={region} ip={ip} account={account_used}",
        )
        invalidate_account_scan()

        try:
            buyer_name, buyer_username = await notify.buyer_label(context.bot, inv["user_id"])
            await notify.notify_owner(
                context.bot,
                inst,
                source="purchase",
                buyer_name=buyer_name,
                buyer_username=buyer_username,
                invoice=inv,
            )
        except Exception as e:
            log.warning("Owner purchase notice failed | vps_id=%s err=%r", vps_id, e)

        try:
            rich, plain = render.provisioned_content(inst)
            await send_rich(context.bot, inv["chat_id"], None, rich, plain, render.instance_detail_keyboard(vps_id, "active", True))
        except Exception as e:
            log.error("Failed to send provisioned card | vps_id=%s err=%r", vps_id, e)

        return inst
    except Exception as e:
        log.error("Provisioning failed | short_id=%s err=%r", short_id, e)
        vps_db.log_audit(None, "PROVISION_FAILED", inv.get("user_id"), f"short_id={short_id} err={e}")
        await notify_owner(context.bot, inv, "failed", Reason=f"Provisioning failed: {e}")
        try:
            await notify.notify_owner_failure(context.bot, inv, str(e))
        except Exception as notify_err:
            log.warning("Owner failure notice failed | short_id=%s err=%r", short_id, notify_err)
        try:
            vps_db.set_invoice_status(short_id, "PAID")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=inv["chat_id"],
                text=(
                    "<b>⚠️ Automatic Provisioning Failed</b>\n\n"
                    f"Your payment has been recorded (Ref: <code>{inv['trx_id']}</code>). "
                    "Our team will activate your server manually. "
                    "Please contact support with this reference number."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return None
    finally:
        clear_provisioning(short_id)


async def poll_payment_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    short_id = data.get("short_id")
    if not short_id:
        context.job.schedule_removal()
        return

    inv = vps_db.get_invoice(short_id)
    if not inv or inv["status"] in ("PAID", "EXPIRED", "CANCELLED"):
        context.job.schedule_removal()
        return

    now = time.time()
    if now >= inv["deadline"]:
        vps_db.set_invoice_status(short_id, "EXPIRED")
        stop_polling_job(context, short_id)
        await notify_owner(context.bot, inv, "expired", Reason="5-minute payment window ended")
        try:
            await context.bot.edit_message_caption(
                chat_id=inv["chat_id"],
                message_id=inv["message_id"],
                caption="<b>⏰ PAYMENT EXPIRED</b>\n\nThe 5-minute payment window has ended. Start a new order to try again.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    async with get_lock(short_id):
        fresh = vps_db.get_invoice(short_id)
        if not fresh or fresh["status"] != "PENDING":
            context.job.schedule_removal()
            return

        try:
            res = await check_payment(fresh)
        except Exception as e:
            log.warning("VPS payment poll failed | short_id=%s err=%r", short_id, e)
            return

        if res.get("paid") or res.get("status") == "PAID":
            stop_polling_job(context, short_id)
            await notify_owner(context.bot, fresh, "success")
            await provision_instance_for_invoice(context, fresh)
            return

        if res.get("status") == "EXPIRED":
            vps_db.set_invoice_status(short_id, "EXPIRED")
            stop_polling_job(context, short_id)
            await notify_owner(context.bot, fresh, "expired", Reason="Expired at provider")
            try:
                await context.bot.edit_message_caption(
                    chat_id=fresh["chat_id"],
                    message_id=fresh["message_id"],
                    caption="<b>⏰ PAYMENT EXPIRED</b>\n\nThis invoice was not paid within the 5-minute deadline.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        try:
            rem = int(fresh["deadline"] - now)
            rich, plain = render.invoice_content(fresh, status="PENDING", remaining=rem)
            await context.bot.edit_message_caption(
                chat_id=fresh["chat_id"],
                message_id=fresh["message_id"],
                caption=plain,
                parse_mode="HTML",
                reply_markup=render.invoice_keyboard(short_id, qr_url_for(fresh)),
            )
        except Exception:
            pass
