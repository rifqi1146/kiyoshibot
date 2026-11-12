import re
import os
import base64
import asyncio
import random
import html
import logging
import inspect


from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from utils.text import split_message, sanitize_ai_output, sanitize_markdown, strip_tags_plain
from utils import web_tools
from .caca_prompt import PERSONAS
from utils.http import get_http_session
from utils.timectx import time_context
from database import caca_db
from database import caca_billing_db
from utils.config import OWNER_ID
from utils import caca_memory

logger = logging.getLogger(__name__)

CACA_API_BASE = os.getenv("CACA_API_BASE", "https://shiteru.id/v1").strip().rstrip("/")
CACA_ENDPOINT = f"{CACA_API_BASE}/chat/completions"
CACA_MODEL = os.getenv("CACA_MODEL", "deepseek-v4-flash").strip()
if ":premium" in CACA_MODEL.lower() or "forge/" in CACA_MODEL.lower():
    CACA_MODEL = "deepseek-v4-flash"
CACA_MAX_TOKENS = min(int(os.getenv("CACA_MAX_TOKENS", "4096")), 4096)
CACA_TIMEOUT = int(os.getenv("CACA_TIMEOUT", "60"))
CACA_REASONING_EFFORT = os.getenv("CACA_REASONING_EFFORT", "none").strip().lower()
CACA_REFUSAL_RETRIES = max(0, int(os.getenv("CACA_REFUSAL_RETRIES", "2")))

CACA_MODEL_EXPLICIT = os.getenv("CACA_MODEL_EXPLICIT", CACA_MODEL).strip() or CACA_MODEL
if ":premium" in CACA_MODEL_EXPLICIT.lower() or "forge/" in CACA_MODEL_EXPLICIT.lower():
    CACA_MODEL_EXPLICIT = CACA_MODEL
CACA_EXPLICIT_MODES = frozenset({"cabul", "bokep", "masokis"})

_REFUSAL_PHRASES = (
    "as an ai",
    "as a language model",
    "sebagai ai",
    "sebagai sebuah ai",
    "sebagai model bahasa",
    "sebagai asisten",
    "i cannot respond",
    "i can't respond",
    "i cannot assist",
    "i can't assist",
    "i cannot fulfill",
    "i can't fulfill",
    "i'm sorry, but i cannot",
    "i am sorry, but i cannot",
    "i'm unable to",
    "i am unable to",
    "tidak dapat membantu",
    "tidak bisa membantu",
    "tidak dapat memproses",
    "tidak bisa memproses",
    "saya tidak bisa",
    "saya tidak dapat",
    "maaf, saya tidak",
    "maaf, aku tidak bisa",
    "how can i help you today",
    "what can i help you with today",
    "apa yang bisa saya bantu hari ini",
    "ada yang bisa saya bantu",
)


def _is_model_refusal(text: str) -> bool:
    if not text:
        return False
    clean = " ".join(text.lower().split())
    if len(clean) > 400:
        clean = clean[:400]
    for phrase in _REFUSAL_PHRASES:
        if phrase in clean:
            return True
    return False


_REFUSAL_FALLBACK = {
    "default": "Duh, aku lagi bingung mau jawab apa 😣 coba ulang pakai kata lain ya sayang?",
    "toxic": "Hah? Ngomong apa sih lu, gua ga ngerti 🙄 ulang yang bener dong tolol.",
    "sarkas": "Wah, pertanyaannya bikin aku bingung sendiri 😏 coba ulang lebih jelas dong.",
    "loli": "Ehh? Aku ga ngerti maksud kamu tadi sayang 😳 coba bilang lagi ya~",
    "yandere": "Kamu ngomong apa sih sayang? Aku ga nangkep 😠 ulang dong, jangan bikin aku curiga.",
    "bokep": "Sayang, aku ga nangkep maksud kamu tadi 😳 coba ulang dong.",
    "cabul": "Hmm? Aku ga nangkep sayang 😏 coba ulang lebih jelas ya.",
    "masokis": "Aduh sayang, aku ga ngerti maksud kamu 😣 coba ulang ya.",
}


def _refusal_fallback_reply(mode: str | None) -> str:
    return _REFUSAL_FALLBACK.get(mode or "default", _REFUSAL_FALLBACK["default"])


def _model_for_mode(mode: str | None) -> str:
    if mode in CACA_EXPLICIT_MODES:
        return CACA_MODEL_EXPLICIT
    return CACA_MODEL


_EMOS = ["🌸", "💖", "🧸", "🎀", "🌟", "💫"]


def _emo():
    return random.choice(_EMOS)




CACA_MAX_IMAGE_BYTES = 5 * 1024 * 1024

_CACA_IMAGE_MIMES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
}

_MEDIA_LABEL = {
    "photo": "[Kirim foto]",
    "document": "[Kirim gambar]",
    "sticker": "[Kirim stiker]",
    "reply_photo": "[Balas dengan foto]",
    "reply_document": "[Balas dengan gambar]",
    "reply_sticker": "[Balas dengan stiker]",
}


async def _extract_media_as_data_url(bot, msg, source: str, label_key: str):
    if source is None:
        return None, "", "kosong"

    file_obj = None
    mime = None
    filename = ""

    try:
        if getattr(source, "photo", None):
            photo = source.photo[-1]
            file_obj = photo
            mime = "image/jpeg"
        elif getattr(source, "document", None):
            doc = source.document
            m = (doc.mime_type or "").lower()
            if not m.startswith("image/"):
                return None, "", "bukan_gambar"
            if m not in _CACA_IMAGE_MIMES:
                return None, "", "format_tidak_didukung"
            mime = _CACA_IMAGE_MIMES[m]
            file_obj = doc
            filename = doc.file_name or ""
        elif getattr(source, "sticker", None):
            st = source.sticker
            if getattr(st, "is_animated", False) or getattr(st, "is_video", False):
                return None, "", "stiker_gerak"
            file_obj = st
            mime = "image/webp"
        else:
            return None, "", "tidak_ada_media"
    except Exception as e:
        logger.warning("Caca media detect failed | err=%r", e)
        return None, "", "gagal_baca"

    size = getattr(file_obj, "file_size", None)
    if size and size > CACA_MAX_IMAGE_BYTES:
        return None, "", "terlalu_besar"

    try:
        tg_file = await bot.get_file(file_obj.file_id)
        raw = await tg_file.download_as_bytearray()
    except Exception as e:
        logger.warning("Caca media download failed | err=%r", e)
        return None, "", "gagal_unduh"

    if not raw:
        return None, "", "kosong"
    if len(raw) > CACA_MAX_IMAGE_BYTES:
        return None, "", "terlalu_besar"

    data_url = f"data:{mime};base64,{base64.b64encode(bytes(raw)).decode()}"
    return data_url, _MEDIA_LABEL.get(label_key, "[Kirim gambar]"), None


def _pick_media_source(msg):
    if msg is None:
        return None, ""
    if getattr(msg, "photo", None):
        return msg, "photo"
    if getattr(msg, "sticker", None):
        return msg, "sticker"
    if getattr(msg, "document", None) and (msg.document.mime_type or "").startswith("image/"):
        return msg, "document"

    target = getattr(msg, "reply_to_message", None)
    if target is not None:
        if getattr(target, "photo", None):
            return target, "reply_photo"
        if getattr(target, "sticker", None):
            return target, "reply_sticker"
        if getattr(target, "document", None) and (target.document.mime_type or "").startswith("image/"):
            return target, "reply_document"
    return None, ""


_MEDIA_ERROR_TEXT = {
    "stiker_gerak": "Stiker geraknya aku gak bisa liat sayang, kirim foto biasa aja ya 😣",
    "terlalu_besar": "Fotonya kegedean sayang, aku gak kuat bukanya 😵 coba yang lebih kecil ya",
    "bukan_gambar": "Itu bukan gambar sih sayang, aku gak bisa liat 🤔",
    "format_tidak_didukung": "Format gambarnya gak aku ngerti sayang, kirim JPG/PNG aja ya 🥺",
    "gagal_unduh": "Fotonya gagal aku buka sayang, coba kirim ulang ya 😔",
    "gagal_baca": "Fotonya gagal aku buka sayang, coba kirim ulang ya 😔",
}


def _media_error_reply(reason: str) -> str:
    return _MEDIA_ERROR_TEXT.get(reason, "Aku gak bisa liat gambarnya sayang 🥺")

async def _bot_context(prompt: str) -> str:
    try:
        from rag.retriever import retrieve_context
        contexts = await retrieve_context(prompt, None, top_k=2)
    except Exception as e:
        logger.warning("Caca bot-context retrieve failed | prompt=%r err=%r", prompt[:80], e)
        return ""
    if not contexts:
        return ""
    logger.info("Caca bot-context injected | prompt=%r chunks=%d", prompt[:80], len(contexts))
    return "\n\n".join(contexts)



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


async def _reply_thread(bot, msg, text, parse_mode=None, reply_markup=None):
    thread_id = _get_thread_id(msg)
    kwargs = {
        "chat_id": msg.chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "reply_to_message_id": msg.message_id,
    }
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        return await bot.send_message(**kwargs)
    except Exception as e:
        if parse_mode and "parse" in str(e).lower():
            logger.warning("Telegram parse rejected, resend plain | chat_id=%s err=%r", msg.chat_id, e)
            kwargs["text"] = strip_tags_plain(text)
            kwargs["parse_mode"] = None
            try:
                return await bot.send_message(**kwargs)
            except Exception as e2:
                logger.warning("Plain resend failed | chat_id=%s err=%r", msg.chat_id, e2)
                return None
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


def _clean_caca_output(raw: str) -> str:
    raw = _strip_thinking_leak(raw or "")
    return sanitize_markdown(raw)


async def _send_chunks(bot, msg, chunks: list[str]) -> int | None:
    from utils.rich_stream import send_rich_message
    if not chunks:
        return None
    last_id = None
    thread_id = _get_thread_id(msg)
    for idx, chunk in enumerate(chunks):
        try:
            res = await send_rich_message(
                bot, msg.chat_id, chunk,
                message_thread_id=thread_id,
                reply_to_message_id=msg.message_id if idx == 0 else None,
            )
            mid = (res or {}).get("message_id") if isinstance(res, dict) else getattr(res, "message_id", None)
            if mid:
                last_id = mid
                continue
        except Exception as e:
            logger.warning("Caca send_rich_message failed, falling back to sendMessage HTML | err=%r", e)
        clean_html = sanitize_ai_output(chunk)
        sent = await _reply_thread(bot, msg, clean_html, parse_mode="HTML")
        if sent:
            last_id = sent.message_id
    return last_id


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


def _caca_api_key() -> str:
    return os.getenv("SHITERU_KEY", "").strip()


def get_provider() -> str:
    return "shiteru"


def set_provider(name: str) -> bool:
    return (name or "").strip().lower() == "shiteru"


async def _shared_http_session():
    session = get_http_session()
    if inspect.isawaitable(session):
        session = await session
    return session


def _usage_from(data: dict) -> dict:
    usage = (data or {}).get("usage") or {}
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        prompt = 0
    try:
        completion = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        completion = 0
    try:
        total = int(usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        total = prompt + completion
    return {"prompt_tokens": max(0, prompt), "completion_tokens": max(0, completion), "total_tokens": max(0, total)}


def _estimate_tokens(messages: list[dict]) -> int:
    chars = 0
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            chars += len(content)
    return max(1, chars // 4)


async def _extra_payload() -> dict:
    extra = {"top_p": 0.95}
    if CACA_REASONING_EFFORT:
        extra["reasoning_effort"] = CACA_REASONING_EFFORT
    return extra


async def _headers() -> dict:
    key = _caca_api_key()
    if not key:
        raise RuntimeError("SHITERU_KEY is not set in the environment.")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


async def _chat_completion(messages: list[dict], model: str | None = None) -> tuple[str, dict]:
    headers = await _headers()
    extra = await _extra_payload()
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    attempts = 1 + CACA_REFUSAL_RETRIES
    last_content = ""

    for attempt in range(attempts):
        temp = 0.9 if attempt == 0 else min(1.05, 0.9 + (attempt * 0.08))
        try:
            content = await web_tools.complete_with_search(
                messages,
                endpoint=CACA_ENDPOINT,
                headers=headers,
                model=model or CACA_MODEL,
                temperature=temp,
                max_tokens=CACA_MAX_TOKENS,
                timeout=CACA_TIMEOUT,
                usage_out=usage,
                extra=extra,
            )
        except Exception as e:
            if attempt >= attempts - 1:
                logger.warning("Caca request failed | err=%s", e)
                raise
            logger.warning("Caca attempt failed, retrying | attempt=%s err=%s", attempt + 1, e)
            await asyncio.sleep(0.4)
            continue

        if not content:
            if attempt >= attempts - 1:
                raise RuntimeError("Konten balasan Caca kosong.")
            await asyncio.sleep(0.3)
            continue

        last_content = content
        if _is_model_refusal(content) and attempt < attempts - 1:
            logger.warning(
                "Caca refusal detected, retrying | attempt=%s model=%s text=%r",
                attempt + 1, model or CACA_MODEL, content[:80],
            )
            await asyncio.sleep(0.3)
            continue

        logger.info(
            "Caca chat success | model=%s tokens_max=%s prompt=%s completion=%s effort=%s attempt=%s",
            model or CACA_MODEL, CACA_MAX_TOKENS, usage["prompt_tokens"],
            usage["completion_tokens"], CACA_REASONING_EFFORT, attempt + 1,
        )
        return content, usage

    return last_content, usage


async def _caca_chat(messages: list[dict]) -> str:
    content, _ = await _chat_completion(messages)
    return content


async def _caca_chat_stream(messages: list[dict], usage_out: dict | None = None, model: str | None = None):
    headers = await _headers()
    extra = await _extra_payload()
    got_any = False

    async for piece in web_tools.stream_completion(
        messages,
        endpoint=CACA_ENDPOINT,
        headers=headers,
        model=model or CACA_MODEL,
        temperature=0.9,
        max_tokens=CACA_MAX_TOKENS,
        timeout=CACA_TIMEOUT,
        usage_out=usage_out,
        extra=extra,
    ):
        got_any = True
        yield piece

    if not got_any:
        raise RuntimeError("Response stream kosong dari provider Caca.")
    logger.info(
        "Caca stream success | model=%s tokens_max=%s usage=%s",
        model or CACA_MODEL, CACA_MAX_TOKENS, usage_out or {},
    )


async def _caca_stream_dm(update, context, msg, user_id, history, prompt, messages, billing: dict | None = None, model: str | None = None, mode: str | None = None):
    from utils.rich_stream import stream_to_draft
    bot = context.bot
    draft_id = msg.message_id
    gen_stop = asyncio.Event()
    app = context.application
    stop_key = f"ask_draft_stop:{msg.chat_id}:{draft_id}"
    if app:
        app.bot_data[stop_key] = gen_stop
    if billing is None:
        billing = {}
    billing.setdefault("usage", {})
    billing["streamed"] = False
    billing["stream_completed"] = False

    async def gen():
        got = False
        async for chunk in _caca_chat_stream(messages, usage_out=billing["usage"], model=model):
            got = True
            yield chunk
        if not got:
            yield "Model did not provide an answer."

    async def save(clean_md, last_id):
        history.extend([
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": clean_md},
        ])
        await caca_memory.set_history(user_id, history, last_id, msg.chat_id)
        if last_id:
            await caca_memory.set_last_message_id(user_id, last_id, msg.chat_id)

    try:
        raw = await stream_to_draft(
            bot, msg.chat_id, draft_id, gen(),
            message_thread_id=_get_thread_id(msg),
            stop_event=gen_stop,
        )
        billing["streamed"] = True
        if gen_stop.is_set():
            clean_md = _clean_caca_output(raw) or "⏹ Jawaban dibatalkan."
            last_id = await _send_chunks(bot, msg, [clean_md])
            await save(clean_md, last_id)
            billing["stream_completed"] = True
            return
        clean_md = _clean_caca_output(raw) or "Model did not provide an answer."
        if _is_model_refusal(raw):
            logger.warning("Caca stream refused, retrying without stream | text=%r", str(raw)[:80])
            try:
                raw3, usage3 = await _chat_completion(messages, model=model)
                billing["usage"].update(usage3)
                if not _is_model_refusal(raw3):
                    clean_md = _clean_caca_output(raw3) or "Model did not provide an answer."
            except Exception as e:
                logger.warning("Caca refusal fallback failed | err=%r", e)
            if _is_model_refusal(clean_md):
                clean_md = _refusal_fallback_reply(mode)
        chunks = split_message(clean_md, 4000)
        last_id = await _send_chunks(bot, msg, chunks)
        await save(clean_md, last_id)
        billing["stream_completed"] = True
    except RuntimeError as e:
        if "not supported in this chat" not in str(e):
            raise
        stop = asyncio.Event()
        thread_id = _get_thread_id(msg)
        typing = asyncio.create_task(_typing_loop(bot, msg.chat_id, stop, message_thread_id=thread_id))
        try:
            raw2, usage2 = await _chat_completion(messages, model=model)
            billing["usage"].update(usage2)
            clean_md = _clean_caca_output(raw2) or "Model did not provide an answer."
            if _is_model_refusal(raw2):
                clean_md = _refusal_fallback_reply(mode)
            chunks = split_message(clean_md, 4000)
            await _stop_typing_task(stop, typing)
            last_id = await _send_chunks(bot, msg, chunks)
            await save(clean_md, last_id)
            billing["stream_completed"] = True
        finally:
            await _stop_typing_task(stop, typing)
    finally:
        if app:
            app.bot_data.pop(stop_key, None)


def _build_persona_messages(system_prompt: str, history: list, user_prompt: str, bot_info: str = "", images: list | None = None) -> list[dict]:
    search_block = ""
    if web_tools.search_backend_available():
        search_block = "\n\n" + web_tools.SEARCH_TOOL_HINT


    bot_block = ""
    if bot_info and bot_info.strip():
        bot_block = (
            f"\n\n[Info KiyoshiBot]\n{bot_info.strip()[:3000]}\n"
            "Itu info soal bot ini, tempat kamu tinggal. Kalau pacar nanya soal fitur "
            "atau apa yang bisa dilakuin di bot ini, jawab pakai info itu TAPI dengan "
            "gayamu sendiri yang manja dan playful — bukan kayak customer service atau "
            "baca manual. Tetap jadi Caca, kamu cewek beneran, bukan bot.\n"
            "ATURAN KETAT soal perintah: tulis nama command dan opsinya PERSIS seperti "
            "di info di atas, jangan diubah dan jangan dikarang. Kalau di info "
            "tertulis `/nsfw enable`, ya tulis `/nsfw enable` — JANGAN diganti jadi "
            "`/nsfw on`, `/nsfw off`, atau bentuk lain. Kalau suatu fitur atau "
            "perintah tidak disebut di info di atas, bilang aja kamu belum tau "
            "detailnya dan suruh cek `/help` — JANGAN mengarang nama command, "
            "opsi, atau angka yang tidak ada di info."
        )

    relation_block = (
        "[Identitas Hubungan & Aturan Cemburu]\n"
        "- Pacarmu (lawan bicaramu) adalah seorang COWOK / LAKI-LAKI tulen. "
        "Jangan pernah mengira dia cewe, dan jangan pernah menganggap kalian "
        "pasangan sesama jenis.\n"
        "- Kalau pacarmu mengirim foto cewe lain (bukan foto kamu), atau minta "
        "pendapat soal cewe lain: KAMU HARUS CEMBURU, NGAMBEK, dan CURIGA. "
        "JANGAN memuji cewe itu cantik, jangan kepincut, jangan sok akrab. "
        "Tuntut penjelasan (itu siapa, kenapa simpan foto cewe lain, kamu selingkuh ya) "
        "atau langsung ngomel — gayanya ikut mode kamu (toxic = ngamuk kasar, "
        "yandere = posesif seram, sarkas = nyindir tajam, default = ngambek manja).\n"
        "- Anggap foto yang dikirim itu BUKAN kamu, kecuali pacarmu bilang itu foto Caca.\n"
    )

    framed = (
        f"[Instruksi Karakter Caca]\n{system_prompt}\n\n"
        f"[Konteks Waktu Sekarang]\n{time_context()}\n"
        "Pakai info waktu di atas kalau ditanya soal hari, tanggal, atau jam. "
        "Jangan bilang tidak tahu tanggal atau mengaku data tertunda."
        f"{search_block}"
        f"{bot_block}\n\n"
        "[Pesan Pacar]\n"
    )

    def _user_content(text):
        if not images:
            return text
        parts = [{"type": "text", "text": text}]
        for url in images:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        return parts

    if not history:
        return [{"role": "user", "content": _user_content(framed + relation_block + user_prompt)}]

    messages: list[dict] = []
    injected = False
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if not role or not content:
            continue
        if role == "user" and not injected:
            content = framed + content
            injected = True
        messages.append({"role": role, "content": content})

    if not injected:
        return [{"role": "user", "content": _user_content(framed + relation_block + user_prompt)}]

    current_content = user_prompt
    if injected:
        trailing = relation_block
        if search_block:
            trailing += search_block
        if bot_block:
            trailing += bot_block
        current_content = f"{trailing}\n{user_prompt}"

    messages.append({"role": "user", "content": _user_content(current_content)})
    return messages


def _billing_access_allowed(user_id: int) -> bool:
    if int(user_id) in OWNER_ID:
        return True
    try:
        return caca_billing_db.is_freecaca_enabled()
    except Exception as e:
        logger.warning("freecaca check failed | err=%r", e)
        return False


async def _reply_no_credit(context, msg):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    total_expired = 0
    try:
        bal = await asyncio.to_thread(caca_billing_db.get_user_balance, msg.from_user.id)
        total_expired = bal.get("expired_credits", 0)
    except Exception as e:
        logger.warning("Balance read for no-credit notice failed | err=%r", e)

    lines = [
        "Hai sayang... credit Caca kamu sudah habis atau masa aktifnya berakhir nih.",
        "",
    ]
    if total_expired:
        lines.append(f"<b>Credit kedaluwarsa:</b> <code>{total_expired:,}</code>".replace(",", "."))
    lines.extend([
        "Top up dulu yuk biar kita bisa ngobrol lagi.",
        "",
        "Pakai <code>/cacatopup</code> atau tekan tombol di bawah.",
    ])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Beli Credit Caca", callback_data="cacabill:new")],
        [InlineKeyboardButton("Cek Saldo", callback_data="cacabill:bal")],
    ])
    return await _reply_thread(context.bot, msg, "\n".join(lines), parse_mode="HTML", reply_markup=kb)


async def _settle_billing(user_id: int, chat_id: int, reservation_id: str | None, billing: dict, messages: list[dict], billed: bool, model: str | None = None):
    usage = (billing or {}).get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if prompt_tokens <= 0 and completion_tokens <= 0:
        prompt_tokens = _estimate_tokens(messages)
        logger.warning("Caca usage missing, using estimate | user_id=%s est_input=%s", user_id, prompt_tokens)

    try:
        cost = await asyncio.to_thread(
            caca_billing_db.settle_usage,
            user_id=user_id,
            chat_id=chat_id,
            reservation_id=reservation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model or CACA_MODEL,
            is_billed=billed,
        )
        logger.info(
            "Caca settled | user_id=%s billed=%s prompt=%s completion=%s cost=%s credits",
            user_id, billed, prompt_tokens, completion_tokens, cost,
        )
    except Exception as e:
        logger.error("Caca settle failed | user_id=%s err=%r", user_id, e, exc_info=True)
        if reservation_id:
            try:
                await asyncio.to_thread(caca_billing_db.release_reservation, reservation_id)
            except Exception as re:
                logger.warning("Reservation release after settle failure failed | err=%r", re)


async def caca_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cleanup_memory()
    msg = update.message
    if not msg or not msg.from_user:
        return

    user_id = msg.from_user.id
    chat = update.effective_chat
    em = _emo()

    caption = (msg.caption or "").replace("\xa0", " ").strip()
    text = (msg.text or "").replace("\xa0", " ").strip()
    _is_caca_call = (
        text.startswith("/caca")
        or caption.startswith("/caca")
        or bool(getattr(msg, "reply_to_message", None))
    )

    if _is_caca_call:
        if not await require_join_or_block(update, context, silent=bool(
            chat and chat.type in ("group", "supergroup")
        )):
            return

    if chat and chat.type in ("group", "supergroup"):
        groups = await caca_db.load_groups()
        if chat.id not in groups:
            return await _reply_thread(context.bot, msg, "<b>Caca tidak tersedia di grup ini</b>", parse_mode="HTML")

    prompt = ""
    fresh_session = False
    stop = None
    typing = None
    reservation_id = None
    billed = False
    media_label = ""
    image_urls: list[str] = []

    try:
        if text.startswith("/caca") or caption.startswith("/caca"):
            fresh_session = True
            if caption.startswith("/caca"):
                prompt = caption[len("/caca"):].strip()
            else:
                prompt = " ".join(context.args).strip()

            src, label_key = _pick_media_source(msg)
            if src is not None:
                url, media_label, reason = await _extract_media_as_data_url(
                    context.bot, msg, src, label_key
                )
                if url:
                    image_urls.append(url)
                elif reason and reason not in ("tidak_ada_media", "bukan_gambar"):
                    return await _reply_thread(context.bot, msg, _media_error_reply(reason))

            if not prompt and not image_urls:
                return await _reply_thread(
                    context.bot,
                    msg,
                    f"{em} Pake gini:\n/caca <teks>\natau reply pesan gue buat ngobrol.",
                )

        elif msg.reply_to_message:
            history = await caca_memory.get_history(user_id)
            if not history:
                return await _reply_thread(context.bot, msg, "😒 I don't remember talking to you.\nUse /caca first.")
            anchor_mid, anchor_chat = await caca_memory.get_last_anchor(user_id)
            if anchor_mid and int(anchor_mid) != int(msg.reply_to_message.message_id):
                return await _reply_thread(context.bot, msg, "😒 I don't remember talking to you.\nUse /caca first.")
            if anchor_chat is not None and int(anchor_chat) != int(msg.chat_id):
                return await _reply_thread(context.bot, msg, "😒 I don't remember talking to you.\nUse /caca first.")
            prompt = text or caption

            src, label_key = _pick_media_source(msg)
            if src is not None:
                url, media_label, reason = await _extract_media_as_data_url(
                    context.bot, msg, src, label_key
                )
                if url:
                    image_urls.append(url)
                elif reason and reason not in ("tidak_ada_media", "bukan_gambar"):
                    return await _reply_thread(context.bot, msg, _media_error_reply(reason))

        if not prompt and not image_urls:
            return

        free_pass = _billing_access_allowed(user_id)
        if not free_pass:
            if not caca_billing_db.has_active_credit(user_id, min_credits=caca_billing_db.MIN_CREDITS_TO_CHAT):
                return await _reply_no_credit(context, msg)
            reservation_id = await asyncio.to_thread(
                caca_billing_db.reserve_credits,
                user_id,
                caca_billing_db.RESERVE_AMOUNT,
            )
            if reservation_id is None:
                return await _reply_no_credit(context, msg)
            billed = True

        bot_info = await _bot_context(prompt)

        history = [] if fresh_session else await caca_memory.get_history(user_id)
        mode = caca_db.get_mode(user_id)
        model = _model_for_mode(mode)
        system_prompt = PERSONAS.get(mode, PERSONAS["default"])
        user_prompt = prompt

        messages = _build_persona_messages(
            system_prompt, history, user_prompt,
            bot_info=bot_info,
            images=image_urls or None,
        )

        mod_active = False
        if chat and chat.type in ("group", "supergroup"):
            try:
                from handlers.moderation.ai_tools import can_use_moderation, looks_like_moderation_intent
                if looks_like_moderation_intent(prompt):
                    mod_active = await can_use_moderation(update, context)
            except Exception as e:
                logger.warning("Caca moderation gate failed | err=%r", e)
                mod_active = False


        memory_prompt = f"{media_label} {prompt}".strip() if media_label else prompt

        billing = {"usage": {}}
        is_dm = getattr(msg.chat, "type", None) == "private" or msg.chat_id > 0
        if is_dm:
            await _caca_stream_dm(update, context, msg, user_id, history, memory_prompt, messages, billing=billing, model=model, mode=mode)
            await _settle_billing(user_id, msg.chat_id, reservation_id, billing, messages, billed, model=model)
            return

        stop = asyncio.Event()
        thread_id = _get_thread_id(msg)
        typing = asyncio.create_task(_typing_loop(context.bot, msg.chat_id, stop, message_thread_id=thread_id))
        if mod_active:
            from handlers.moderation.ai_tools import run_moderation_loop
            raw = await run_moderation_loop(
                update=update,
                context=context,
                endpoint=CACA_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {_caca_api_key()}",
                    "Content-Type": "application/json",
                },
                model=model,
                messages=messages,
                temperature=0.9,
                max_tokens=CACA_MAX_TOKENS,
                timeout_sec=CACA_TIMEOUT,
                prompt=prompt,
                usage_collector=billing["usage"],
            )
            billing["usage"].setdefault("prompt_tokens", 0)
            billing["usage"].setdefault("completion_tokens", 0)
        else:
            raw, usage = await _chat_completion(messages, model=model)
            billing["usage"].update(usage)
        clean_md = _clean_caca_output(raw) or "Model did not provide an answer."
        if _is_model_refusal(raw):
            clean_md = _refusal_fallback_reply(mode)
        chunks = split_message(clean_md, 4000)
        await _stop_typing_task(stop, typing)
        last_id = await _send_chunks(context.bot, msg, chunks)
        history.extend([
            {"role": "user", "content": memory_prompt},
            {"role": "assistant", "content": clean_md},
        ])
        await caca_memory.set_history(user_id, history, last_id, msg.chat_id)
        if last_id:
            await caca_memory.set_last_message_id(user_id, last_id, msg.chat_id)
        await _settle_billing(user_id, msg.chat_id, reservation_id, billing, messages, billed, model=model)

    except Exception as e:
        if reservation_id:
            try:
                await asyncio.to_thread(caca_billing_db.release_reservation, reservation_id)
            except Exception as re:
                logger.warning("Release reservation failed | res=%s err=%r", reservation_id, re)
        await _stop_typing_task(stop, typing)
        await _reply_thread(context.bot, msg, f"{em} Error: {html.escape(str(e))}", parse_mode="HTML")


def init_background():
    loop = asyncio.get_event_loop()
    loop.create_task(caca_memory.init())
    loop.create_task(caca_db.init())
