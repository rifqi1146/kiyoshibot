import html
import logging
import time
from datetime import datetime, timezone, timedelta

from utils.config import OWNER_ID

from . import accounts as accounts_store
from .constants import get_os, get_tier, region_display, normalize_tier

log = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts), WIB).strftime("%d %b %Y %H:%M WIB")
    except Exception:
        return "-"


def _fmt_rp(amount) -> str:
    try:
        return f"Rp {int(amount or 0):,}".replace(",", ".")
    except Exception:
        return "Rp 0"


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "-"))


def build_owner_notice(
    inst: dict,
    *,
    source: str = "purchase",
    buyer_name: str = "",
    buyer_username: str = "",
    invoice: dict | None = None,
    note: str = "",
) -> str:
    inst = inst or {}
    invoice = invoice or {}

    tier_id = normalize_tier(inst.get("tier") or invoice.get("tier"))
    tier = get_tier(tier_id) or {}
    os_row = get_os(inst.get("os_image") or invoice.get("os_image")) or {}

    region = inst.get("region") or invoice.get("region") or "-"
    account = inst.get("account") or ""
    account_label = accounts_store.account_label(account) if account else "default"
    account_chain = []
    try:
        from . import aws

        account_chain = aws.region_chain(region)
    except Exception:
        account_chain = []

    user_id = inst.get("user_id") or invoice.get("user_id") or "-"
    if buyer_username:
        buyer = f"@{buyer_username.lstrip('@')}"
    elif buyer_name:
        buyer = buyer_name
    else:
        buyer = "unknown"

    icon = {
        "purchase": "🆕 New VPS Sold",
        "grant": "🎁 VPS Granted",
        "warranty": "🛠️ Warranty Replacement",
    }.get(source, "🆕 New VPS")

    lines = [
        f"<b>{icon}</b>",
        "",
        "<b>Server</b>",
        f"ID: <code>{_esc(inst.get('vps_id'))}</code>",
        f"Instance: <code>{_esc(inst.get('instance_id'))}</code>",
        f"Plan: {_esc(tier.get('label') or tier_id)} "
        f"({_esc(tier.get('vcpu', '-'))} vCPU · {_esc(tier.get('ram_gb', '-'))} GB RAM · "
        f"{_esc(tier.get('disk_gb', inst.get('disk_gb')))} GB SSD)",
        f"OS: {_esc(os_row.get('label') or inst.get('os_image'))}",
        f"Type: <code>{_esc(inst.get('instance_type'))}</code>",
        "",
        "<b>Where</b>",
        f"Region: {_esc(region_display(region))} (<code>{_esc(region)}</code>)",
        (
            f"Account used: <b>{_esc(account_label)}</b> (<code>{_esc(account)}</code>)"
            if account_label != (account or "")
            else f"Account used: <b>{_esc(account_label or 'default')}</b>"
        ),
    ]

    if len(account_chain) > 1:
        lines.append(f"Account chain: <code>{_esc(' → '.join(account_chain))}</code>")

    lines += [
        "",
        "<b>Access</b>",
        f"IP: <code>{_esc(inst.get('ip_address') or '-')}</code>",
        f"SSH: <code>ssh root@{_esc(inst.get('ip_address') or '-')}</code>",
        f"Password: <code>{_esc(inst.get('root_password') or '-')}</code>",
        "",
        "<b>Buyer</b>",
        f"Name: {_esc(buyer)}",
        f"User ID: <code>{_esc(user_id)}</code>",
    ]

    if invoice.get("trx_id") or invoice.get("short_id"):
        lines += [
            "",
            "<b>Payment</b>",
            f"Base: {_esc(_fmt_rp(invoice.get('base_amount', inst.get('price'))))}",
            f"Charged (QRIS): {_esc(_fmt_rp(invoice.get('gross_amount')))}",
            f"Ref: <code>{_esc(invoice.get('trx_id') or invoice.get('short_id'))}</code>",
        ]
    elif inst.get("price"):
        lines += ["", "<b>Payment</b>", f"Base: {_esc(_fmt_rp(inst.get('price')))}"]

    lines += [
        "",
        "<b>Validity</b>",
        f"Days: {_esc(inst.get('days'))}",
        f"Active until: {_esc(_fmt_time(inst.get('expires_at')))}",
        f"Warranty until: {_esc(_fmt_time(inst.get('warranty_until')))}",
        f"Created: {_esc(_fmt_time(inst.get('created_at') or time.time()))}",
    ]

    if note:
        lines += ["", f"<i>{_esc(note)}</i>"]

    return "\n".join(lines)


async def notify_owner(bot, inst: dict, **kwargs) -> None:
    if not bot or not OWNER_ID:
        return
    try:
        text = build_owner_notice(inst, **kwargs)
    except Exception as e:
        log.error("Owner notice build failed | err=%r", e)
        return

    for owner_id in OWNER_ID:
        try:
            await bot.send_message(chat_id=int(owner_id), text=text, parse_mode="HTML")
        except Exception as e:
            log.warning("Owner notice failed | owner_id=%s err=%r", owner_id, e)

    log.info(
        "Owner notified | vps_id=%s account=%s region=%s",
        inst.get("vps_id"),
        inst.get("account"),
        inst.get("region"),
    )


async def notify_owner_failure(bot, inv: dict, error: str) -> None:
    if not bot or not OWNER_ID:
        return
    tier_id = normalize_tier(inv.get("tier"))
    tier = get_tier(tier_id) or {}
    region = inv.get("region") or "-"
    text = "\n".join(
        [
            "<b>⚠️ VPS Provisioning Failed</b>",
            "",
            f"Plan: {_esc(tier.get('label') or tier_id)}",
            f"Region: {_esc(region_display(region))} (<code>{_esc(region)}</code>)",
            f"User ID: <code>{_esc(inv.get('user_id'))}</code>",
            f"Ref: <code>{_esc(inv.get('trx_id') or inv.get('short_id'))}</code>",
            f"Paid: {_esc(_fmt_rp(inv.get('gross_amount')))}",
            "",
            f"Error: <code>{_esc(str(error)[:400])}</code>",
        ]
    )
    for owner_id in OWNER_ID:
        try:
            await bot.send_message(chat_id=int(owner_id), text=text, parse_mode="HTML")
        except Exception as e:
            log.warning("Owner failure notice failed | owner_id=%s err=%r", owner_id, e)


async def buyer_label(bot, user_id) -> tuple[str, str]:
    try:
        chat = await bot.get_chat(int(user_id))
        first = getattr(chat, "first_name", "") or ""
        last = getattr(chat, "last_name", "") or ""
        name = (first + (" " + last if last else "")).strip()
        username = getattr(chat, "username", "") or ""
        return name, username
    except Exception:
        return "", ""
