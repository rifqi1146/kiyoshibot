import html
import logging
import time
from datetime import datetime, timedelta, timezone

from utils.config import OWNER_ID

log = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

PRODUCT_NAMES = {
    "premium": "Premium Membership",
    "caca": "Caca Chat Credits",
    "vps": "AWS VPS Instance",
}

_RESERVED_KEYS = frozenset({"user_name", "username"})

EVENT_LABELS = {
    "new": "New Transaction",
    "success": "Payment Successful",
    "failed": "Payment Failed",
    "expired": "Payment Expired",
    "cancelled": "Payment Cancelled",
}


def _fmt_rp(amount) -> str:
    try:
        return "Rp " + f"{int(amount):,}".replace(",", ".")
    except Exception:
        return "Rp -"


def _fmt_time(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    except Exception:
        return "-"


def _row(label: str, value) -> str:
    return f"<b>{html.escape(label)}:</b> <code>{html.escape(str(value))}</code>"


def _user_label(details: dict) -> str:
    name = (details.get("user_name") or "").strip()
    username = (details.get("username") or "").strip()
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return "-"


async def _resolve_user_label(bot, user_id) -> dict:
    info = {"user_name": "", "username": ""}
    if not bot or not user_id:
        return info
    try:
        chat = await bot.get_chat(int(user_id))
        info["user_name"] = (getattr(chat, "full_name", None) or getattr(chat, "first_name", None) or "").strip()
        info["username"] = (getattr(chat, "username", None) or "").strip()
    except Exception as e:
        log.debug("Owner payment notice user lookup failed | user_id=%s err=%r", user_id, e)
    return info


def build_notice(product: str, event: str, *, user_id=None, trx_id=None, amount=None,
                 base_amount=None, fee_amount=None, details: dict | None = None) -> tuple[str, str]:
    details = details or {}
    product_name = PRODUCT_NAMES.get(product, product)
    event_label = EVENT_LABELS.get(event, event)

    lines = [
        f"<b>💳 {event_label}</b>",
        "",
        _row("Product", product_name),
        _row("User ID", user_id if user_id not in (None, "") else "-"),
    ]
    label = _user_label(details)
    if label != "-":
        lines.append(_row("User", label))
    lines.append(_row("Trx ID", trx_id or "-"))
    if amount not in (None, ""):
        lines.append(_row("Total Charged", _fmt_rp(amount)))
    if base_amount not in (None, ""):
        lines.append(_row("Base Price", _fmt_rp(base_amount)))
    if fee_amount not in (None, ""):
        lines.append(_row("Admin Fee", _fmt_rp(fee_amount)))
    lines.append(_row("Time", _fmt_time(time.time())))

    extra = [
        (k, v) for k, v in details.items()
        if k not in _RESERVED_KEYS and v not in (None, "", "-", 0)
    ]
    if extra:
        lines.append("")
        for key, value in extra:
            lines.append(_row(key, value))

    rich = "\n".join(lines)
    plain = rich
    return rich, plain


async def notify_owner_event(bot, product: str, event: str, *, user_id=None, trx_id=None,
                             amount=None, base_amount=None, fee_amount=None, details: dict | None = None):
    if not OWNER_ID:
        return False
    details = dict(details or {})
    lookup = await _resolve_user_label(bot, user_id)
    for key, value in lookup.items():
        details.setdefault(key, value)
    rich, plain = build_notice(
        product, event, user_id=user_id, trx_id=trx_id, amount=amount,
        base_amount=base_amount, fee_amount=fee_amount, details=details,
    )
    sent = 0
    for owner in sorted(OWNER_ID):
        try:
            await bot.send_message(chat_id=int(owner), text=plain, parse_mode="HTML",
                                   disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            log.warning("Owner payment notice failed | owner=%s product=%s event=%s trx=%s err=%r",
                        owner, product, event, trx_id, e)
    log.info(
        "Owner payment notice | product=%s event=%s trx=%s user=%s amount=%s delivered=%s/%s",
        product, event, trx_id, user_id, amount, sent, len(OWNER_ID),
    )
    return sent > 0
