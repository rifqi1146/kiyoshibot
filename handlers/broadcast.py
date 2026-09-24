import time
import uuid
import asyncio

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import RetryAfter
from utils.config import OWNER_ID
from database.db import db_session

BROADCAST_DB = "data/broadcast.sqlite3"
BROADCAST_PENDING = {}


def _db_init():
    with db_session(BROADCAST_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_users (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_groups (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            )
        """)
        con.commit()


def _get_targets_sync(mode: str) -> list[int]:
    with db_session(BROADCAST_DB) as con:
        if mode == "users":
            rows = con.execute(
                "SELECT chat_id FROM broadcast_users WHERE enabled=1"
            ).fetchall()
            return [int(r[0]) for r in rows if r and r[0] is not None]
        if mode == "groups":
            rows = con.execute(
                "SELECT chat_id FROM broadcast_groups WHERE enabled=1"
            ).fetchall()
            return [int(r[0]) for r in rows if r and r[0] is not None]
        rows = con.execute(
            "SELECT chat_id FROM broadcast_users WHERE enabled=1 "
            "UNION SELECT chat_id FROM broadcast_groups WHERE enabled=1"
        ).fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]


async def _get_targets(mode: str) -> list[int]:
    return await asyncio.to_thread(_get_targets_sync, mode)


def _broadcast_keyboard(bid: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Send Users", callback_data=f"broadcast:send:users:{bid}"),
            InlineKeyboardButton("Send Groups", callback_data=f"broadcast:send:groups:{bid}"),
        ],
        [
            InlineKeyboardButton("Send All", callback_data=f"broadcast:send:all:{bid}"),
            InlineKeyboardButton("Cancel", callback_data=f"broadcast:cancel:{bid}"),
        ]
    ])


def _cleanup_pending(max_age: int = 3600):
    now = time.time()
    expired = [
        key for key, value in BROADCAST_PENDING.items()
        if now - float(value.get("ts", 0)) > max_age
    ]
    for key in expired:
        BROADCAST_PENDING.pop(key, None)


def _mode_label(mode: str) -> str:
    if mode == "users":
        return "Users Only"
    if mode == "groups":
        return "Groups Only"
    return "All Targets"


def _extract_broadcast_text(msg) -> str:
    raw_text = msg.text or msg.caption or ""
    raw_text = raw_text.strip()

    if raw_text.startswith("/broadcast"):
        return raw_text[len("/broadcast"):].lstrip()

    return raw_text


def _extract_broadcast_payload(msg):
    text = _extract_broadcast_text(msg)

    if msg.photo:
        return {
            "kind": "photo",
            "file_id": msg.photo[-1].file_id,
            "text": text,
        }

    if msg.reply_to_message and msg.reply_to_message.photo:
        return {
            "kind": "photo",
            "file_id": msg.reply_to_message.photo[-1].file_id,
            "text": text,
        }

    return {
        "kind": "text",
        "text": text,
    }


async def _send_payload(bot, chat_id: int, payload: dict):
    kind = payload.get("kind", "text")
    text = payload.get("text", "") or ""

    if kind == "photo":
        await bot.send_photo(
            chat_id=chat_id,
            photo=payload["file_id"],
            caption=text or None,
            parse_mode="HTML",
            disable_notification=True,
        )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        disable_notification=True,
    )


async def _edit_preview_message(message, payload: dict, mode: str | None = None, finished: tuple[int, int] | None = None):
    kind = payload.get("kind", "text")
    text = payload.get("text", "") or ""

    header = "<b>Broadcast Preview</b>"
    if mode:
        header = "<b>Broadcast started...</b>"
    if finished:
        sent, failed = finished
        header = (
            "<b>Broadcast finished</b>\n\n"
            f"<b>Target:</b> {_mode_label(mode)}\n"
            f"Sent: <b>{sent}</b>\n"
            f"Failed: <b>{failed}</b>"
        )

    if kind == "photo":
        caption_parts = [header]
        if mode and not finished:
            caption_parts.append(f"<b>Target:</b> {_mode_label(mode)}")
        if text:
            caption_parts.append(text)
        if not mode and not finished:
            caption_parts.append("<i>This message has not been sent yet.</i>\n<i>Select target below.</i>")
        caption = "\n\n".join(caption_parts)

        await message.edit_caption(
            caption=caption,
            parse_mode="HTML",
            reply_markup=None if mode or finished else _broadcast_keyboard(payload["bid"]),
        )
        return

    text_parts = [header]
    if mode and not finished:
        text_parts.append(f"<b>Target:</b> {_mode_label(mode)}")
    if text:
        text_parts.append(text)
    if not mode and not finished:
        text_parts.append("<i>This message has not been sent yet.</i>\n<i>Select target below.</i>")

    final_text = "\n\n".join(text_parts)
    await message.edit_text(
        final_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=None if mode or finished else _broadcast_keyboard(payload["bid"]),
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    if not user or user.id not in OWNER_ID:
        return

    if not msg:
        return

    payload = _extract_broadcast_payload(msg)
    text = payload.get("text", "") or ""

    if payload["kind"] == "text" and not text:
        return await msg.reply_text("Message is empty.")

    _cleanup_pending()

    bid = uuid.uuid4().hex[:10]
    payload["bid"] = bid

    BROADCAST_PENDING[bid] = {
        "owner_id": user.id,
        "payload": payload,
        "ts": time.time(),
    }

    if payload["kind"] == "photo":
        await msg.reply_photo(
            photo=payload["file_id"],
            caption=(
                "<b>Broadcast Preview</b>\n\n"
                + (f"{text}\n\n" if text else "")
                + "<i>This message has not been sent yet.</i>\n"
                + "<i>Select target below.</i>"
            ),
            parse_mode="HTML",
            reply_markup=_broadcast_keyboard(bid),
        )
        return

    await msg.reply_text(
        "<b>Broadcast Preview</b>\n\n"
        f"{text}\n\n"
        "<i>This message has not been sent yet.</i>\n"
        "<i>Select target below.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_broadcast_keyboard(bid),
    )


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    parts = q.data.split(":")
    if len(parts) < 3 or parts[0] != "broadcast":
        return

    user = q.from_user

    if parts[1] == "cancel":
        if len(parts) != 3:
            return

        bid = parts[2]
        data = BROADCAST_PENDING.get(bid)

        if not data:
            await q.answer("Broadcast request expired.", show_alert=True)
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        if not user or user.id != data["owner_id"] or user.id not in OWNER_ID:
            return await q.answer("This is not your broadcast.", show_alert=True)

        BROADCAST_PENDING.pop(bid, None)
        await q.answer("Broadcast cancelled.")

        try:
            return await q.message.edit_text(
                "<b>Broadcast cancelled</b>",
                parse_mode="HTML",
            )
        except Exception:
            try:
                return await q.message.edit_caption(
                    caption="<b>Broadcast cancelled</b>",
                    parse_mode="HTML",
                )
            except Exception:
                return

    if len(parts) != 4 or parts[1] != "send":
        return await q.answer()

    mode = parts[2]
    bid = parts[3]

    if mode not in ("users", "groups", "all"):
        return await q.answer("Invalid target mode.", show_alert=True)

    data = BROADCAST_PENDING.get(bid)
    if not data:
        await q.answer("Broadcast request expired.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if not user or user.id != data["owner_id"] or user.id not in OWNER_ID:
        return await q.answer("This is not your broadcast.", show_alert=True)

    payload = data["payload"]
    targets = await _get_targets(mode)

    if not targets:
        BROADCAST_PENDING.pop(bid, None)
        await q.answer()
        try:
            return await q.message.edit_text(
                f"<b>Broadcast aborted</b>\n\nNo targets found for <b>{_mode_label(mode)}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            try:
                return await q.message.edit_caption(
                    caption=f"<b>Broadcast aborted</b>\n\nNo targets found for <b>{_mode_label(mode)}</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                return

    await q.answer(f"Starting broadcast to {_mode_label(mode)}...")
    await _edit_preview_message(q.message, payload, mode=mode)

    sent = 0
    failed = 0

    for cid in targets:
        try:
            await _send_payload(context.bot, cid, payload)
            sent += 1
            await asyncio.sleep(0.7)

        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1)) + 1)
            try:
                await _send_payload(context.bot, cid, payload)
                sent += 1
            except Exception:
                failed += 1

        except Exception:
            failed += 1
            await asyncio.sleep(0.7)

    BROADCAST_PENDING.pop(bid, None)

    try:
        if payload.get("kind") == "photo":
            text = payload.get("text", "") or ""
            caption = (
                "<b>Broadcast finished</b>\n\n"
                f"<b>Target:</b> {_mode_label(mode)}\n"
                f"Sent: <b>{sent}</b>\n"
                f"Failed: <b>{failed}</b>"
            )
            if text:
                caption += f"\n\n{text}"

            await q.message.edit_caption(
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await q.message.edit_text(
                "<b>Broadcast finished</b>\n\n"
                f"<b>Target:</b> {_mode_label(mode)}\n"
                f"Sent: <b>{sent}</b>\n"
                f"Failed: <b>{failed}</b>\n\n"
                "<b>Message:</b>\n"
                f"{payload.get('text', '')}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    except Exception:
        pass


try:
    _db_init()
except Exception:
    pass