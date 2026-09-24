import re
import os
import asyncio
import random
import html
import logging
import inspect
import aiohttp

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from handlers.gsearch import google_search
from utils.text import split_message, sanitize_ai_output
from .caca_prompt import PERSONAS
from utils.http import get_http_session
from database import caca_db
from utils import caca_memory

logger = logging.getLogger(__name__)

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("CACA_GROQ_MODEL", "qwen/qwen3.6-27b").strip()
GROQ_TIMEOUT = int(os.getenv("GROQ_TIMEOUT", "60"))
_EMOS = ["🌸", "💖", "🧸", "🎀", "🌟", "💫"]


def _emo():
    return random.choice(_EMOS)


def _cleanup_memory():
    try:
        asyncio.get_event_loop().create_task(caca_memory.cleanup())
    except Exception as e:
        logger.warning("Failed to schedule Caca memory cleanup | err=%r", e)


def _get_thread_id(msg):
    if not getattr(msg.chat, "is_forum", False):
        return None
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id:
        return thread_id
    reply = getattr(msg, "reply_to_message", None)
    if reply:
        return getattr(reply, "message_thread_id", None)
    return None


async def _typing_loop(bot, chat_id, stop_event: asyncio.Event, message_thread_id=None):
    try:
        kwargs = {
            "chat_id": chat_id,
            "action": ChatAction.TYPING,
        }
        if message_thread_id:
            kwargs["message_thread_id"] = message_thread_id
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(**kwargs)
            except Exception as api_err:
                logger.warning("Typing action gagal, hapus thread_id. Error: %s", api_err)
                if "message_thread_id" in kwargs:
                    kwargs.pop("message_thread_id", None)
                    continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        logger.debug("Caca typing task cancelled")
    except Exception as e:
        logger.warning("Caca typing loop stopped | err=%r", e)


async def _stop_typing_task(stop, typing):
    if stop:
        stop.set()
    if typing:
        typing.cancel()
        try:
            await typing
        except asyncio.CancelledError:
            logger.debug("Caca typing task stopped")
        except Exception as e:
            logger.warning("Caca typing task stop failed | err=%r", e)


async def _reply_thread(bot, msg, text, parse_mode=None):
    thread_id = _get_thread_id(msg)
    kwargs = {
        "chat_id": msg.chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "reply_to_message_id": msg.message_id,
    }
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        return await bot.send_message(**kwargs)
    except Exception as e:
        logger.warning(
            "Caca reply failed, retry without reply target | chat_id=%s thread_id=%s err=%r",
            msg.chat_id,
            thread_id,
            e,
        )
        kwargs.pop("reply_to_message_id", None)
        try:
            return await bot.send_message(**kwargs)
        except Exception as e2:
            logger.warning(
                "Caca reply retry failed, retry without thread | chat_id=%s thread_id=%s err=%r",
                msg.chat_id,
                thread_id,
                e2,
            )
            kwargs.pop("message_thread_id", None)
            return await bot.send_message(**kwargs)


def _normalize_caca_output(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<[^>\n]+>", " ", text)
    text = re.sub(r"\b/?(?:b|i|u|s|code|pre|strong|em|tg-spoiler)\b", " ", text, flags=re.I)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"\s+", " ", line)
        if lines and len(lines[-1]) <= 35 and not re.search(r"[.!?:]$", lines[-1]):
            lines[-1] += f" {line}"
        else:
            lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _strip_thinking_leak(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"(?is)<think>.*?</think>", "", text).strip()
    text = re.sub(r"(?is)<thinking>.*?</thinking>", "", text).strip()
    leak = any(
        k in text.lower()
        for k in (
            "wait, looking at",
            "looking at the context",
            "let me ",
            "i should ",
            "the user wants",
            "as caca",
            "actually,",
            "or more",
            "this feels right",
            "let me revise",
        )
    )
    if leak:
        quoted = re.findall(r"[\"“]([^\"”]{8,2500})[\"”]", text, flags=re.S)
        if quoted:
            return quoted[-1].strip()
        lines = []
        for line in text.splitlines():
            low = line.lower().strip()
            if not low:
                continue
            if low.startswith(
                ("wait,", "actually,", "let me", "the user", "i should", "looking at", "or more", "this feels")
            ):
                continue
            if "as caca" in low or "the rules say" in low:
                continue
            lines.append(line)
        text = "\n".join(lines).strip()
    return text


def _groq_api_keys() -> list[str]:
    keys = []
    seen = set()

    raw_primary = os.getenv("GROQ_API_KEY", "")
    for k in raw_primary.split(","):
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    for i in range(1, 15):
        k = os.getenv(f"GROQ_API_KEY_{i}", "").strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    return keys


async def _shared_http_session():
    session = get_http_session()
    if inspect.isawaitable(session):
        session = await session
    return session


async def _groq_chat(messages: list[dict]) -> str:
    keys = _groq_api_keys()
    if not keys:
        raise RuntimeError("GROQ_API_KEY belum disetel di environment.")

    session = await _shared_http_session()
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.9,
        "max_completion_tokens": 1024,
        "top_p": 0.95,
        "reasoning_effort": "none",
        "stream": False,
    }

    errors = []
    for idx, key in enumerate(keys, start=1):
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        try:
            async with session.post(
                GROQ_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=GROQ_TIMEOUT),
            ) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    err_msg = ""
                    if isinstance(data, dict):
                        err_msg = (data.get("error") or {}).get("message") or str(data)
                    raise RuntimeError(f"HTTP {r.status}: {err_msg or 'Request error'}")

                choices = data.get("choices") or []
                if not choices:
                    raise RuntimeError("Response choices kosong dari Groq.")

                content = (choices[0].get("message") or {}).get("content") or ""
                if not content:
                    raise RuntimeError("Konten balasan Groq kosong.")

                logger.info("Groq success | key_index=%s model=%s", idx, GROQ_MODEL)
                return content
        except Exception as e:
            err = str(e)
            errors.append(f"key#{idx}: {err}")
            logger.warning("Groq request failed | key_index=%s err=%s", idx, err)
            continue

    raise RuntimeError("Semua API key Groq gagal: " + " | ".join(errors[-3:]))


async def meta_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    _cleanup_memory()
    msg = update.message
    if not msg or not msg.from_user:
        return

    user_id = msg.from_user.id
    chat = update.effective_chat
    em = _emo()

    if chat and chat.type in ("group", "supergroup"):
        groups = await caca_db.load_groups()
        if chat.id not in groups:
            return await _reply_thread(context.bot, msg, "<b>Caca tidak tersedia di grup ini</b>", parse_mode="HTML")

    prompt = ""
    use_search = False
    fresh_session = False
    stop = None
    typing = None

    try:
        if msg.text and msg.text.startswith("/caca"):
            if context.args and context.args[0].lower() == "search":
                use_search = True
                prompt = " ".join(context.args[1:]).strip()
            else:
                fresh_session = True
                prompt = " ".join(context.args).strip()

            if not prompt:
                return await _reply_thread(
                    context.bot,
                    msg,
                    f"{em} Pake gini:\n/caca <teks>\n/caca search <teks>\natau reply pesan gue buat ngobrol.",
                )
        elif msg.reply_to_message:
            history = await caca_memory.get_history(user_id)
            if not history:
                return await _reply_thread(context.bot, msg, "😒 Gue ga inget ngobrol sama lu.\nKetik /caca dulu.")
            prompt = (msg.text or "").strip()

        if not prompt:
            return

        logger.debug(
            "Caca typing start | chat_id=%s message_id=%s has_reply=%s",
            msg.chat_id,
            msg.message_id,
            bool(msg.reply_to_message),
        )
        stop = asyncio.Event()
        thread_id = _get_thread_id(msg)
        typing = asyncio.create_task(_typing_loop(context.bot, msg.chat_id, stop, message_thread_id=thread_id))

        search_context = ""
        if use_search:
            try:
                ok, results = await google_search(prompt, limit=5)
                if ok and results:
                    lines = [f"- {r['title']}\n  {r['snippet']}\n  Sumber: {r['link']}" for r in results]
                    search_context = (
                        "Ini hasil search, pake buat nambah konteks, anggap ini adalah sumber terbaru. "
                        "Jawab tetap sebagai Caca.\n\n" + "\n\n".join(lines)
                    )
                elif not ok:
                    logger.warning("Google Search failed | query=%r err=%r", prompt, results)
            except Exception as e:
                logger.error("Google Search unexpected error | query=%r err=%r", prompt, e, exc_info=True)

        history = [] if fresh_session else await caca_memory.get_history(user_id)
        mode = caca_db.get_mode(user_id)
        system_prompt = PERSONAS.get(mode, PERSONAS["default"])
        user_prompt = f"{search_context}\n\n{prompt}" if search_context else prompt

        messages = (
            [{"role": "system", "content": system_prompt}]
            + history
            + [{"role": "user", "content": user_prompt}]
        )

        raw = await _groq_chat(messages)
        cleaned = _normalize_caca_output(sanitize_ai_output(_strip_thinking_leak(raw)))

        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": cleaned},
        ]
        await caca_memory.set_history(user_id, history)
        await _stop_typing_task(stop, typing)

        chunks = split_message(cleaned, 4000)
        sent = None
        for chunk in chunks:
            sent = await _reply_thread(context.bot, msg, chunk, parse_mode="HTML")
        if sent:
            await caca_memory.set_last_message_id(user_id, sent.message_id)

    except Exception as e:
        await _stop_typing_task(stop, typing)
        await _reply_thread(context.bot, msg, f"{em} Error: {html.escape(str(e))}", parse_mode="HTML")


def init_background():
    loop = asyncio.get_event_loop()
    loop.create_task(caca_memory.init())
    loop.create_task(caca_db.init())
