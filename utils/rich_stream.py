import asyncio
import html as html_lib
import time
from typing import AsyncIterator
from telegram import Bot

from .rich_common import is_message_not_modified
from .text import strip_code_wrapper

DEFAULT_DRAFT_INTERVAL = 0.8
DEFAULT_THINKING_HINT = "Thinking..."
MAX_DRAFT_CHARS = 30000


def draft_thinking_md(hint: str | None = None) -> str:
    hint = html_lib.escape(hint or DEFAULT_THINKING_HINT)
    return f"<tg-thinking>{hint}</tg-thinking>"


def draft_html(text: str, hint: str | None = None) -> str:
    body = strip_code_wrapper(text or "")
    if not body:
        return draft_thinking_md(hint)
    return f"{body}\n\n{draft_thinking_md(hint)}"

def draft_markdown(text: str, hint: str | None = None) -> str:
    from utils.text import convert_bullets
    body = convert_bullets((text or "").strip())
    if not body:
        return draft_thinking_md(hint)
    if body.count("```") % 2 == 1:
        return body
    return f"{body}\n\n{draft_thinking_md(hint)}"


async def send_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    rich_markdown: str,
    message_thread_id: int | None = None,
    can_stop: bool = True,
    is_rtl: bool = False,
    rich_html: str | None = None,
) -> None:
    rich_message: dict = {"html": rich_html} if rich_html else {"markdown": rich_markdown}
    if is_rtl:
        rich_message["is_rtl"] = True
    payload: dict = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": rich_message,
        "can_stop": can_stop,
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    await bot.do_api_request("sendRichMessageDraft", payload)


async def stream_to_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    text_iter: AsyncIterator[str],
    message_thread_id: int | None = None,
    interval: float = DEFAULT_DRAFT_INTERVAL,
    can_stop: bool = True,
    is_rtl: bool = False,
    thinking_hint: str | None = None,
    stop_event: asyncio.Event | None = None,
    max_chars: int = MAX_DRAFT_CHARS,
    html_mode: bool = False,
) -> str:
    build = draft_html if html_mode else draft_markdown
    try:
        await send_draft(
            bot, chat_id, draft_id, draft_thinking_md(thinking_hint),
            message_thread_id, can_stop, is_rtl,
        )
    except Exception as e:
        raise RuntimeError(f"Rich draft not supported in this chat: {e}")

    acc = ""
    last_sent = time.monotonic()
    stop_event = stop_event or asyncio.Event()
    async for delta in text_iter:
        if stop_event.is_set():
            break
        if delta:
            acc = (acc + delta)[:max_chars]
        now = time.monotonic()
        if acc and now - last_sent >= interval:
            try:
                await send_draft(
                    bot, chat_id, draft_id,
                    build(acc, thinking_hint),
                    message_thread_id, can_stop, is_rtl,
                    rich_html=build(acc, thinking_hint) if html_mode else None,
                )
                last_sent = now
            except Exception:
                pass
    if acc:
        try:
            await send_draft(
                bot, chat_id, draft_id, build(acc, thinking_hint),
                message_thread_id, can_stop, is_rtl,
                rich_html=build(acc, thinking_hint) if html_mode else None,
            )
        except Exception:
            pass
    return acc


def _markup_to_dict(reply_markup):
    if reply_markup is None:
        return None
    if hasattr(reply_markup, "to_dict"):
        return reply_markup.to_dict()
    if isinstance(reply_markup, dict):
        return reply_markup
    return None


async def send_rich_message(
    bot: Bot,
    chat_id: int,
    rich_markdown: str,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    is_rtl: bool = False,
    reply_markup=None,
    rich_html: str | None = None,
):
    rich_message: dict = {"html": rich_html} if rich_html else {"markdown": rich_markdown}
    if is_rtl:
        rich_message["is_rtl"] = True
    payload: dict = {"chat_id": chat_id, "rich_message": rich_message}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    markup = _markup_to_dict(reply_markup)
    if markup:
        payload["reply_markup"] = markup
    return await bot.do_api_request("sendRichMessage", payload)


async def edit_rich_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    rich_markdown: str,
    is_rtl: bool = False,
    reply_markup=None,
    rich_html: str | None = None,
):
    rich_message: dict = {"html": rich_html} if rich_html else {"markdown": rich_markdown}
    if is_rtl:
        rich_message["is_rtl"] = True
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": rich_message,
    }
    markup = _markup_to_dict(reply_markup)
    if markup:
        payload["reply_markup"] = markup
    try:
        return await bot.do_api_request("editMessageText", payload)
    except Exception as e:
        if is_message_not_modified(e):
            return None
        raise


def is_rich_message(message) -> bool:
    api_kwargs = getattr(message, "api_kwargs", None) or {}
    return "rich_message" in api_kwargs


DEFAULT_CAPTION_SUMMARY = "Show Caption"


def _caption_block(caption: str, summary: str | None = None, collapsible: bool = True) -> dict:
    if collapsible:
        return {
            "type": "details",
            "summary": (summary or DEFAULT_CAPTION_SUMMARY),
            "blocks": [{"type": "paragraph", "text": caption}],
            "is_open": False,
        }
    return {
        "type": "blockquote",
        "blocks": [{"type": "paragraph", "text": caption}],
    }


async def send_rich_slideshow(
    bot: Bot,
    chat_id: int,
    photos: list[str],
    caption: str | None = None,
    credit: str | None = None,
    heading: str | None = None,
    summary: str | None = None,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    reply_markup=None,
):
    import json as _json
    import os as _os

    from utils.http import get_http_session

    attach: list[tuple[str, str]] = []
    slides: list[dict] = []
    for i, item in enumerate(photos):
        if item and _os.path.isfile(item):
            field = f"photo{i}"
            attach.append((field, item))
            slides.append({"type": "photo", "photo": {"type": "photo", "media": f"attach://{field}"}})
        else:
            slides.append({"type": "photo", "photo": {"type": "photo", "media": item}})

    def build_blocks(collapsible: bool) -> list[dict]:
        blocks: list[dict] = []
        if heading:
            blocks.append({"type": "heading", "text": heading, "size": 3})
        blocks.append({"type": "slideshow", "blocks": slides})
        if caption:
            blocks.append(_caption_block(caption, summary=summary, collapsible=collapsible))
        if credit:
            blocks.append({"type": "footer", "text": credit})
        return blocks

    def build_payload(blocks: list[dict]) -> dict:
        payload: dict = {"chat_id": chat_id, "rich_message": {"blocks": blocks}}
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        markup = _markup_to_dict(reply_markup)
        if markup:
            payload["reply_markup"] = markup
        return payload

    async def post(payload: dict):
        if not attach:
            res = await bot.do_api_request("sendRichMessage", payload)
            if isinstance(res, dict) and res.get("error_code"):
                raise RuntimeError(res.get("description") or "sendRichMessage failed")
            return res
        import aiohttp

        form = aiohttp.FormData()
        for key, val in payload.items():
            form.add_field(key, _json.dumps(val) if isinstance(val, (dict, list)) else str(val))
        for field, path in attach:
            with open(path, "rb") as fh:
                data = fh.read()
            form.add_field(
                field, data,
                filename=_os.path.basename(path),
                content_type="application/octet-stream",
            )
        session = await get_http_session()
        url = f"{bot.base_url}/sendRichMessage"
        async with session.post(url, data=form) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "sendRichMessage failed")
        return data.get("result")

    try:
        return await post(build_payload(build_blocks(collapsible=bool(caption))))
    except Exception as e:
        err = str(e).lower()
        if caption and ("unsupported" in err or "can't parse" in err):
            return await post(build_payload(build_blocks(collapsible=False)))
        raise
