from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from utils.config import OWNER_ID
from utils.rich_common import is_message_not_modified
from utils.rich_stream import edit_rich_message, send_rich_message
from utils.text import sanitize_ai_output

CALLBACK_PREFIX = "helpowner"

HELPOWNER_TEXT = {
    "menu": (
        "### Owner Commands\n\n"
        "Administrative and system-level controls.\n\n"
        "Choose a category using the buttons below:"
    ),
    "system": (
        "### System Management\n\n"
        "- `/restart` — Restart the bot process\n"
        "- `/reload` — Hot reload handlers, utils, database, and RAG\n"
        "- `/update` — Pull latest git commits and hot reload\n"
        "- `/stats` — System resources and bot process diagnostics\n"
        "- `/speedtest` — Run server network speed test\n"
        "- `/broadcast` — Send broadcast announcement\n"
        "- `/uploadengine` — Switch upload engine (Telethon or PyroFork)\n"
        "- `/fasttelethon` — Toggle FastTelethon acceleration\n\n"
        "> Process lifecycle and system engine controls."
    ),
    "ai": (
        "### AI & Proxy Settings\n\n"
        "- `/getmodel` — List available models from OpenCode proxy\n"
        "- `/setmodel` — Select active model for `/ask`\n"
        "- `/setsearch` — Configure search engine and depth for `/ask`\n"
        "- `/thinking` — Toggle reasoning/thinking mode for `/ask`\n\n"
        "> Controls proxy settings and model routing for AI chat."
    ),
    "caca": (
        "### Caca AI Settings\n\n"
        "- `/providercaca` — Switch Caca provider (Shiteru or CodeBuddy)\n"
        "- `/cbacc` — Manage CodeBuddy account pool\n"
        "- `/freecaca` — Toggle global free Caca access\n"
        "- `/cacaadmin` — View billing overview and grant credits\n"
        "- `/addcredit` — Add credits to user (reply or @user/id)\n"
        "- `/cacaa` — Manage Caca in groups (enable, disable, status, list)\n\n"
        "> Manages AI companion provider, accounts, and credit billing."
    ),
    "access": (
        "### Access & Permissions\n\n"
        "- `/premium` — Manage premium user access\n"
        "- `/addsudo` — Grant sudo privileges to user\n"
        "- `/rmsudo` — Revoke sudo privileges from user\n"
        "- `/sudolist` — List active sudo users\n"
        "- `/blacklist` — Manage blacklisted users and chats\n"
        "- `/cookies` — Update Netscape cookies via Telegram\n\n"
        "> Sudo hierarchy and user permission controls."
    ),
    "data": (
        "### Data & Media Controls\n\n"
        "- `/backup` — Create database backup archive\n"
        "- `/restore` — Restore database from backup archive\n"
        "- `/autobackup` — Configure automatic backup scheduler\n"
        "- `/asupann` — Manage asupan sources and pool\n"
        "- `/autodel` — Configure auto-delete for asupan\n"
        "- `/wlc` — Configure group welcome messages\n\n"
        "> Backup files and automated group media handlers."
    ),
    "vps": (
        "### VPS Management\n\n"
        "- `/vpsadmin` — Open VPS owner panel (scans live AWS + bot stats + prices)\n"
        "- `/vpslist` — List every EC2 in the account across all regions (managed + manual)\n"
        "- `/vpsinfo <vps_id>` — Show full detail of a single instance\n"
        "- `/vpsterminate <vps_id>` — Terminate an instance immediately\n"
        "- `/vpsrenew <vps_id> <days>` — Extend an instance lifetime\n"
        "- `/vpsgrant <user|@username|reply> <tier> <days> [region]` — Provision a free instance\n"
        "- `/vpsstats` — Revenue, active servers, warranty claims\n"
        "- `/vpsprice <plan> <30d|14d|7d> <amount>` — Change one period\n"
        "- `/vpsprice <plan> <30d> <14d> <7d>` — Set all three periods\n"
        "- `/vpsprice` — Open interactive pricing picker\n\n"
        "> Tiers: `1g`, `2g`, `4g`, `8g`. Durations: `7d`, `14d`, `30d`."
    ),
}

def helpowner_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("System", callback_data=f"{CALLBACK_PREFIX}:system"),
            InlineKeyboardButton("AI & Proxy", callback_data=f"{CALLBACK_PREFIX}:ai"),
        ],
        [
            InlineKeyboardButton("Caca AI", callback_data=f"{CALLBACK_PREFIX}:caca"),
            InlineKeyboardButton("Access & Users", callback_data=f"{CALLBACK_PREFIX}:access"),
        ],
        [
            InlineKeyboardButton("Data & Media", callback_data=f"{CALLBACK_PREFIX}:data"),
            InlineKeyboardButton("AWS VPS", callback_data=f"{CALLBACK_PREFIX}:vps"),
        ],
        [
            InlineKeyboardButton("Close", callback_data=f"{CALLBACK_PREFIX}:close"),
        ],
    ])

def helpowner_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Back", callback_data=f"{CALLBACK_PREFIX}:menu")],
        [InlineKeyboardButton("Close", callback_data=f"{CALLBACK_PREFIX}:close")],
    ])

async def _send_helpowner(bot, chat, text_md: str, keyboard, reply_to=None):
    try:
        return await send_rich_message(
            bot,
            chat.id,
            text_md,
            reply_markup=keyboard,
            message_thread_id=getattr(reply_to, "message_thread_id", None),
            reply_to_message_id=getattr(reply_to, "message_id", None),
        )
    except Exception:
        pass
    kwargs = {
        "chat_id": chat.id,
        "text": sanitize_ai_output(text_md),
        "reply_markup": keyboard,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        kwargs["reply_to_message_id"] = reply_to.message_id
        tid = getattr(reply_to, "message_thread_id", None)
        if tid:
            kwargs["message_thread_id"] = tid
    return await bot.send_message(**kwargs)

async def _edit_helpowner(q, text_md: str, keyboard):
    msg = q.message
    try:
        return await edit_rich_message(
            q.get_bot(),
            msg.chat_id,
            msg.message_id,
            text_md,
            reply_markup=keyboard,
        )
    except Exception as e:
        if is_message_not_modified(e):
            return None
    try:
        return await q.edit_message_text(
            sanitize_ai_output(text_md),
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        if is_message_not_modified(e):
            return None
        try:
            return await q.edit_message_text(
                sanitize_ai_output(text_md),
                reply_markup=keyboard,
            )
        except Exception:
            return None

async def helpowner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if user.id not in OWNER_ID:
        return

    await _send_helpowner(
        context.bot,
        msg.chat,
        HELPOWNER_TEXT["menu"],
        helpowner_main_keyboard(),
        reply_to=msg,
    )

async def helpowner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    if not q.from_user or q.from_user.id not in OWNER_ID:
        try:
            await q.answer("This is not your menu.", show_alert=True)
        except Exception:
            pass
        return

    data = q.data
    prefix, _, action = data.partition(":")
    if prefix != CALLBACK_PREFIX:
        return

    if action == "close":
        try:
            await q.answer()
        except Exception:
            pass
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    try:
        await q.answer()
    except Exception:
        pass

    if action == "menu":
        await _edit_helpowner(
            q,
            HELPOWNER_TEXT["menu"],
            helpowner_main_keyboard(),
        )
        return

    text = HELPOWNER_TEXT.get(action)
    if text:
        await _edit_helpowner(
            q,
            text,
            helpowner_back_keyboard(),
        )
