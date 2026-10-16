import asyncio
import logging
import time
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils.menu_lock import guard_callback_owner

from .constants import MAX_RESULTS, PREFIX, REGIONS
from .render import (
    build_menu_content,
    build_mirrors_content,
    build_region_picker_content,
    build_results_content,
    build_search_prompt_content,
    menu_markup,
)
from .rich import edit_rich
from .scraper import fetch_devices_map, fetch_rom_archive, fetch_rom_mirrors
from .state import FIRMWARE_CACHE

log = logging.getLogger(__name__)


async def firmware_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data or not q.data.startswith(f"{PREFIX}:"):
        return

    if not await guard_callback_owner(q):
        return

    bot = getattr(context, "bot", None) or (q.get_bot() if hasattr(q, "get_bot") else None)
    chat_id = q.message.chat.id
    message_id = q.message.message_id

    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("Closed.")
        return await q.answer()

    if action == "prompt":
        r_html, p_html, markup = build_search_prompt_content()
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "menu":
        r_html, p_html = build_menu_content()
        await edit_rich(bot, chat_id, message_id, r_html, p_html, menu_markup())
        return await q.answer()

    if action == "open":
        codename = parts[2] if len(parts) > 2 else ""
        if not codename:
            return await q.answer("Invalid device.", show_alert=True)

        devs = fetch_devices_map()
        dev_name = devs.get(codename, codename)

        await q.answer(f"Loading {dev_name}...")
        results = await asyncio.to_thread(fetch_rom_archive, codename)
        if not results:
            return await q.edit_message_text(
                f"No ROM records available for <b>{dev_name}</b> (<code>{codename}</code>).",
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
            "user_id": q.from_user.id if q.from_user else None,
            "chat_id": chat_id,
        }

        r_html, p_html, markup = build_results_content(session_id, FIRMWARE_CACHE[session_id])
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action == "nav":
        session_id = parts[2] if len(parts) > 2 else ""
        try:
            page = int(parts[3])
        except (IndexError, ValueError):
            page = 0

        data = FIRMWARE_CACHE.get(session_id)
        if not data:
            return await q.answer("Session expired. Please search again.", show_alert=True)

        data["page"] = page
        data["ts"] = time.time()
        r_html, p_html, markup = build_results_content(session_id, data)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "regions":
        session_id = parts[2] if len(parts) > 2 else ""
        data = FIRMWARE_CACHE.get(session_id)
        if not data:
            return await q.answer("Session expired. Please search again.", show_alert=True)

        r_html, p_html, markup = build_region_picker_content(session_id, data)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    if action == "regf":
        session_id = parts[2] if len(parts) > 2 else ""
        region_key = parts[3] if len(parts) > 3 else "all"

        data = FIRMWARE_CACHE.get(session_id)
        if not data:
            return await q.answer("Session expired. Please search again.", show_alert=True)

        data["region_filter"] = None if region_key == "all" else region_key
        data["page"] = 0
        data["ts"] = time.time()

        r_html, p_html, markup = build_results_content(session_id, data)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)

        label = "All Regions" if region_key == "all" else REGIONS.get(region_key, ("Region", ""))[0]
        return await q.answer(f"Filter: {label}")

    if action == "mirrors":
        session_id = parts[2] if len(parts) > 2 else ""
        try:
            idx = int(parts[3])
        except (IndexError, ValueError):
            idx = 0

        data = FIRMWARE_CACHE.get(session_id)
        if not data:
            return await q.answer("Session expired. Please search again.", show_alert=True)

        from .render import _filter_items
        filtered = _filter_items(data.get("results") or [], data.get("region_filter"), data.get("type_filter"))
        if not (0 <= idx < len(filtered)):
            return await q.answer("ROM not found.", show_alert=True)

        item = filtered[idx]
        detail_path = item.get("detail_path") or ""

        await q.answer("Loading mirrors...")
        blocks = await asyncio.to_thread(fetch_rom_mirrors, detail_path)

        r_html, p_html, markup = build_mirrors_content(item, blocks)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return

    if action in ("back", "list"):
        session_id = parts[2] if len(parts) > 2 else ""
        data = FIRMWARE_CACHE.get(session_id)
        if not data:
            return await q.answer("Session expired. Please search again.", show_alert=True)

        r_html, p_html, markup = build_results_content(session_id, data)
        await edit_rich(bot, chat_id, message_id, r_html, p_html, markup)
        return await q.answer()

    return await q.answer()
