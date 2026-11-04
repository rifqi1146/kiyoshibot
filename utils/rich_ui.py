import logging
import warnings
from telegram import InlineKeyboardMarkup
from utils.rich_common import is_message_not_modified

log = logging.getLogger(__name__)


def _markup_dict(markup: InlineKeyboardMarkup | None):
    if markup is None:
        return None
    if hasattr(markup, "to_dict"):
        return markup.to_dict()
    return markup if isinstance(markup, dict) else None


async def send_rich(
    bot,
    chat_id: int,
    reply_to: int | None,
    rich_html: str,
    plain_html: str,
    markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool = True,
):
    payload = {
        "chat_id": chat_id,
        "text": rich_html,
        "rich_message": {"html": rich_html},
        "reply_markup": _markup_dict(markup),
    }
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("sendRichMessage", payload)
    except Exception as e:
        log.debug("sendRichMessage failed, fallback to plain HTML | err=%r", e)

    return await bot.send_message(
        chat_id=chat_id,
        text=plain_html,
        reply_to_message_id=reply_to,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=disable_web_page_preview,
    )


async def edit_rich(
    bot,
    chat_id: int,
    message_id: int,
    rich_html: str,
    plain_html: str,
    markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool = True,
):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": rich_html,
        "rich_message": {"html": rich_html},
        "reply_markup": _markup_dict(markup),
    }

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("editMessageText", payload)
    except Exception as e:
        if is_message_not_modified(e):
            return None
        log.debug("editMessageText (rich) failed, fallback to plain HTML | err=%r", e)

    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=plain_html,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except Exception as e:
        if is_message_not_modified(e):
            return None
        log.debug("edit_message_text fallback failed | err=%r", e)
        return None
