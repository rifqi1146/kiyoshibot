import asyncio
import html as html_lib
import time
from typing import AsyncIterator
from telegram import Bot

DEFAULT_DRAFT_INTERVAL = 0.8
DEFAULT_THINKING_HINT = "Sedang mikir..."
MAX_DRAFT_CHARS = 30000


def draft_thinking_md(hint: str | None = None) -> str:
    hint = html_lib.escape(hint or DEFAULT_THINKING_HINT)
    return f"<tg-thinking>{hint}</tg-thinking>"


def draft_markdown(text: str, hint: str | None = None) -> str:
    """Gabung teks stream (markdown mentah) dengan blok thinking.

    Blok thinking tidak ditempel kalau jumlah fence ``` ganjil (code block
    belum tertutup), supaya tidak masuk ke dalam code block.
    """
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
) -> None:
    rich_message: dict = {"markdown": rich_markdown}
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
) -> str:
    """
    Tampilkan thinking draft, lalu update draft tiap `interval` detik
    dengan teks markdown terakumulasi. Return teks final (markdown mentah).

    Raise RuntimeError jika draft tidak didukung di chat ini.
    Exception dari text_iter (generator) diteruskan ke pemanggil.
    """
    try:
        await send_draft(
            bot, chat_id, draft_id, draft_thinking_md(thinking_hint),
            message_thread_id, can_stop, is_rtl,
        )
    except Exception as e:
        raise RuntimeError(f"Rich draft tidak didukung di chat ini: {e}")

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
                    draft_markdown(acc, thinking_hint),
                    message_thread_id, can_stop, is_rtl,
                )
                last_sent = now
            except Exception:
                pass
    if acc:
        try:
            await send_draft(
                bot, chat_id, draft_id, draft_markdown(acc, thinking_hint),
                message_thread_id, can_stop, is_rtl,
            )
        except Exception:
            pass
    return acc


async def send_rich_message(
    bot: Bot,
    chat_id: int,
    rich_markdown: str,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    is_rtl: bool = False,
):
    rich_message: dict = {"markdown": rich_markdown}
    if is_rtl:
        rich_message["is_rtl"] = True
    payload: dict = {"chat_id": chat_id, "rich_message": rich_message}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    return await bot.do_api_request("sendRichMessage", payload)
