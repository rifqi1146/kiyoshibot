import shlex
import logging
from telegram import Update
from telegram.ext import ContextTypes

from handlers.commands import COMMAND_HANDLERS

log = logging.getLogger(__name__)

_DOLLAR_CMD_MAP = {}
for names, handler, _ in COMMAND_HANDLERS:
    for name in ([names] if isinstance(names, str) else list(names)):
        _DOLLAR_CMD_MAP[name] = handler

async def dollar_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    txt = msg.text.strip()
    if not txt.startswith("$"):
        return

    try:
        parts = shlex.split(txt[1:])
    except Exception:
        parts = txt[1:].split()

    if not parts:
        return

    cmd = parts[0].lower()
    context.args = parts[1:]

    handler = _DOLLAR_CMD_MAP.get(cmd)
    if not handler:
        return

    try:
        await handler(update, context)
    except Exception:
        log.exception("Dollar command failed")