from telegram import Update
from telegram.ext import ContextTypes


async def stopped_generation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tangani update stopped_message_generation (Bot API 10.3).

    PTB 22.8 belum mengenali field ini, jadi payload mentah ada di
    update.api_kwargs["stopped_message_generation"].
    """
    payload = None
    api_kwargs = getattr(update, "api_kwargs", None)
    get = getattr(api_kwargs, "get", None)
    if callable(get):
        payload = get("stopped_message_generation")
    if not payload or not hasattr(payload, "get"):
        return

    chat = payload.get("chat") or {}
    chat_id = chat.get("id")
    draft_id = payload.get("draft_id")
    if chat_id is None or draft_id is None:
        return

    app = getattr(context, "application", None)
    if not app:
        return
    key = f"ask_draft_stop:{int(chat_id)}:{int(draft_id)}"
    ev = app.bot_data.get(key)
    if ev is not None:
        ev.set()
