import asyncio
import logging
import os
import time
from telegram import Update
from telegram.ext import ContextTypes
from utils.config import OWNER_ID

log_cookies = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "data", "cookies.txt"))
COOKIES_DIR = os.path.dirname(COOKIES_PATH)

_MAX_FILE_SIZE = 512 * 1024
_EST_EXPIRE_SECONDS = 3 * 24 * 60 * 60


def _is_owner(user_id: int) -> bool:
    try:
        return int(user_id) in set(OWNER_ID)
    except Exception:
        return False


def _looks_like_netscape_cookies(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "HTTP Cookie File" in t:
        return True
    lines = [ln for ln in t.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return False
    cols = lines[0].split("\t")
    return len(cols) >= 6


async def cookies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    if not user or not _is_owner(user.id):
        return

    doc = None
    if msg.document:
        doc = msg.document
    elif msg.reply_to_message and msg.reply_to_message.document:
        doc = msg.reply_to_message.document

    if not doc:
        return await msg.reply_text(
            "<b>Send the cookies file as a document</b>.\n\n"
            "How to use:\n"
            "• Send <code>cookies.txt</code> then type <code>/cookies</code>\n"
            "• Or reply to the file message with <code>/cookies</code>",
            parse_mode="HTML",
        )

    if doc.file_size and doc.file_size > _MAX_FILE_SIZE:
        return await msg.reply_text(
            "<b>File too large.</b> Maximum 512KB.",
            parse_mode="HTML"
        )

    os.makedirs(COOKIES_DIR, exist_ok=True)
    tmp_path = COOKIES_PATH + ".uploading"

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=tmp_path)

        def _read_file():
            with open(tmp_path, "rb") as f:
                return f.read()

        raw = await asyncio.to_thread(_read_file)

        if not raw:
            raise RuntimeError("File is empty")

        text = raw.decode("utf-8", errors="ignore")

        if not _looks_like_netscape_cookies(text):
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return await msg.reply_text(
                "<b>Unrecognized cookies format.</b>\n"
                "Make sure it uses <b>Netscape cookies.txt</b> format.",
                parse_mode="HTML",
            )

        os.replace(tmp_path, COOKIES_PATH)
        try:
            from handlers.dl.tiktok import reset_tiktok_cookie_cache
            reset_tiktok_cookie_cache()
        except Exception as e:
            log_cookies.warning("TikTok cookie cache reset failed | err=%r", e)

        now = time.time()
        updated_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        exp_ts = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(now + _EST_EXPIRE_SECONDS),
        )

        return await msg.reply_text(
            "<b>Cookies successfully updated.</b>\n"
            f"Updated: <code>{updated_ts}</code>\n"
            f"Estimated expiration: <code>{exp_ts}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return await msg.reply_text(
            f"<b>Failed to update cookies:</b> <code>{e}</code>",
            parse_mode="HTML",
        )