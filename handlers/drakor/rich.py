"""Pengiriman Rich Message (Bot API 10.3) dengan fallback plain HTML.

- `send_rich(bot, ...)`  -> `sendRichMessage`, fallback `bot.send_message`.
- `edit_rich(bot, ...)`  -> `editMessageText` + `rich_message`, fallback
  `bot.edit_message_text` (plain HTML).

`do_api_request` dibungkus `warnings.catch_warnings()` karena PTB 22.8
memunculkan `PTBUserWarning` untuk endpoint yang belum punya wrapper native.
Jangan pakai `q.bot` (CallbackQuery tidak punya atribut itu) — selalu kirim
`bot` eksplisit.
"""
import logging
import warnings
from telegram import InlineKeyboardMarkup

log = logging.getLogger(__name__)


async def send_rich(bot, chat_id: int, reply_to: int | None, rich_html: str, plain_html: str, markup: InlineKeyboardMarkup):
    payload = {
        "chat_id": chat_id,
        "rich_message": {"html": rich_html},
    }
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    if markup:
        payload["reply_markup"] = markup.to_dict()

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("sendRichMessage", payload)
    except Exception as e:
        log.debug("sendRichMessage fallback | err=%r", e)

    return await bot.send_message(
        chat_id=chat_id,
        text=plain_html,
        reply_markup=markup,
        parse_mode="HTML",
        reply_to_message_id=reply_to,
        disable_web_page_preview=False,
    )


async def edit_rich(bot, chat_id: int, message_id: int, rich_html: str, plain_html: str, markup: InlineKeyboardMarkup):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": {"html": rich_html},
    }
    if markup:
        payload["reply_markup"] = markup.to_dict()

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("editMessageText", payload)
    except Exception as e:
        log.debug("editMessageText rich fallback | err=%r", e)

    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=plain_html,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
    except Exception as e:
        log.debug("edit_message_text fallback failed | err=%r", e)
        return None
