import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import vps_db
from utils.config import OWNER_ID

from . import render
from .rich import send_rich

log = logging.getLogger(__name__)

PM_COMMANDS = {"vps", "myvps"}

TIER_ALIASES = {
    "1": "vps_1gb", "1gb": "vps_1gb", "vps_1gb": "vps_1gb", "micro": "vps_1gb",
    "2": "vps_2gb", "2gb": "vps_2gb", "vps_2gb": "vps_2gb", "small": "vps_2gb",
    "4": "vps_4gb", "4gb": "vps_4gb", "vps_4gb": "vps_4gb", "medium": "vps_4gb",
    "8": "vps_8gb", "8gb": "vps_8gb", "vps_8gb": "vps_8gb", "large": "vps_8gb",
}

PERIOD_30 = {"30d", "30", "month", "monthly", "bulanan"}
PERIOD_14 = {"14d", "14", "2w", "2week", "2weeks", "2minggu", "biweekly"}
PERIOD_7 = {"7d", "7", "week", "weekly", "mingguan"}


def _is_group(chat) -> bool:
    if not chat:
        return False
    return chat.type in ("group", "supergroup") or int(chat.id) < 0


def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    uname = getattr(context.bot, "username", None)
    if uname:
        return uname
    try:
        data = context.bot.username
    except Exception:
        return ""
    return data or ""


def _pm_keyboard(context: ContextTypes.DEFAULT_TYPE, start_arg: str, user_id: int) -> InlineKeyboardMarkup:
    uname = _bot_username(context)
    if not uname:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Open Private Chat", callback_data=f"vps:pm:{start_arg}:{user_id}")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Open in Private Chat", url=f"https://t.me/{uname}?start={start_arg}")]
    ])


async def _redirect_to_pm(update: Update, context: ContextTypes.DEFAULT_TYPE, start_arg: str, title: str) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    text = (
        f"<b>{title}</b>\n\n"
        "For privacy, VPS ordering and credentials are only available in a direct message with the bot.\n\n"
        "Tap the button below to continue in private chat."
    )
    try:
        await msg.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=_pm_keyboard(context, start_arg, user.id),
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("VPS PM redirect failed | err=%r", e)


async def vps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    if _is_group(chat):
        return await _redirect_to_pm(update, context, "vps", "AWS VPS")

    vps_db.init_db()
    rich, plain = render.main_menu_content()
    await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.main_menu_keyboard())


async def myvps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    if _is_group(chat):
        return await _redirect_to_pm(update, context, "myvps", "My VPS")

    vps_db.init_db()
    instances = await asyncio.to_thread(vps_db.list_user_instances, user.id)
    active = [i for i in instances if i["status"] in ("active", "replaced")]
    rich, plain = render.myvps_content(active)
    await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.myvps_keyboard(active))


def _parse_amount(raw: str) -> int | None:
    text = raw.strip().lower().replace("rp", "").replace("_", "").replace(" ", "")
    if not text:
        return None
    if text.endswith("k"):
        body = text[:-1]
        body = body.replace(",", ".")
        try:
            return int(round(float(body) * 1000))
        except Exception:
            return None
    try:
        return int(text.replace(".", "").replace(",", ""))
    except Exception:
        return None


def _resolve_tier(raw: str) -> str | None:
    key = raw.strip().lower()
    if key in TIER_ALIASES:
        return TIER_ALIASES[key]
    compact = key.replace("-", "").replace("_", "").replace(" ", "")
    if compact in TIER_ALIASES:
        return TIER_ALIASES[compact]
    if compact in ("vps1gb", "vps2gb", "vps4gb", "vps8gb"):
        return "vps_" + compact[3:]
    return None


def _price_help() -> tuple[str, str]:
    rich = (
        "<h1>🏷 Set Plan Pricing</h1>"
        "<p>Change prices instantly. New prices apply to every new invoice.</p>"
        "<h3>Usage</h3>"
        "<ul>"
        "<li><code>/vpsprice &lt;plan&gt; &lt;period&gt; &lt;amount&gt;</code> — set one period</li>"
        "<li><code>/vpsprice &lt;plan&gt; &lt;30d&gt; &lt;14d&gt; &lt;7d&gt;</code> — set all three</li>"
        "<li><code>/vpsprice &lt;plan&gt; &lt;30d&gt; &lt;7d&gt;</code> — 14d auto-computed</li>"
        "<li><code>/vpsprice &lt;plan&gt; &lt;amount&gt;</code> — 14d and 7d auto-computed</li>"
        "</ul>"
        "<h3>Examples</h3>"
        "<ul>"
        "<li><code>/vpsprice 1gb 30d 20000</code></li>"
        "<li><code>/vpsprice 2gb 14d 18000</code></li>"
        "<li><code>/vpsprice 4gb 7d 15000</code></li>"
        "<li><code>/vpsprice 8gb 55000 33000 22000</code></li>"
        "<li><code>/vpsprice 1gb 25000</code></li>"
        "</ul>"
        "<p>Plans: <code>1gb</code> <code>2gb</code> <code>4gb</code> <code>8gb</code>. "
        "Periods: <code>30d</code> <code>14d</code> <code>7d</code>. "
        "Amounts accept <code>20000</code>, <code>20k</code>, or <code>Rp 20.000</code>.</p>"
    )
    plain = (
        "<b>🏷 Set Plan Pricing</b>\n\n"
        "Usage:\n"
        "/vpsprice <plan> <period> <amount>\n"
        "/vpsprice <plan> <30d> <14d> <7d>\n"
        "/vpsprice <plan> <30d> <7d>\n"
        "/vpsprice <plan> <amount>\n\n"
        "Examples:\n"
        "• /vpsprice 1gb 30d 20000\n"
        "• /vpsprice 2gb 14d 18000\n"
        "• /vpsprice 4gb 7d 15000\n"
        "• /vpsprice 8gb 55000 33000 22000\n"
        "• /vpsprice 1gb 25000\n\n"
        "Plans: 1gb, 2gb, 4gb, 8gb. Periods: 30d, 14d, 7d. "
        "Amounts accept 20000, 20k, or Rp 20.000."
    )
    return rich, plain


async def vpsprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if int(user.id) not in OWNER_ID:
        return

    vps_db.init_db()
    args = context.args or []
    if not args:
        rich, plain = render.owner_price_content()
        return await send_rich(context.bot, chat.id, msg.message_id, rich, plain, render.owner_price_keyboard())

    tier_id = _resolve_tier(args[0])
    if not tier_id:
        rich, plain = _price_help()
        return await send_rich(
            context.bot, chat.id, msg.message_id, rich, plain, render.owner_price_keyboard()
        )

    pricing = vps_db.get_all_pricing()
    cur = pricing.get(tier_id, {"price_30d": 0, "price_14d": 0, "price_7d": 0})

    if len(args) == 1:
        rich, plain = _price_help()
        return await send_rich(
            context.bot, chat.id, msg.message_id, rich, plain, render.owner_price_keyboard()
        )

    new_30d = cur["price_30d"]
    new_14d = cur.get("price_14d", 0)
    new_7d = cur["price_7d"]
    changed = []

    period = args[1].strip().lower() if len(args) >= 3 else ""

    if period in PERIOD_30 or period in PERIOD_14 or period in PERIOD_7:
        amount = _parse_amount(args[2])
        if amount is None or amount <= 0:
            return await msg.reply_text(
                "<b>Invalid amount. Use a number like 20000 or 20k.</b>", parse_mode="HTML"
            )
        if period in PERIOD_30:
            new_30d = amount
            changed.append("30d")
        elif period in PERIOD_14:
            new_14d = amount
            changed.append("14d")
        else:
            new_7d = amount
            changed.append("7d")
    elif len(args) == 2:
        amount = _parse_amount(args[1])
        if amount is None or amount <= 0:
            return await msg.reply_text(
                "<b>Invalid amount. Use a number like 20000 or 20k.</b>", parse_mode="HTML"
            )
        new_30d = amount
        new_14d = max(1, round(amount * 0.60))
        new_7d = max(1, round(amount * 0.40))
        changed.extend(["30d", "14d", "7d"])
    elif len(args) == 4:
        a30 = _parse_amount(args[1])
        a14 = _parse_amount(args[2])
        a7 = _parse_amount(args[3])
        if any(x is None or x <= 0 for x in (a30, a14, a7)):
            return await msg.reply_text(
                "<b>Invalid amounts. Example: /vpsprice 4gb 45000 27000 18000</b>", parse_mode="HTML"
            )
        new_30d, new_14d, new_7d = a30, a14, a7
        changed.extend(["30d", "14d", "7d"])
    elif len(args) == 3:
        a30 = _parse_amount(args[1])
        a7 = _parse_amount(args[2])
        if a30 is None or a7 is None or a30 <= 0 or a7 <= 0:
            return await msg.reply_text(
                "<b>Invalid amounts. Example: /vpsprice 4gb 45000 18000</b>", parse_mode="HTML"
            )
        new_30d = a30
        new_14d = max(1, round(a30 * 0.60))
        new_7d = a7
        changed.extend(["30d", "14d", "7d"])
    else:
        rich, plain = _price_help()
        return await send_rich(
            context.bot, chat.id, msg.message_id, rich, plain, render.owner_price_keyboard()
        )

    vps_db.set_pricing(tier_id, new_30d, new_7d, new_14d)
    vps_db.log_audit(
        None,
        "PRICE_CHANGE",
        user.id,
        f"tier={tier_id} periods={','.join(changed)} 30d={new_30d} 14d={new_14d} 7d={new_7d}",
    )

    updated = vps_db.get_all_pricing()[tier_id]
    rich, plain = render.owner_price_content()
    await msg.reply_text(
        f"<b>Price updated for {tier_id}</b>\n\n"
        f"30 days: Rp {updated['price_30d']:,}\n"
        f"14 days: Rp {updated['price_14d']:,}\n"
        f"7 days: Rp {updated['price_7d']:,}".replace(",", "."),
        parse_mode="HTML",
    )
    return await send_rich(
        context.bot, chat.id, msg.message_id, rich, plain, render.owner_price_keyboard()
    )
