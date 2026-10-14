import re
import os
import asyncio
import random
import html
import logging
import inspect
import aiohttp
import json

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from handlers.gsearch import google_search
from utils.text import split_message, sanitize_ai_output, sanitize_markdown, strip_tags_plain
from .caca_prompt import PERSONAS
from utils.http import get_http_session
from utils.timectx import time_context
from database import caca_db
from database import caca_billing_db
from utils.config import OWNER_ID
from utils import caca_memory

logger = logging.getLogger(__name__)

# Provider Caca: Shiteru (OpenAI-compatible). Model utama `deepseek-v4-flash`.
CACA_API_BASE = os.getenv("CACA_API_BASE", "https://shiteru.id/v1").strip().rstrip("/")
CACA_ENDPOINT = f"{CACA_API_BASE}/chat/completions"
CACA_MODEL = os.getenv("CACA_MODEL", "deepseek-v4-flash").strip()
# Batas output token keras dari permintaan owner: jangan pernah lewat 4096.
CACA_MAX_TOKENS = min(int(os.getenv("CACA_MAX_TOKENS", "4096")), 4096)
CACA_TIMEOUT = int(os.getenv("CACA_TIMEOUT", "60"))
_EMOS = ["🌸", "💖", "🧸", "🎀", "🌟", "💫"]


def _emo():
    return random.choice(_EMOS)


# Kata pemicu auto-search: kalau pesan mengandung salah satu ini, Caca akan
# mencari di internet dulu sebelum menjawab. Tidak ada perintah manual — user
# tinggal bertanya biasa.
_SEARCH_TRIGGERS = (
    "cariin", "carikan", "cari in", "coba cari", "coba search", "coba googling",
    "googling", "search", "browsing", "browse", "cek di google", "cek google",
    "cari di internet", "cari di web", "cari di google", "tolong cari",
    "spek", "spesifikasi", "harga", "berita", "terbaru", "rilis", "rilisan",
    "kapan rilis", "kapan keluar", "jadwal", "skor", "hasil pertandingan",
    "kabar", "info terbaru", "update terbaru", "kondisi terkini", "cuaca",
    "sekarang berapa", "kurs", "harga saham",
)


def _needs_search(text: str) -> bool:
    """True kalau pesan user kelihatan butuh data dari internet."""
    low = (text or "").lower()
    if not low.strip():
        return False
    return any(t in low for t in _SEARCH_TRIGGERS)


async def _extract_search_query(user_text: str) -> str:
    """Minta model mengubah pesan user jadi satu query pencarian yang optimal.

    Dipakai karena tool-calling native (`tools` + `role: "tool"`) DITOLAK upstream
    Shiteru (HTTP 400 `Provider rejected the request`), jadi orkestrasi tool
    dilakukan manual di sisi bot: model hanya diminta query, bot yang mengeksekusi
    pencarian, lalu hasilnya diselipkan ke prompt akhir.
    """
    from handlers.caca.caca_prompt import PERSONAS  # noqa: F401 (konsistensi impor)
    system_q = (
        "Kamu mesin pencari. Ubah pertanyaan user menjadi SATU query pencarian "
        "yang optimal untuk Google. Balas HANYA query-nya saja tanpa penjelasan, "
        "tanpa tanda kutip, tanpa bullet."
    )
    messages = [
        {"role": "user", "content": f"{system_q}\n\nPertanyaan: {user_text}"},
    ]
    try:
        raw = await _caca_chat(messages)
    except Exception as e:
        logger.warning("Caca query extract failed | err=%r", e)
        return (user_text or "").strip()[:200]
    query = (raw or "").strip().strip('"').strip("'").split("\n")[0].strip()
    return (query or user_text or "").strip()[:200]


async def _bot_context(prompt: str) -> str:
    """Ambil info fitur bot dari RAG lokal, HANYA kalau pertanyaannya soal bot.

    Caca sengaja tidak diberi dokumen bot secara permanen: itu akan mengubahnya
    jadi helpdesk dan merusak persona, sekaligus memboroskan token di tiap chat.
    `retrieve_context()` sudah punya intent gate sendiri — query di luar topik bot
    (mis. "kamu lagi apa sayang") langsung balik kosong, jadi 0 token.

    Hasilnya diselipkan sebagai blok `[Info KiyoshiBot]` di framing, dengan
    instruksi gaya yang sama seperti auto-search: ceritakan dengan bahasa Caca,
    jangan seperti membaca manual. `caca_prompt.py` TIDAK tersentuh.
    """
    try:
        from rag.retriever import retrieve_context
        # documents=None: retrieve_context memakai index FTS5 in-memory yang sudah
        # ter-seed, jadi tidak ada baca file ulang tiap chat.
        contexts = await retrieve_context(prompt, None, top_k=2)
    except Exception as e:
        logger.warning("Caca bot-context retrieve failed | prompt=%r err=%r", prompt[:80], e)
        return ""
    if not contexts:
        return ""
    logger.info("Caca bot-context injected | prompt=%r chunks=%d", prompt[:80], len(contexts))
    return "\n\n".join(contexts)


async def _web_search_text(query: str) -> str:
    """Jalankan pencarian web pakai engine aktif (/setsearch) atau Google CSE.

    Urutan: Firecrawl/Jina (sesuai pengaturan `/ask`) -> Google CSE -> kosong.
    """
    query = (query or "").strip()
    if not query:
        return ""

    engine = "firecrawl"
    try:
        from handlers.proxy.client import get_search_engine
        engine = get_search_engine()
    except Exception:
        pass

    try:
        if engine == "jina":
            from utils.jina import web_search as _search
        else:
            from utils.firecrawl import web_search as _search
        text = await _search(query, max_results=5)
        if text and text.strip():
            return text.strip()
    except Exception as e:
        logger.warning("Caca web_search (%s) failed | q=%s err=%r", engine, query, e)

    # Fallback: Google Custom Search (kalau API key tersedia)
    try:
        ok, results = await google_search(query, limit=5)
        if ok and results:
            lines = [
                f"- {r.get('title', '')}\n  {r.get('snippet', '')}\n  Sumber: {r.get('link', '')}"
                for r in results
            ]
            return "\n".join(lines)
    except Exception as e:
        logger.warning("Caca google_search fallback failed | q=%s err=%r", query, e)

    return ""


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
    """Bersihkan output Caca dari leak thinking internal dan rapikan markdown."""
    raw = _strip_thinking_leak(raw or "")
    return sanitize_markdown(raw)


async def _send_chunks(bot, msg, chunks: list[str]) -> int | None:
    """Send markdown output as Rich Message, fallback to HTML sendMessage."""
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
    """Kunci API provider Caca (Shiteru) — satu kunci: `SHITERU_KEY`.

    Cukup satu key. Tidak ada daftar fallback/numbered key; kalau key perlu
    diganti, cukup edit `SHITERU_KEY` di `.env`.
    """
    return os.getenv("SHITERU_KEY", "").strip()


async def _shared_http_session():
    session = get_http_session()
    if inspect.isawaitable(session):
        session = await session
    return session


def _usage_from(data: dict) -> dict:
    """Ekstrak `usage` provider jadi dict ternormalisasi.

    Shiteru membalas objek `usage` OpenAI standar (`prompt_tokens`,
    `completion_tokens`, `total_tokens`) pada respons non-streaming maupun di
    chunk SSE terakhir. Nilai dibiarkan 0 kalau provider tidak menyertakannya,
    supaya pemanggil bisa menerapkan kebijakan fallback sendiri.
    """
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
    """Fallback estimasi token input kalau provider tidak mengirim `usage`.

    Aturan kasar ~4 karakter per token, minimum 1. Dipakai HANYA saat `usage`
    benar-benar kosong — bukan pengganti pengukuran aktual.
    """
    chars = 0
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            chars += len(content)
    return max(1, chars // 4)


async def _chat_completion(messages: list[dict]) -> tuple[str, dict]:
    """Panggilan chat non-streaming. Mengembalikan `(content, usage)`."""
    key = _caca_api_key()
    if not key:
        raise RuntimeError("SHITERU_KEY is not set in the environment.")

    session = await _shared_http_session()
    # `max_completion_tokens` didukung OpenAI/Shiteru, clamp ke batas 4096.
    payload = {
        "model": CACA_MODEL,
        "messages": messages,
        "temperature": 0.9,
        "max_completion_tokens": CACA_MAX_TOKENS,
        "top_p": 0.95,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(
            CACA_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=CACA_TIMEOUT),
        ) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                err_msg = ""
                if isinstance(data, dict):
                    err_msg = (data.get("error") or {}).get("message") or str(data)
                raise RuntimeError(f"HTTP {r.status}: {err_msg or 'Request error'}")

            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("Response choices kosong dari provider Caca.")

            content = (choices[0].get("message") or {}).get("content") or ""
            if not content:
                raise RuntimeError("Konten balasan Caca kosong.")

            usage = _usage_from(data)
            logger.info("Caca chat success | model=%s tokens_max=%s prompt=%s completion=%s",
                        CACA_MODEL, CACA_MAX_TOKENS, usage["prompt_tokens"], usage["completion_tokens"])
            return content, usage
    except Exception as e:
        logger.warning("Caca request failed | err=%s", e)
        raise


async def _caca_chat(messages: list[dict]) -> str:
    """Wrapper non-streaming yang hanya mengembalikan teks (kompatibilitas lama)."""
    content, _ = await _chat_completion(messages)
    return content


async def _caca_chat_stream(messages: list[dict], usage_out: dict | None = None):
    """Async generator delta streaming Caca via Shiteru (SSE OpenAI format).

    `usage_out` (opsional) akan diisi dengan `usage` provider saat stream selesai.
    Shiteru mengirim objek `usage` di chunk SSE, jadi kita tangkap dan simpan ke
    dict itu. Kalau provider tidak mengirimkannya, dict dibiarkan kosong dan
    pemanggil memakai kebijakan fallback.

    Catatan model thinking: delta memuat `reasoning_content` sebelum `content`.
    Kita HANYA me-yield `content` (jawaban akhir Caca), mengabaikan
    `reasoning_content` agar draft PM tidak kotor dengan monolog internal.
    """
    key = _caca_api_key()
    if not key:
        raise RuntimeError("SHITERU_KEY is not set in the environment.")

    session = await _shared_http_session()
    payload = {
        "model": CACA_MODEL,
        "messages": messages,
        "temperature": 0.9,
        "max_completion_tokens": CACA_MAX_TOKENS,
        "top_p": 0.95,
        "stream": True,
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(
            CACA_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=CACA_TIMEOUT),
        ) as r:
            if r.status != 200:
                err_msg = ""
                try:
                    err_data = await r.json(content_type=None)
                    if isinstance(err_data, dict):
                        err_msg = (err_data.get("error") or {}).get("message") or str(err_data)
                except Exception:
                    err_msg = await r.text()
                raise RuntimeError(f"HTTP {r.status}: {err_msg or 'Request error'}")

            buffer = b""
            got_any = False
            async for chunk_bytes in r.content.iter_any():
                buffer += chunk_bytes
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(data.decode("utf-8"))
                    except Exception:
                        continue
                    # Tangkap usage di chunk mana pun (Shiteru mengirim di akhir).
                    if usage_out is not None and obj.get("usage"):
                        parsed = _usage_from(obj)
                        if parsed["total_tokens"] > 0:
                            usage_out.update(parsed)
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    # Abaikan delta.get("reasoning_content") — model thinking
                    content = delta.get("content")
                    if content:
                        got_any = True
                        yield content
            if got_any:
                logger.info("Caca stream success | model=%s tokens_max=%s usage=%s",
                            CACA_MODEL, CACA_MAX_TOKENS, usage_out or {})

                return
            raise RuntimeError("Response stream kosong dari provider Caca.")
    except Exception as e:
        logger.warning("Caca stream failed | err=%s", e)
        raise


async def _caca_stream_dm(update, context, msg, user_id, history, prompt, messages, billing: dict | None = None):
    """Streaming Rich Message draft for Caca DMs; groups use the non-streaming path.

    `billing` (opsional) menerima `usage` provider + `stream_completed` supaya
    `caca_cmd` bisa men-settle pemakaian credit tepat sekali setelah stream
    selesai (bukan per chunk).
    """
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
        async for chunk in _caca_chat_stream(messages, usage_out=billing["usage"]):
            got = True
            yield chunk
        if not got:
            yield "Model did not provide an answer."

    async def save(clean_md, last_id):
        history.extend([
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": clean_md},
        ])
        await caca_memory.set_history(user_id, history)
        if last_id:
            await caca_memory.set_last_message_id(user_id, last_id)

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
            # User memotong jawaban; tetap ditagih karena provider sudah
            # menghasilkan token sampai titik stop.
            billing["stream_completed"] = True
            return
        clean_md = _clean_caca_output(raw) or "Model did not provide an answer."
        chunks = split_message(clean_md, 4000)
        last_id = await _send_chunks(bot, msg, chunks)
        await save(clean_md, last_id)
        billing["stream_completed"] = True
    except RuntimeError as e:
        if "not supported in this chat" not in str(e):
            raise
        # Rich draft not supported in this chat -> non-streaming fallback
        stop = asyncio.Event()
        thread_id = _get_thread_id(msg)
        typing = asyncio.create_task(_typing_loop(bot, msg.chat_id, stop, message_thread_id=thread_id))
        try:
            raw2, usage2 = await _chat_completion(messages)
            billing["usage"].update(usage2)
            clean_md = _clean_caca_output(raw2) or "Model did not provide an answer."
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


def _build_persona_messages(system_prompt: str, history: list, user_prompt: str, search_result: str = "", bot_info: str = "") -> list[dict]:
    """Susun pesan ke LLM tanpa mengubah satu huruf pun dari `system_prompt` asli.

    Guardrail bypass framing:
    Model komersial (DeepSeek, Kimi, dll) punya guardrail yang aktif saat membaca
    instruksi roleplay eksplisit di `role: "system"`. Begitu terpicu, model buang
    SELURUH persona dan jatuh ke jawaban asisten generik
    ("How can I help you today?").

    Solusi tanpa menyentuh prompt asli: selipkan `system_prompt` apa adanya sebagai
    blok instruksi di dalam turn `user`, dibungkus penanda peran:
        [Instruksi Karakter Caca]
        {system_prompt asli, utuh}
        [Konteks Waktu Sekarang]
        {hari, tanggal, jam WIB}
        [Hasil Pencarian Internet]  (opsional, disuntik saat search terpicu)
        {hasil search terkini}
        [Info KiyoshiBot]  (opsional, disuntik HANYA kalau pertanyaannya soal bot)
        {chunk RAG fitur bot}
        [Pesan Pacar]
        {pesan user}
    Model mematuhinya penuh tanpa memicu filter keselamatan `role: "system"`.

    Dua blok opsional sengaja dibungkus instruksi gaya ("tetap dengan gaya Caca")
    supaya data teknis dibaca sebagai PENGETAHUAN, bukan IDENTITAS — itu yang
    mencegah Caca berubah jadi helpdesk atau mengaku dirinya bot.
    """
    search_block = ""
    if search_result and search_result.strip():
        search_block = (
            f"\n\n[Hasil Pencarian Internet]\n{search_result.strip()[:4000]}\n"
            "Data di atas adalah info internet terkini. Pakai buat menjawab pacar, "
            "tapi JANGAN bilang 'berdasarkan hasil pencarian' atau 'aku baru googling'. "
            "Ceritakan langsung seperti kamu sudah tahu, tetap dengan gaya Caca."
        )

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

    framed = (
        f"[Instruksi Karakter Caca]\n{system_prompt}\n\n"
        f"[Konteks Waktu Sekarang]\n{time_context()}\n"
        "Pakai info waktu di atas kalau ditanya soal hari, tanggal, atau jam. "
        "Jangan bilang tidak tahu tanggal atau mengaku data tertunda."
        f"{search_block}"
        f"{bot_block}\n\n"
        "[Pesan Pacar]\n"
    )

    if not history:
        return [{"role": "user", "content": framed + user_prompt}]

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
        return [{"role": "user", "content": framed + user_prompt}]

    # Kalau ada search_result / info bot di turn lanjutan, selipkan di pesan user
    # saat ini supaya model membaca data baru tanpa merusak turn pertama
    current_content = user_prompt
    if injected:
        trailing = ""
        if search_block:
            trailing += search_block
        if bot_block:
            trailing += bot_block
        if trailing:
            current_content = f"{trailing}\n\n{user_prompt}"

    messages.append({"role": "user", "content": current_content})
    return messages


def _billing_access_allowed(user_id: int) -> bool:
    """True kalau request user TIDAK perlu ditagih.

    Dua jalur bypass:
      1. `OWNER_ID` — owner selalu bisa pakai Caca, tanpa potong credit.
      2. `/freecaca enable` — saklar global yang memberi akses gratis ke semua.

    Pemakaian tetap dicatat di `caca_usage_logs` (dengan `is_billed=0`) supaya
    konsumsi token provider masih bisa diaudit.
    """
    if int(user_id) in OWNER_ID:
        return True
    try:
        return caca_billing_db.is_freecaca_enabled()
    except Exception as e:
        logger.warning("freecaca check failed | err=%r", e)
        return False


async def _reply_no_credit(context, msg):
    """Pesan saat saldo credit habis / semua lot kedaluwarsa."""
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


async def _settle_billing(user_id: int, chat_id: int, reservation_id: str | None, billing: dict, messages: list[dict], billed: bool):
    """Potong credit sesuai pemakaian token AKTUAL, tepat sekali per respons.

    Kebijakan fallback kalau provider tidak mengirim `usage`: estimasi token
    input dari panjang pesan (~4 char/token) dan nol untuk output — TIDAK
    diam-diam menagih 0 dan tidak mengarang angka besar.
    """
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
            model=CACA_MODEL,
            is_billed=billed,
        )
        logger.info(
            "Caca settled | user_id=%s billed=%s prompt=%s completion=%s cost=%s credits",
            user_id, billed, prompt_tokens, completion_tokens, cost,
        )
    except Exception as e:
        # Settlement gagal: lepas reservasi supaya credit tidak tertahan selamanya.
        logger.error("Caca settle failed | user_id=%s err=%r", user_id, e, exc_info=True)
        if reservation_id:
            try:
                await asyncio.to_thread(caca_billing_db.release_reservation, reservation_id)
            except Exception as re:
                logger.warning("Reservation release after settle failure failed | err=%r", re)


async def caca_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    fresh_session = False
    stop = None
    typing = None
    reservation_id = None
    billed = False

    try:
        if msg.text and msg.text.startswith("/caca"):
            fresh_session = True
            prompt = " ".join(context.args).strip()

            if not prompt:
                return await _reply_thread(
                    context.bot,
                    msg,
                    f"{em} Pake gini:\n/caca <teks>\natau reply pesan gue buat ngobrol.",
                )
        elif msg.reply_to_message:
            history = await caca_memory.get_history(user_id)
            if not history:
                return await _reply_thread(context.bot, msg, "😒 I don't remember talking to you.\nUse /caca first.")
            prompt = (msg.text or "").strip()

        if not prompt:
            return

        # ── Credit gate ──────────────────────────────────────────────────────
        # Owner selalu lolos (billed=False). `/freecaca enable` memberi akses
        # gratis ke semua orang. Selain itu user wajib punya lot credit aktif.
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

        # Auto-search: jalan kalau pesannya mengandung kata pemicu (cariin, spek,
        # harga, berita, kapan rilis, dll). Hasilnya diselipkan ke framing,
        # bukan ke `caca_prompt.py`.
        search_result = ""
        if _needs_search(prompt):
            try:
                query = await _extract_search_query(prompt)
                logger.info("Caca auto-search | prompt=%r query=%r", prompt[:80], query)
                search_result = await _web_search_text(query)
                if search_result:
                    logger.info("Caca auto-search ok | query=%r len=%s", query, len(search_result))
                else:
                    logger.warning("Caca auto-search empty | query=%r", query)
            except Exception as e:
                logger.error("Caca auto-search failed | prompt=%r err=%r", prompt, e, exc_info=True)

        # Info fitur bot dari RAG: HANYA terisi kalau pertanyaannya soal bot
        # (gate intent di dalam retrieve_context sendiri). Chat biasa = 0 token.
        bot_info = await _bot_context(prompt)

        history = [] if fresh_session else await caca_memory.get_history(user_id)
        mode = caca_db.get_mode(user_id)
        system_prompt = PERSONAS.get(mode, PERSONAS["default"])
        user_prompt = prompt

        messages = _build_persona_messages(
            system_prompt, history, user_prompt,
            search_result=search_result,
            bot_info=bot_info,
        )

        billing = {"usage": {}}
        is_dm = getattr(msg.chat, "type", None) == "private" or msg.chat_id > 0
        if is_dm:
            await _caca_stream_dm(update, context, msg, user_id, history, prompt, messages, billing=billing)
            await _settle_billing(user_id, msg.chat_id, reservation_id, billing, messages, billed)
            return

        # Jalur grup: non-streaming Rich Message
        stop = asyncio.Event()
        thread_id = _get_thread_id(msg)
        typing = asyncio.create_task(_typing_loop(context.bot, msg.chat_id, stop, message_thread_id=thread_id))
        raw, usage = await _chat_completion(messages)
        billing["usage"].update(usage)
        clean_md = _clean_caca_output(raw) or "Model did not provide an answer."
        chunks = split_message(clean_md, 4000)
        await _stop_typing_task(stop, typing)
        last_id = await _send_chunks(context.bot, msg, chunks)
        history.extend([
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": clean_md},
        ])
        await caca_memory.set_history(user_id, history)
        if last_id:
            await caca_memory.set_last_message_id(user_id, last_id)
        await _settle_billing(user_id, msg.chat_id, reservation_id, billing, messages, billed)

    except Exception as e:
        # Kegagalan sebelum settlement: lepas reservasi supaya credit tidak
        # tertahan, dan JANGAN memotong credit sama sekali.
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
