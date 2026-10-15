import re
import os
import base64
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
from . import codebuddy

logger = logging.getLogger(__name__)

# Provider Caca: Shiteru (OpenAI-compatible). Model utama `deepseek-v4-flash`.
# Dipakai sebagai FALLBACK kalau CodeBuddy tidak dikonfigurasi di `.env`.
CACA_API_BASE = os.getenv("CACA_API_BASE", "https://shiteru.id/v1").strip().rstrip("/")
CACA_ENDPOINT = f"{CACA_API_BASE}/chat/completions"
CACA_MODEL = os.getenv("CACA_MODEL", "deepseek-v4-flash").strip()
# Batas output token keras dari permintaan owner: jangan pernah lewat 4096.
CACA_MAX_TOKENS = min(int(os.getenv("CACA_MAX_TOKENS", "4096")), 4096)
CACA_TIMEOUT = int(os.getenv("CACA_TIMEOUT", "60"))

# Provider utama Caca: CodeBuddy intl (`www.codebuddy.ai`), stream-only.
# Aktif otomatis kalau `CODEBUDDY_REFRESH_TOKEN` / `CODEBUDDY_ACCESS_TOKEN`
# ada di `.env`; kalau tidak, Caca jatuh ke Shiteru seperti semula.
# `reasoning_effort: "none"` dikirim supaya thinking mati (lihat codebuddy.py).
CACA_PROVIDER = os.getenv("CACA_PROVIDER", "codebuddy").strip().lower()
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


# ── Vision: Caca bisa lihat foto & stiker statis ─────────────────────────────
# Batas ukuran file mentah sebelum di-encode. Dipilih 5 MB: cukup untuk foto
# Telegram (compress ~200-500 KB, dokumen sampai beberapa MB) tanpa bikin
# payload base64 membengkak (~4/3 dari ukuran asli).
CACA_MAX_IMAGE_BYTES = 5 * 1024 * 1024

# MIME yang diterima provider. Semua ini sudah diuji live ke Shiteru.
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
    """Ambil media dari pesan Telegram -> data URL base64 siap dikirim ke model.

    `source` adalah objek pesan yang memuat media (pesan user sendiri, atau
    `msg.reply_to_message` kalau user membalas gambar). `label_key` menentukan
    teks ringkas yang dicatat ke memori (mis. "reply_photo").

    Return `(data_url, media_label, error_reason)`; hanya satu yang terisi.
    Media DIAMBIL KE MEMORI (`download_as_bytearray`) — tidak menulis ke disk,
    dan TIDAK disimpan ke riwayat chat (lihat `caca_cmd`).
    """
    if source is None:
        return None, "", "kosong"

    file_obj = None
    mime = None
    filename = ""

    try:
        if getattr(source, "photo", None):
            # photo adalah list ukuran; ambil yang terbesar
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
    """Tentukan dari mana media diambil + label memorinya.

    Prioritas:
      1. Media di pesan user sendiri (foto/stiker/dokumen yang dia kirim).
      2. Media di pesan yang user balas (`msg.reply_to_message`).
    Return `(source, label_key)` atau `(None, "")`.
    """
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


# Pesan ramah saat media tidak bisa dibaca, tetap dalam karakter Caca.
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


# ── Pemilihan provider Caca ──────────────────────────────────────────────────
# Owner bisa memaksa provider lewat `/providercaca shiteru|codebuddy`. Pilihan
# itu disimpan di `data/caca_provider.txt` supaya tahan restart, dan mengalahkan
# default dari env. Kalau file tidak ada, pakai `CACA_PROVIDER`.
_PROVIDER_FILE = os.path.join("data", "caca_provider.txt")
_PROVIDER_CHOICES = ("shiteru", "codebuddy")


def get_provider() -> str:
    """Provider Caca yang sedang aktif: 'codebuddy' atau 'shiteru'."""
    try:
        if os.path.exists(_PROVIDER_FILE):
            forced = (open(_PROVIDER_FILE, encoding="utf-8").read().strip().lower())
            if forced in _PROVIDER_CHOICES:
                return forced
    except Exception as e:
        logger.warning("Gagal baca caca_provider.txt | err=%s", e)
    return CACA_PROVIDER if CACA_PROVIDER in _PROVIDER_CHOICES else "codebuddy"


def set_provider(name: str) -> bool:
    """Simpan pilihan provider. False kalau namanya tidak dikenal."""
    name = (name or "").strip().lower()
    if name not in _PROVIDER_CHOICES:
        return False
    try:
        os.makedirs(os.path.dirname(_PROVIDER_FILE) or ".", exist_ok=True)
        with open(_PROVIDER_FILE, "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
        if name == "codebuddy":
            # Buang token cache supaya switch langsung pakai kredensial segar.
            codebuddy.reset_token_cache()
        return True
    except Exception as e:
        logger.warning("Gagal simpan provider Caca | err=%s", e)
        return False


def _use_codebuddy() -> bool:
    """True kalau provider aktif CodeBuddy DAN kredensialnya tersedia."""
    return get_provider() == "codebuddy" and codebuddy.configured()


def _codebuddy_model() -> str:
    return os.getenv("CODEBUDDY_MODEL", "deepseek-v4.1-flash").strip() or "deepseek-v4.1-flash"


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
    """Panggilan chat non-streaming. Mengembalikan `(content, usage)`.

    Provider utama = CodeBuddy intl (stream-only, jadi delta dikumpulkan dulu).
    Kalau CodeBuddy tidak dikonfigurasi, jatuh ke Shiteru seperti semula.
    """
    if _use_codebuddy():
        session = await _shared_http_session()
        try:
            content, raw_usage = await codebuddy.chat(
                session,
                messages,
                max_tokens=CACA_MAX_TOKENS,
                temperature=0.9,
                top_p=0.95,
                timeout=CACA_TIMEOUT,
            )
            usage = _usage_from({"usage": raw_usage}) if raw_usage else _usage_from({})
            logger.info(
                "Caca chat success | provider=codebuddy model=%s tokens_max=%s prompt=%s completion=%s",
                _codebuddy_model(), CACA_MAX_TOKENS, usage["prompt_tokens"], usage["completion_tokens"],
            )
            return content, usage
        except Exception as e:
            logger.warning("Caca CodeBuddy gagal, fallback ke Shiteru | err=%s", e)
            if not _caca_api_key():
                raise

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
    """Async generator delta streaming Caca (CodeBuddy atau Shiteru).

    `usage_out` (opsional) akan diisi dengan `usage` provider saat stream selesai.
    Provider utama = CodeBuddy intl (thinking dimatikan via `reasoning_effort: 'none'`).
    Kalau CodeBuddy tidak dikonfigurasi, jatuh ke Shiteru seperti semula.

    Catatan model thinking: delta memuat `reasoning_content` sebelum `content`.
    Kita HANYA me-yield `content` (jawaban akhir Caca), mengabaikan
    `reasoning_content` agar draft PM tidak kotor dengan monolog internal.
    """
    if _use_codebuddy():
        session = await _shared_http_session()
        got_any = False
        try:
            async for piece in codebuddy.chat_stream(
                session,
                messages,
                max_tokens=CACA_MAX_TOKENS,
                temperature=0.9,
                top_p=0.95,
                timeout=CACA_TIMEOUT,
                usage_out=usage_out,
            ):
                got_any = True
                yield piece
            if got_any:
                logger.info("Caca stream success | provider=codebuddy model=%s tokens_max=%s usage=%s",
                            _codebuddy_model(), CACA_MAX_TOKENS, usage_out or {})
                return
        except Exception as e:
            if got_any:
                # Sebagian chunk sudah ter-emit ke draft Telegram; fallback ke
                # Shiteru akan mengulang dari awal dan bikin respon dobel.
                logger.error("Caca CodeBuddy putus di tengah stream | err=%s", e)
                raise
            logger.warning("Caca CodeBuddy stream gagal, fallback ke Shiteru | err=%s", e)
            if not _caca_api_key():
                raise

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


def _build_persona_messages(system_prompt: str, history: list, user_prompt: str, search_result: str = "", bot_info: str = "", images: list | None = None) -> list[dict]:
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

    # ── Aturan hubungan (SELALU hadir, tiap turn) ─────────────────────────────
    # Tidak boleh ikut "terkubur" di framing turn pertama saja: kalau riwayat
    # lama sudah tersimpan di DB, framing itu tidak disuntik ulang, dan model
    # kembali menebak gender user (pernah salah mengira pacar sebagai cewe).
    # Karena itu blok ini ditempel di turn user SETIAP kali, seperti search/bot.
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

    # ── Vision: sisipkan gambar HANYA di turn user saat ini ───────────────────
    # Gambar tidak pernah masuk `history` (lihat `caca_cmd`), jadi base64 besar
    # tidak dikirim ulang di setiap pesan berikutnya. Kalau ada gambar, content
    # turn user berubah jadi list multimodal (teks + image_url).
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

    # Kalau ada search_result / info bot di turn lanjutan, selipkan di pesan user
    # saat ini supaya model membaca data baru tanpa merusak turn pertama.
    # `relation_block` SELALU ikut (aturan gender & cemburu tidak boleh hilang).
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
    media_label = ""
    image_urls: list[str] = []

    caption = (msg.caption or "").strip()
    text = (msg.text or "").strip()

    try:
        # ── Teks/caption yang memicu sesi baru: "/caca ..." ───────────────────
        if text.startswith("/caca") or caption.startswith("/caca"):
            fresh_session = True
            # context.args hanya terisi untuk pesan teks, jadi untuk caption
            # kita potong manual dari awal string.
            if caption.startswith("/caca"):
                prompt = caption[len("/caca"):].strip()
            else:
                prompt = " ".join(context.args).strip()

            # `/caca` polos tanpa teks: boleh kalau ada gambar yang menyertai.
            # Gambar dibiarkan bicara sendiri — TIDAK diinjeksi teks paksa.
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

        # ── Lanjutan obrolan: reply ke pesan Caca (boleh bawa gambar) ─────────
        elif msg.reply_to_message:
            history = await caca_memory.get_history(user_id)
            if not history:
                return await _reply_thread(context.bot, msg, "😒 I don't remember talking to you.\nUse /caca first.")
            prompt = text or caption

            # Gambar dibiarkan bicara sendiri; teks hanya dipakai kalau ada.
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
            images=image_urls or None,
        )

        # Turn yang dicatat ke memori memakai TEKS RINGKAS, bukan base64 gambar.
        # Kalau gambar ikut disimpan, (a) DB membengkak dan (b) setiap pesan
        # berikutnya mengirim ulang gambar itu -> token meledak. Yang disimpan
        # cukup penanda "[Kirim foto] <teks>", sehingga Caca tetap ingat konteks.
        memory_prompt = f"{media_label} {prompt}".strip() if media_label else prompt

        billing = {"usage": {}}
        is_dm = getattr(msg.chat, "type", None) == "private" or msg.chat_id > 0
        if is_dm:
            await _caca_stream_dm(update, context, msg, user_id, history, memory_prompt, messages, billing=billing)
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
            {"role": "user", "content": memory_prompt},
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
