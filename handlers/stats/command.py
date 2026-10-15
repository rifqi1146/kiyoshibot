import asyncio
import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

from utils.config import OWNER_ID
from .system_info import gather_system_stats_async
from .diagnostics import gather_diagnostics
from .renderer import render_dashboard
from .formatting import build_fallback_text

log = logging.getLogger(__name__)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not user or user.id not in OWNER_ID:
        return

    (stats, net_speed), diagnostics = await asyncio.gather(
        gather_system_stats_async(), gather_diagnostics(context.bot)
    )
    stats["diagnostics"] = diagnostics

    try:
        bio = await asyncio.to_thread(render_dashboard, stats, net_speed)
    except Exception:
        log.exception("/stats dashboard rendering failed")
        bio = None

    if bio:
        try:
            return await msg.reply_photo(photo=bio)
        except Exception:
            log.exception("/stats photo delivery failed; falling back to text")

    out = "<b>System Stats</b>\n\n<pre>" + html.escape(build_fallback_text(stats, net_speed)) + "</pre>"
    return await msg.reply_text(out, parse_mode="HTML")