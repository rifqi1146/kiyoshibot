from __future__ import annotations

import html
import logging
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.caca import codebuddy, codebuddy_store
from utils.config import OWNER_ID
from utils.menu_lock import guard_callback_owner, register_message_owner

from . import llm

log = logging.getLogger(__name__)

CALLBACK_PREFIX = "askprov"


def _account_lines() -> list[str]:
    accounts = codebuddy.account_summaries()
    if not accounts:
        return ["  <i>No CodeBuddy account saved yet.</i>"]
    now = int(time.time())
    lines = []
    for acc in accounts:
        flags = []
        refresh_exp = int(acc.get("refresh_exp") or 0)
        access_exp = int(acc.get("access_exp") or 0)
        if acc.get("has_refresh"):
            flags.append("RT" if not refresh_exp or refresh_exp > now else "RT-expired")
        if acc.get("has_access"):
            flags.append("AT" if not access_exp or access_exp > now else "AT-expired")
        if acc.get("disabled"):
            flags.append("off")
        label = html.escape(str(acc.get("label") or acc.get("key") or "unknown"))
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {acc['index']}. <code>{label}</code>{suffix}")
    return lines


def _status_text(current: str) -> str:
    active_accounts = [a for a in codebuddy.account_summaries() if not a.get("disabled")]
    cb_ready = f"{len(active_accounts)} account(s)" if active_accounts else "no account yet"
    sh_ready = "ready" if llm.shiteru_ready() else "no SHITERU_KEY in .env"

    active_line = ""
    if active_accounts:
        label = html.escape(str(active_accounts[0].get("label") or ""))
        active_line = f"\n<b>Next CodeBuddy account:</b> <code>{label}</code>"

    return (
        "<b>Ask Provider Switch</b>\n\n"
        f"<b>Active provider:</b> <code>{html.escape(current)}</code>\n"
        f"<b>Model:</b> <code>{html.escape(llm.model_label())}</code>\n\n"
        "<b>CodeBuddy accounts:</b>\n"
        + "\n".join(_account_lines())
        + f"{active_line}\n\n"
        f"<b>Shiteru:</b> <code>{sh_ready}</code>\n\n"
        "<i>Pick a provider below.</i>"
    )


def _keyboard(current: str) -> InlineKeyboardMarkup:
    def label(name: str, ready: bool) -> str:
        mark = " · active" if name == current else ""
        if not ready:
            mark = " (not configured)"
        return f"{name.capitalize()}{mark}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("shiteru", llm.shiteru_ready()), callback_data=f"{CALLBACK_PREFIX}:set:shiteru")],
        [InlineKeyboardButton(label("codebuddy", llm.codebuddy_ready()), callback_data=f"{CALLBACK_PREFIX}:set:codebuddy")],
        [InlineKeyboardButton("Refresh Status", callback_data=f"{CALLBACK_PREFIX}:status:0")],
        [InlineKeyboardButton("Close", callback_data=f"{CALLBACK_PREFIX}:close:0")],
    ])


async def _repair_missing_emails():
    codebuddy_store.repair_labels()
    for acc in codebuddy.account_summaries():
        if not acc.get("email") and acc.get("has_refresh"):
            key = acc.get("key")
            for full in codebuddy_store.all_accounts():
                if full.get("key") == key and full.get("refresh_token"):
                    try:
                        await codebuddy.bootstrap_account(full)
                    except Exception as e:
                        log.debug("ask provider email repair failed | account=%s err=%r", key, e)
                    break


async def providerask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.id not in OWNER_ID:
        return

    args = list(context.args or [])
    if args:
        target = (args[0] or "").strip().lower()
        if target not in ("shiteru", "codebuddy"):
            return await msg.reply_text(
                "<b>Usage:</b> <code>/providerask shiteru|codebuddy</code>",
                parse_mode="HTML",
            )
        if target == "codebuddy" and not llm.codebuddy_ready():
            return await msg.reply_text(
                "<b>Cannot switch:</b> no CodeBuddy account in the pool. Add one via <code>/cbacc add</code>.",
                parse_mode="HTML",
            )
        if target == "shiteru" and not llm.shiteru_ready():
            return await msg.reply_text(
                "<b>Cannot switch:</b> <code>SHITERU_KEY</code> is missing in .env.",
                parse_mode="HTML",
            )
        if not llm.set_provider(target):
            return await msg.reply_text("<b>Failed to save provider choice.</b>", parse_mode="HTML")
        log.info("Ask provider switched via command | user_id=%s provider=%s", user.id, target)
        return await msg.reply_text(
            f"<b>Ask provider:</b> <code>{html.escape(target)}</code>\n"
            f"<b>Model:</b> <code>{html.escape(llm.model_label())}</code>",
            parse_mode="HTML",
        )

    await _repair_missing_emails()
    current = llm.get_provider()
    sent = await msg.reply_text(
        _status_text(current),
        parse_mode="HTML",
        reply_markup=_keyboard(current),
    )
    register_message_owner(msg.chat_id, sent.message_id, user.id)
    return sent


async def providerask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    user = update.effective_user
    if not user or user.id not in OWNER_ID:
        return await q.answer("Owner only.", show_alert=True)
    if not await guard_callback_owner(q):
        return

    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            pass
        return await q.answer()

    if action == "status":
        current = llm.get_provider()
        await q.answer("Status refreshed.")
        try:
            await q.edit_message_text(
                _status_text(current),
                parse_mode="HTML",
                reply_markup=_keyboard(current),
            )
        except Exception:
            pass
        return

    if action == "set":
        target = parts[2] if len(parts) > 2 else ""
        if target not in ("shiteru", "codebuddy"):
            return await q.answer("Invalid provider.", show_alert=True)
        if target == "codebuddy" and not llm.codebuddy_ready():
            return await q.answer(
                "Cannot switch: no CodeBuddy account in the pool.",
                show_alert=True,
            )
        if target == "shiteru" and not llm.shiteru_ready():
            return await q.answer(
                "Cannot switch: SHITERU_KEY is missing in .env.",
                show_alert=True,
            )
        if not llm.set_provider(target):
            return await q.answer("Failed to save provider choice.", show_alert=True)

        log.info("Ask provider switched via inline | user_id=%s provider=%s", user.id, target)
        await q.answer(f"Switched to {target.capitalize()}!", show_alert=True)
        try:
            await q.edit_message_text(
                _status_text(target),
                parse_mode="HTML",
                reply_markup=_keyboard(target),
            )
        except Exception:
            pass
        return

    return await q.answer()
