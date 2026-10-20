import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import vps_db
from utils.config import OWNER_ID

from . import accounts as accounts_store
from . import aws, render, service
from .constants import DEFAULT_REGION
from .rich import send_rich

log = logging.getLogger(__name__)

ACCOUNT_PREFIX = "vpsacc"
PENDING_TTL = 600

USAGE = (
    "<b>AWS Account Manager</b>\n\n"
    "<code>/addawsaccount</code> — open the manager panel\n"
    "<code>/addawsaccount &lt;name&gt; &lt;access_key&gt; &lt;secret_key&gt; [regions]</code>\n"
    "<code>/delawsaccount &lt;name&gt;</code>\n"
    "<code>/delawsaccount</code> — pick an account to delete\n\n"
    "Leave regions out and the bot detects every region the account can use.\n"
    "Accounts are tried in priority order, lowest number first."
)

_PENDING: dict[int, dict] = {}


def _is_owner(user_id: int) -> bool:
    return int(user_id) in OWNER_ID


def _parse_regions(raw: str) -> list[str]:
    return [r.strip().lower() for r in (raw or "").split(",") if r.strip()]


def _panel(show_secrets: bool = False) -> tuple[str, str, InlineKeyboardMarkup]:
    rich, plain = render.accounts_content(show_secrets=show_secrets)
    return rich, plain, render.accounts_keyboard(show_secrets)


def _delete_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for a in accounts_store.describe(include_disabled=True):
        label = "Default profile (cannot delete)" if a.get("is_base") else a["label"]
        rows.append(
            [
                InlineKeyboardButton(
                    f"Delete {label}",
                    callback_data=f"{ACCOUNT_PREFIX}:del:{a['name']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("Back", callback_data=f"{ACCOUNT_PREFIX}:list")])
    return InlineKeyboardMarkup(rows)


async def _send_panel(msg, context, show_secrets: bool = False):
    rich, plain, kb = _panel(show_secrets)
    return await send_rich(context.bot, msg.chat_id, msg.message_id, rich, plain, kb)


async def _add_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    name: str,
    access_key: str = "",
    secret_key: str = "",
):
    name = (name or "").strip().lower()
    if not name:
        return await update.effective_message.reply_text(USAGE, parse_mode="HTML")
    if accounts_store.get_account(name):
        return await update.effective_message.reply_text(
            f"<b>Account <code>{name}</code> already exists.</b>\nUse the panel to reveal or delete it.",
            parse_mode="HTML",
        )

    if access_key and secret_key:
        _PENDING[int(update.effective_user.id)] = {
            "name": name,
            "access_key": access_key,
            "secret_key": secret_key,
            "stage": "confirm",
        }
        return await update.effective_message.reply_text(
            f"<b>Adding account:</b> <code>{name}</code>\n\n"
            "Send <code>yes</code> to detect regions automatically, "
            "or list them manually (for example <code>ap-southeast-2</code>).\n"
            "<i>Send /cancel to abort.</i>",
            parse_mode="HTML",
        )

    _PENDING[int(update.effective_user.id)] = {"name": name, "stage": "access_key"}
    return await update.effective_message.reply_text(
        f"<b>Adding account:</b> <code>{name}</code>\n\n"
        "Send the <b>Access Key ID</b>.\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode="HTML",
    )


async def addawsaccount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return

    vps_db.init_db()
    args = [a for a in (context.args or []) if (a or "").strip()]

    if not args:
        return await _send_panel(msg, context, show_secrets=False)

    if args[0].lower() in ("list", "panel"):
        return await _send_panel(msg, context, show_secrets=False)

    if len(args) == 1:
        return await _add_flow(update, context, args[0])

    if len(args) < 3:
        return await msg.reply_text(USAGE, parse_mode="HTML")

    name = args[0]
    access_key = args[1]
    secret_key = args[2]
    regions = _parse_regions(args[3]) if len(args) > 3 else None

    pending = {"name": name.strip().lower(), "access_key": access_key, "secret_key": secret_key}
    return await _finish_account(update, context, pending, regions=regions)


async def delawsaccount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not _is_owner(user.id):
        return

    vps_db.init_db()
    args = [a for a in (context.args or []) if (a or "").strip()]

    accounts = accounts_store.describe(include_disabled=True)
    if not accounts:
        return await msg.reply_text(
            "<b>No extra AWS accounts configured.</b>", parse_mode="HTML"
        )

    if not args:
        return await msg.reply_text(
            "<b>Select an account to delete:</b>",
            parse_mode="HTML",
            reply_markup=_delete_keyboard(),
        )

    name = args[0].strip().lower()
    if not accounts_store.get_account(name):
        return await msg.reply_text(
            f"<b>Account <code>{name}</code> not found.</b>", parse_mode="HTML"
        )

    accounts_store.remove_account(name)
    aws.invalidate_region_sources()
    service.invalidate_account_scan()
    vps_db.log_audit(None, "ACCOUNT_DEL", user.id, f"name={name}")
    await _send_panel(msg, context, show_secrets=False)
    return await msg.reply_text(
        f"<b>Account removed:</b> <code>{name}</code>", parse_mode="HTML"
    )


async def capture_account_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not _is_owner(user.id):
        return
    pending = _PENDING.get(int(user.id))
    if not pending:
        return

    text = (msg.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        _PENDING.pop(int(user.id), None)
        return await msg.reply_text("<b>Cancelled.</b>", parse_mode="HTML")

    stage = pending.get("stage")
    if stage == "access_key":
        if len(text) < 16:
            return await msg.reply_text(
                "<b>That does not look like an Access Key ID.</b> Send it again.",
                parse_mode="HTML",
            )
        pending["access_key"] = text
        pending["stage"] = "secret_key"
        if msg.chat.type == "private":
            try:
                await msg.delete()
            except Exception:
                pass
        return await msg.reply_text(
            "<b>Access Key saved.</b>\n\nNow send the <b>Secret Access Key</b>.\n"
            "<i>Send /cancel to abort.</i>",
            parse_mode="HTML",
        )

    if stage == "secret_key":
        if len(text) < 20:
            return await msg.reply_text(
                "<b>That does not look like a Secret Access Key.</b> Send it again.",
                parse_mode="HTML",
            )
        pending["secret_key"] = text
        pending["stage"] = "confirm"
        if msg.chat.type == "private":
            try:
                await msg.delete()
            except Exception:
                pass
        return await msg.reply_text(
            "<b>Secret saved.</b>\n\n"
            "Send <code>yes</code> to detect regions automatically, "
            "or list them manually (for example <code>ap-southeast-2</code>).\n"
            "<i>Send /cancel to abort.</i>",
            parse_mode="HTML",
        )

    if stage in ("confirm", "regions"):
        lowered = text.lower()
        if stage == "confirm" and lowered in ("yes", "y", "ok", "auto", "ya"):
            _PENDING.pop(int(user.id), None)
            return await _finish_account(update, context, pending, regions=None)

        regions = _parse_regions(text)
        if not regions:
            return await msg.reply_text(
                "<b>Send <code>yes</code> to auto-detect, or list at least one region.</b>",
                parse_mode="HTML",
            )
        _PENDING.pop(int(user.id), None)
        return await _finish_account(update, context, pending, regions=regions)


async def _finish_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: dict,
    regions: list[str] | None = None,
):
    msg = update.effective_message
    user = update.effective_user
    name = pending["name"]
    access_key = pending["access_key"]
    secret_key = pending["secret_key"]

    status = await msg.reply_text(f"<b>Verifying {name}...</b>", parse_mode="HTML")

    seed = regions[0] if regions else DEFAULT_REGION
    ok, detail = await aws.verify_account_credentials(access_key, secret_key, seed)
    if not ok:
        return await status.edit_text(
            "<b>Credentials rejected by AWS.</b>\n"
            f"<code>{detail}</code>\n\nNothing was saved.",
            parse_mode="HTML",
        )

    detected = False
    if not regions:
        await status.edit_text(
            f"<b>Verified as <code>{detail}</code></b>\n\n"
            "<i>Detecting regions this account can use...</i>",
            parse_mode="HTML",
        )
        regions = await aws.discover_account_regions(access_key, secret_key)
        detected = True
        if not regions:
            return await status.edit_text(
                "<b>Could not detect any usable region for this account.</b>\n"
                "Send the regions manually, for example <code>ap-southeast-2</code>.",
                parse_mode="HTML",
            )

    accounts_store.add_account(
        name,
        access_key,
        secret_key,
        regions,
        label=name,
    )
    aws.invalidate_region_sources()
    service.invalidate_account_scan()
    vps_db.log_audit(
        None, "ACCOUNT_ADD", user.id, f"name={name} regions={','.join(regions)}"
    )
    await _send_panel(msg, context, show_secrets=False)
    how = "auto-detected" if detected else "configured"
    shown = ", ".join(regions[:8]) + (" ..." if len(regions) > 8 else "")
    return await status.edit_text(
        f"<b>Account added:</b> <code>{name}</code>\n"
        f"Verified as <code>{detail}</code>\n"
        f"Regions ({how}, {len(regions)}): <code>{shown}</code>",
        parse_mode="HTML",
    )


async def account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    if not q or not user or not _is_owner(user.id):
        try:
            await q.answer("Owner only.", show_alert=True)
        except Exception:
            pass
        return

    from .callbacks import _answer, _render_and_edit

    parts = (q.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "list":
        await _answer(q)
        rich, plain, kb = _panel(False)
        return await _render_and_edit(q, rich, plain, kb)

    if action == "secret":
        show = len(parts) > 2 and parts[2] == "1"
        await _answer(q, "Keys revealed." if show else "Keys hidden.")
        rich, plain, kb = _panel(show)
        return await _render_and_edit(q, rich, plain, kb)

    if action == "toggle":
        if len(parts) < 3:
            return
        name = parts[2]
        if name == accounts_store.DEFAULT_ACCOUNT_NAME:
            new_state = accounts_store.toggle_base_account()
            aws.invalidate_region_sources()
            service.invalidate_account_scan()
            await _answer(q, f"default: {'enabled' if new_state else 'disabled'}")
            rich, plain, kb = _panel(False)
            return await _render_and_edit(q, rich, plain, kb)
        cfg = accounts_store.get_account(name)
        if not cfg:
            return await _answer(q, "Account not found.", show_alert=True)
        new_state = not bool(cfg.get("enabled", True))
        accounts_store.set_account_enabled(name, new_state)
        aws.invalidate_region_sources()
        service.invalidate_account_scan()
        await _answer(q, f"{name}: {'enabled' if new_state else 'disabled'}")
        rich, plain, kb = _panel(False)
        return await _render_and_edit(q, rich, plain, kb)

    if action == "del":
        if len(parts) < 3:
            return
        name = parts[2]
        if name == accounts_store.DEFAULT_ACCOUNT_NAME:
            return await _answer(
                q, "The default profile cannot be deleted. Disable it instead.", show_alert=True
            )
        if not accounts_store.get_account(name):
            return await _answer(q, "Account not found.", show_alert=True)
        accounts_store.remove_account(name)
        aws.invalidate_region_sources()
        service.invalidate_account_scan()
        vps_db.log_audit(None, "ACCOUNT_DEL", user.id, f"name={name}")
        await _answer(q, f"Deleted {name}")
        if not accounts_store.describe(include_disabled=True):
            rich, plain = render.accounts_content(show_secrets=False)
            return await _render_and_edit(q, rich, plain, render.accounts_keyboard(False))
        return await _render_and_edit(
            q,
            "<h1>🔑 AWS Accounts</h1><p>Select an account to delete.</p>",
            "<b>🔑 AWS Accounts</b>\n\nSelect an account to delete.",
            _delete_keyboard(),
        )

    if action == "delete_menu":
        await _answer(q)
        if not accounts_store.describe(include_disabled=True):
            rich, plain, kb = _panel(False)
            return await _render_and_edit(q, rich, plain, kb)
        return await _render_and_edit(
            q,
            "<h1>🗑 Delete AWS Account</h1><p>Select an account to delete. "
            "Servers already running on that account stay untouched.</p>",
            "<b>🗑 Delete AWS Account</b>\n\nSelect an account to delete.",
            _delete_keyboard(),
        )

    if action == "new":
        await _answer(q)
        return await q.message.reply_text(
            "<b>Add AWS Account</b>\n\n"
            "Send the account name, or use the one-line form:\n"
            "<code>/addawsaccount &lt;name&gt; &lt;access_key&gt; &lt;secret_key&gt; &lt;regions&gt;</code>",
            parse_mode="HTML",
        )

    return await _answer(q)


def cancel_pending(user_id: int) -> bool:
    return _PENDING.pop(int(user_id), None) is not None


async def account_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.ext import ApplicationHandlerStop

    user = update.effective_user
    if not user or int(user.id) not in _PENDING:
        return
    await capture_account_input(update, context)
    raise ApplicationHandlerStop
