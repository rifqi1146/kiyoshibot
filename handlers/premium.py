from telegram import Update
from telegram.ext import ContextTypes
from utils.config import OWNER_ID
from database import premium, caca_db
from utils import caca_memory
from handlers.moderation.helpers import (
    resolve_target_user_id,
    resolve_target_user_obj_for_display,
    resolve_user_obj_for_display_by_id,
    display_name,
    display_name_from_token,
    mention_html,
    reply_in_topic,
)

async def _resolve_target_display(update: Update, context: ContextTypes.DEFAULT_TYPE, target_token: str | None):
    target_id = await resolve_target_user_id(update, context, target_token)
    if not target_id:
        return None, None
    obj = await resolve_target_user_obj_for_display(update, context, target_token)
    if not obj:
        obj = await resolve_user_obj_for_display_by_id(update, context, int(target_id))
    name = display_name(obj) or display_name_from_token(target_token) or "Unknown User"
    who = mention_html(int(target_id), name)
    return int(target_id), who

def _get_target_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    msg = update.message
    if not msg:
        return None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return None
    if len(context.args) >= 2:
        token = (context.args[1] or "").strip()
        return token or None
    return None

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not msg or not user or user.id not in OWNER_ID:
        return

    if not context.args:
        return await reply_in_topic(
            msg,
            "<b>Premium Control</b>\n\n"
            "<code>/premium add &lt;user_id | @username&gt;</code>\n"
            "<code>/premium del &lt;user_id | @username&gt;</code>\n"
            "<code>/premium list</code>",
            parse_mode="HTML",
        )

    cmd = (context.args[0] or "").lower().strip()

    if cmd in ("add", "del"):
        target_token = _get_target_token(update, context)
        uid, who = await _resolve_target_display(update, context, target_token)
        if not uid:
            return await reply_in_topic(
                msg,
                "Reply ke user atau pakai:\n"
                "<code>/premium add 123456</code>\n"
                "<code>/premium add @username</code>\n"
                "<code>/premium del 123456</code>\n"
                "<code>/premium del @username</code>",
                parse_mode="HTML",
            )

        if cmd == "add":
            if uid in OWNER_ID:
                return await reply_in_topic(
                    msg,
                    "<b>User ini owner.</b>\n"
                    f"<b>User:</b> {who}\n"
                    "<i>Owner selalu premium otomatis.</i>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            premium.add(uid)
            return await reply_in_topic(
                msg,
                "<b>Premium added</b>\n"
                f"<b>User:</b> {who}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        if uid in OWNER_ID:
            return await reply_in_topic(
                msg,
                "<b>Cannot remove owner premium.</b>\n"
                f"<b>User:</b> {who}\n"
                "<i>Owner always premium.</i>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        premium.remove(uid)
        caca_db.remove_mode(uid)
        await caca_memory.clear(uid)
        await caca_memory.clear_last_message_id(uid)
        return await reply_in_topic(
            msg,
            "<b>Premium removed</b>\n"
            f"<b>User:</b> {who}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    if cmd == "list":
        ids = premium.list_users()
        if not ids:
            return await reply_in_topic(msg, "No premium users yet.", parse_mode="HTML")

        owner_lines = []
        premium_lines = []

        for uid in ids[:200]:
            obj = await resolve_user_obj_for_display_by_id(update, context, int(uid))
            name = display_name(obj) or "Unknown User"
            line = f"• {mention_html(int(uid), name)} <code>{uid}</code>"
            if uid in OWNER_ID:
                owner_lines.append(f"{line} <b>[OWNER]</b>")
            else:
                premium_lines.append(line)

        parts = ["👑 <b>Premium Users</b>"]
        if owner_lines:
            parts.append("")
            parts.append("<b>Owner</b>")
            parts.extend(owner_lines)
        if premium_lines:
            parts.append("")
            parts.append("<b>Additional Premium</b>")
            parts.extend(premium_lines)

        return await reply_in_topic(
            msg,
            "\n".join(parts),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    return await reply_in_topic(
        msg,
        "<b>Unknown command.</b>\n\n"
        "Use:\n"
        "<code>/premium add</code>\n"
        "<code>/premium del</code>\n"
        "<code>/premium list</code>",
        parse_mode="HTML",
    )