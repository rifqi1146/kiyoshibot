"""Bot API 10.3 Rich Message helpers for MovieBox Explorer UI."""
import html
import logging
import warnings
from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError
from utils.rich_common import is_message_not_modified

log = logging.getLogger(__name__)


async def send_rich(bot, chat_id: int, reply_to: int | None, rich_html: str, plain_html: str, markup: InlineKeyboardMarkup):
    """Kirim rich message (Bot API 10.3) dengan fallback aman ke plain HTML."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            payload = {
                "chat_id": chat_id,
                "text": rich_html,
                "rich_message": {"html": rich_html},
                "reply_markup": markup.to_dict() if markup else None,
            }
            if reply_to:
                payload["reply_parameters"] = {"message_id": reply_to}
            return await bot.do_api_request("sendRichMessage", payload)
    except Exception as e:
        log.debug("sendRichMessage failed, fallback to plain HTML | err=%r", e)
        return await bot.send_message(
            chat_id=chat_id,
            text=plain_html,
            reply_to_message_id=reply_to,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )


async def edit_rich(bot, chat_id: int, message_id: int, rich_html: str, plain_html: str, markup: InlineKeyboardMarkup):
    """Edit pesan menjadi rich message dengan fallback aman ke plain HTML."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": rich_html,
                "rich_message": {"html": rich_html},
                "reply_markup": markup.to_dict() if markup else None,
            }
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
                disable_web_page_preview=True,
            )
        except TelegramError as te:
            if is_message_not_modified(te):
                return None
            raise
