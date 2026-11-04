from utils.rich_ui import edit_rich as _edit, send_rich as _send


async def send_rich(bot, chat_id: int, reply_to: int | None, rich_html: str, plain_html: str, markup=None):
    return await _send(bot, chat_id, reply_to, rich_html, plain_html, markup, disable_web_page_preview=False)


async def edit_rich(bot, chat_id: int, message_id: int, rich_html: str, plain_html: str, markup=None):
    return await _edit(bot, chat_id, message_id, rich_html, plain_html, markup, disable_web_page_preview=False)
