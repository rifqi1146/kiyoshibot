import html
import inspect
import re

from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import ContextTypes

from database.moderation_db import moderation_is_enabled, sudo_is
from .auth import is_admin_or_owner, is_owner
from .helpers import (
    mention_html,
    display_name,
    display_name_from_token,
    extract_duration_target_reason,
    extract_target_reason,
    resolve_target_user_id,
    resolve_target_user_obj_for_display,
    resolve_user_obj_for_display_by_id,
    reply_in_topic,
)
from .permissions import MUTED_PERMISSIONS, UNMUTED_PERMISSIONS


async def _resolve_target_display(update: Update, context: ContextTypes.DEFAULT_TYPE, target_token: str | None):
    target_id = await resolve_target_user_id(update, context, target_token)
    if not target_id:
        return None, None

    obj = await resolve_target_user_obj_for_display(update, context, target_token)
    if not obj:
        obj = await resolve_user_obj_for_display_by_id(update, context, int(target_id))

    name = display_name(obj) or display_name_from_token(target_token)
    who = mention_html(int(target_id), name)
    return int(target_id), who

FULL_ADMIN_RIGHTS = {
    "can_manage_chat": True,
    "can_delete_messages": True,
    "can_manage_video_chats": True,
    "can_restrict_members": True,
    "can_change_info": True,
    "can_invite_users": True,
    "can_pin_messages": True,
    "can_manage_tags": True,
}

DEMOTE_ADMIN_RIGHTS = {k: False for k in FULL_ADMIN_RIGHTS}

def _clean_admin_title(raw: str | None) -> str:
    title = re.sub(r"\s+", " ", (raw or "").strip())
    if not title or title == "-":
        title = "Admin"
    return title[:16]

async def _call_supported_kwargs(method, **kwargs):
    try:
        sig = inspect.signature(method)
        params = sig.parameters
        has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if has_kwargs:
            return await method(**kwargs)
        return await method(**{k: v for k, v in kwargs.items() if k in params})
    except ValueError:
        return await method(**kwargs)

def _rights_from_admin(member) -> dict:
    return {key: bool(getattr(member, key, False)) for key in FULL_ADMIN_RIGHTS}

async def _actor_promote_rights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False, {}, "Invalid update."

    if is_owner(user.id) or sudo_is(user.id):
        return True, dict(FULL_ADMIN_RIGHTS), "owner"

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception as e:
        return False, {}, str(e)

    if member.status == "creator":
        return True, dict(FULL_ADMIN_RIGHTS), "creator"

    if member.status != "administrator":
        return False, {}, "You are not an admin."

    if not bool(getattr(member, "can_promote_members", False)):
        return False, {}, "You don't have Add New Admins permission."

    return True, _rights_from_admin(member), "admin"

async def _bot_can_promote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
        if member.status == "creator":
            return True
        return member.status == "administrator" and bool(getattr(member, "can_promote_members", False))
    except Exception:
        return False

def _clean_member_tag(raw: str | None) -> str:
    tag = re.sub(r"\s+", " ", (raw or "").strip())
    return tag[:16]

async def _actor_can_manage_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False, {}, "Invalid update."
    if is_owner(user.id) or sudo_is(user.id):
        return True, {}, "owner"
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception as e:
        return False, {}, str(e)
    if member.status == "creator":
        return True, {}, "creator"
    if member.status != "administrator":
        return False, {}, "You are not an admin."
    if not bool(getattr(member, "can_manage_tags", False)):
        return False, {}, "You don't have Manage Tags permission."
    return True, {}, "admin"

async def _bot_can_manage_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
        if member.status == "creator":
            return True
        return member.status == "administrator" and bool(getattr(member, "can_manage_tags", False))
    except Exception:
        return False

async def _target_can_be_tagged(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, int(target_id))
        return member.status in ("member", "restricted")
    except Exception:
        return True

async def _set_chat_member_tag(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, tag: str):
    method = getattr(context.bot, "set_chat_member_tag", None)
    if method:
        return await _call_supported_kwargs(
            method,
            chat_id=chat_id,
            user_id=int(user_id),
            tag=tag,
        )
    post = getattr(context.bot, "_post", None)
    if not post:
        raise RuntimeError("setChatMemberTag is not supported by your python-telegram-bot version.")
    return await post(
        "setChatMemberTag",
        data={
            "chat_id": chat_id,
            "user_id": int(user_id),
            "tag": tag,
        },
    )

async def promote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if not moderation_is_enabled(chat.id):
        return

    ok, rights, actor_type = await _actor_promote_rights(update, context)
    if not ok:
        return await reply_in_topic(msg, html.escape(actor_type), parse_mode="HTML")

    if not await _bot_can_promote(update, context):
        return await reply_in_topic(
            msg,
            "Bot needs <b>Add New Admins</b> permission.",
            parse_mode="HTML",
        )

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    target_token, title_raw = extract_target_reason(context.args or [], has_reply)
    target_id, who = await _resolve_target_display(update, context, target_token)

    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use:\n"
            "<code>/promote</code>\n"
            "<code>/promote babu</code>\n"
            "<code>/promote user_id babu</code>\n"
            "<code>/promote @username babu</code>",
            parse_mode="HTML",
        )

    title = _clean_admin_title(title_raw)

    try:
        await _call_supported_kwargs(
            context.bot.promote_chat_member,
            chat_id=chat.id,
            user_id=int(target_id),
            **rights,
        )

        title_note = ""
        try:
            await context.bot.set_chat_administrator_custom_title(
                chat_id=chat.id,
                user_id=int(target_id),
                custom_title=title,
            )
        except Exception as e:
            title_note = f"\n<b>Title:</b> failed — <code>{html.escape(str(e))}</code>"

        return await reply_in_topic(
            msg,
            "<b>Promoted</b>\n"
            f"<b>User:</b> {who}\n"
            f"<b>Title:</b> <code>{html.escape(title)}</code>\n",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )

async def demote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if not moderation_is_enabled(chat.id):
        return

    ok, _, actor_type = await _actor_promote_rights(update, context)
    if not ok:
        return await reply_in_topic(msg, html.escape(actor_type), parse_mode="HTML")

    if not await _bot_can_promote(update, context):
        return await reply_in_topic(
            msg,
            "Bot needs <b>Add New Admins</b> permission.",
            parse_mode="HTML",
        )

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    target_token, _ = extract_target_reason(context.args or [], has_reply)
    target_id, who = await _resolve_target_display(update, context, target_token)

    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use:\n"
            "<code>/demote user_id</code>\n"
            "<code>/demote @username</code>",
            parse_mode="HTML",
        )

    try:
        await _call_supported_kwargs(
            context.bot.promote_chat_member,
            chat_id=chat.id,
            user_id=int(target_id),
            **DEMOTE_ADMIN_RIGHTS,
        )

        return await reply_in_topic(
            msg,
            "<b>Demoted</b>\n"
            f"<b>User:</b> {who}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )

async def tag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if not moderation_is_enabled(chat.id):
        return

    ok, _, actor_type = await _actor_can_manage_tags(update, context)
    if not ok:
        return await reply_in_topic(msg, html.escape(actor_type), parse_mode="HTML")
    
    if not await _bot_can_manage_tags(update, context):
        return await reply_in_topic(
            msg,
            "Bot needs <b>Manage Tags</b> permission.",
            parse_mode="HTML",
        )

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    target_token, tag_raw = extract_target_reason(context.args or [], has_reply)
    target_id, who = await _resolve_target_display(update, context, target_token)

    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use:\n"
            "<code>/tag title</code>\n"
            "<code>/tag user_id title</code>\n"
            "<code>/tag @username title</code>",
            parse_mode="HTML",
        )

    tag = _clean_member_tag(tag_raw)
    if not tag:
        return await reply_in_topic(
            msg,
            "Use:\n"
            "<code>/tag title</code>\n"
            "<code>/tag user_id title</code>\n"
            "<code>/tag @username title</code>",
            parse_mode="HTML",
        )

    if not await _target_can_be_tagged(update, context, int(target_id)):
        return await reply_in_topic(
            msg,
            "Only regular members can receive member tags.",
            parse_mode="HTML",
        )

    try:
        await _set_chat_member_tag(context, chat.id, int(target_id), tag)
        return await reply_in_topic(
            msg,
            "<b>Member Tagged</b>\n"
            f"<b>User:</b> {who}\n"
            f"<b>Tag:</b> <code>{html.escape(tag)}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        
async def untag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if not moderation_is_enabled(chat.id):
        return

    ok, _, actor_type = await _actor_can_manage_tags(update, context)
    if not ok:
        return await reply_in_topic(msg, html.escape(actor_type), parse_mode="HTML")
    
    if not await _bot_can_manage_tags(update, context):
        return await reply_in_topic(
            msg,
            "Bot needs <b>Manage Tags</b> permission.",
            parse_mode="HTML",
        )

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    target_token, _ = extract_target_reason(context.args or [], has_reply)
    target_id, who = await _resolve_target_display(update, context, target_token)

    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use:\n"
            "<code>/untag user_id</code>\n"
            "<code>/untag @username</code>",
            parse_mode="HTML",
        )

    if not await _target_can_be_tagged(update, context, int(target_id)):
        return await reply_in_topic(
            msg,
            "Only regular members can have member tags removed.",
            parse_mode="HTML",
        )

    try:
        await _set_chat_member_tag(context, chat.id, int(target_id), "")
        return await reply_in_topic(
            msg,
            "<b>Member Tag Removed</b>\n"
            f"<b>User:</b> {who}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if not moderation_is_enabled(chat.id):
        return

    if not await is_admin_or_owner(update, context):
        return await reply_in_topic(msg, "You are not an admin.")

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    until, dur_human, target_token, reason = extract_duration_target_reason(context.args or [], has_reply)

    target_id, who = await _resolve_target_display(update, context, target_token)
    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use:\n"
            "<code>/ban 7d user_id toxic</code>\n"
            "<code>/ban 7d @username toxic</code>",
            parse_mode="HTML",
        )

    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=int(target_id), until_date=until)
        duration_text = f"<b>Duration:</b> {html.escape(dur_human)}\n" if dur_human else "<b>Duration:</b> Permanent\n"
        return await reply_in_topic(
            msg,
            "<b>Banned</b>\n"
            f"<b>User:</b> {who}\n"
            f"{duration_text}"
            f"<b>Reason:</b> <code>{html.escape(reason)}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if not moderation_is_enabled(chat.id):
        return

    if not await is_admin_or_owner(update, context):
        return await reply_in_topic(msg, "You are not an admin.")

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    _, _, target_token, _ = extract_duration_target_reason(context.args or [], has_reply)

    target_id, who = await _resolve_target_display(update, context, target_token)
    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use: <code>/unban user_id</code> / <code>/unban @username</code>",
            parse_mode="HTML",
        )

    try:
        await context.bot.unban_chat_member(chat_id=chat.id, user_id=int(target_id))
        return await reply_in_topic(
            msg,
            "<b>Unbanned</b>\n"
            f"<b>User:</b> {who}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if not moderation_is_enabled(chat.id):
        return

    if not await is_admin_or_owner(update, context):
        return await reply_in_topic(msg, "You are not an admin.")

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    until, dur_human, target_token, reason = extract_duration_target_reason(context.args or [], has_reply)

    target_id, who = await _resolve_target_display(update, context, target_token)
    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use:\n"
            "<code>/mute 10m user_id reason</code>\n"
            "<code>/mute 10m @username reason</code>",
            parse_mode="HTML",
        )

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=int(target_id),
            permissions=MUTED_PERMISSIONS,
            until_date=until,
        )
        duration_text = f"<b>Duration:</b> {html.escape(dur_human)}\n" if dur_human else "<b>Duration:</b> Permanent\n"
        return await reply_in_topic(
            msg,
            "<b>Muted</b>\n"
            f"<b>User:</b> {who}\n"
            f"{duration_text}"
            f"<b>Reason:</b> <code>{html.escape(reason)}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if not moderation_is_enabled(chat.id):
        return

    if not await is_admin_or_owner(update, context):
        return await reply_in_topic(msg, "You are not an admin.")

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    _, _, target_token, _ = extract_duration_target_reason(context.args or [], has_reply)

    target_id, who = await _resolve_target_display(update, context, target_token)
    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use: <code>/unmute user_id</code> / <code>/unmute @username</code>",
            parse_mode="HTML",
        )

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=int(target_id),
            permissions=UNMUTED_PERMISSIONS,
            until_date=None,
        )
        return await reply_in_topic(
            msg,
            "<b>Unmuted</b>\n"
            f"<b>User:</b> {who}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )


async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if not moderation_is_enabled(chat.id):
        return

    if not await is_admin_or_owner(update, context):
        return await reply_in_topic(msg, "You are not an admin.")

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    target_token, reason = extract_target_reason(context.args or [], has_reply)

    target_id, who = await _resolve_target_display(update, context, target_token)
    if not target_id:
        return await reply_in_topic(
            msg,
            "Reply to a user or use:\n"
            "<code>/kick userid reason</code>\n"
            "<code>/kick @username reason</code>",
            parse_mode="HTML",
        )

    try:
        until = datetime.now(timezone.utc) + timedelta(seconds=45)
        await context.bot.ban_chat_member(
            chat_id=chat.id,
            user_id=int(target_id),
            until_date=until,
        )
        await context.bot.unban_chat_member(
            chat_id=chat.id,
            user_id=int(target_id),
            only_if_banned=True,
        )
        return await reply_in_topic(
            msg,
            "<b>Kicked</b>\n"
            f"<b>User:</b> {who}\n"
            f"<b>Reason:</b> <code>{html.escape(reason)}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        return await reply_in_topic(
            msg,
            f"Failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )