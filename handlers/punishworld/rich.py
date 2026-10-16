import logging
import warnings

from telegram import InlineKeyboardMarkup

from utils.rich_common import is_message_not_modified

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
        disable_web_page_preview=True,
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
        if is_message_not_modified(e):
            log.debug("editMessageText rich not modified, no-op")
            return None
        log.debug("editMessageText rich fallback | err=%r", e)

    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=plain_html,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.debug("edit_message_text fallback failed | err=%r", e)
        return None
