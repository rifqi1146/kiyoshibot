import asyncio
import html
import logging
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.join import require_join_or_block
from utils.menu_lock import message_id_from_send, register_message_owner

from .constants import MAX_RESULTS, PREFIX
from .render import (
    build_matches_content,
    build_menu_content,
    build_results_content,
    menu_markup,
)
from .rich import edit_rich, send_rich
from .scraper import fetch_rom_archive, resolve_device
from .state import FIRMWARE_CACHE, purge_expired_cache

log = logging.getLogger(__name__)


async def firmware_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return

    msg = update.message
    if not msg or not update.effective_user:
        return

    user_id = update.effective_user.id
    query = " ".join(context.args).strip() if context.args else ""
    purge_expired_cache()

    if not query:
        r_html, p_html = build_menu_content()
        sent = await send_rich(
            bot=context.bot,
            chat_id=msg.chat.id,
            reply_to=msg.message_id,
            rich_html=r_html,
            plain_html=p_html,
            markup=menu_markup(),
        )
        register_message_owner(msg.chat.id, message_id_from_send(sent), user_id)
        return sent

    log.info("Firmware search start | q=%r chat_id=%s user_id=%s", query, update.effective_chat.id, user_id)
    status = await msg.reply_text(
        f"🔍 Searching device <code>{html.escape(query)}</code>...",
        parse_mode="HTML",
    )
    register_message_owner(msg.chat.id, status.message_id, user_id)

    started = time.monotonic()
    codename, dev_name, matches = await asyncio.to_thread(resolve_device, query)

    if not codename and matches:
        log.info("Firmware multiple matches (%d) for query %r", len(matches), query)
        r_html, p_html, markup = build_matches_content(query, matches)
        await edit_rich(context.bot, status.chat.id, status.message_id, r_html, p_html, markup)
        return

    if not codename:
        log.info("Firmware device not found | q=%r", query)
        return await status.edit_text(
            f"Device <code>{html.escape(query)}</code> not found.\n\n"
            "Try searching by codename (e.g. <code>munch</code>, <code>alioth</code>) "
            "or commercial name (e.g. <code>poco f4</code>).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu:0:0")]
            ]),
            parse_mode="HTML",
        )

    await status.edit_text(
        f"Loading ROMs for <b>{html.escape(dev_name)}</b> (<code>{html.escape(codename)}</code>)...",
        parse_mode="HTML",
    )

    results = await asyncio.to_thread(fetch_rom_archive, codename)
    elapsed = time.monotonic() - started
    log.info(
        "Firmware search done | codename=%s dev=%r results=%d elapsed=%.1fs",
        codename, dev_name, len(results), elapsed,
    )

    if not results:
        return await status.edit_text(
            f"No ROM records available for <b>{html.escape(dev_name)}</b> (<code>{html.escape(codename)}</code>).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu:0:0")]
            ]),
            parse_mode="HTML",
        )

    session_id = uuid.uuid4().hex[:8]
    for r in results:
        r["_session_id"] = session_id
        r["_codename"] = codename

    FIRMWARE_CACHE[session_id] = {
        "title": dev_name,
        "codename": codename,
        "device_name": dev_name,
        "results": results[:MAX_RESULTS],
        "region_filter": None,
        "type_filter": None,
        "page": 0,
        "ts": time.time(),
        "user_id": user_id,
        "chat_id": update.effective_chat.id,
    }

    r_html, p_html, markup = build_results_content(session_id, FIRMWARE_CACHE[session_id])
    await edit_rich(context.bot, status.chat.id, status.message_id, r_html, p_html, markup)


firmware_cmd.__name__ = "firmware_cmd"
