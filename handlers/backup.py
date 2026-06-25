import os
import zipfile
import tempfile
import html
import asyncio
import sqlite3
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from utils.config import OWNER_ID, LOG_CHAT_ID

log = logging.getLogger(__name__)

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "bot.db")
INTERVAL = 6 * 60 * 60


def _normalize_owner_ids():
    if isinstance(OWNER_ID, (list, tuple, set)):
        return {int(x) for x in OWNER_ID}
    if OWNER_ID is None:
        return set()
    return {int(OWNER_ID)}


_OWNER_IDS = _normalize_owner_ids()


def _is_owner(user_id: int) -> bool:
    return user_id in _OWNER_IDS


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _get_db():
    _ensure_data_dir()
    return sqlite3.connect(DB_PATH, timeout=30)


def _init_db():
    with _get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur = db.execute("SELECT value FROM settings WHERE key = ?", ("auto_backup",))
        if not cur.fetchone():
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("auto_backup", "0")
            )
        db.commit()


def _get_setting(key: str, default: str = "0") -> str:
    _init_db()
    with _get_db() as db:
        cur = db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def _set_setting(key: str, value: str):
    _init_db()
    with _get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        db.commit()


def _zip_data(zip_path: str):
    _ensure_data_dir()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(DATA_DIR):
            for f in files:
                full_path = os.path.join(root, f)

                if os.path.abspath(full_path) == os.path.abspath(zip_path):
                    continue

                rel_path = os.path.relpath(full_path, DATA_DIR)
                z.write(full_path, rel_path)


def _safe_extract_zip(zip_path: str, extract_to: str):
    _ensure_data_dir()
    extract_root = os.path.abspath(extract_to)

    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            member_name = member.filename

            if not member_name:
                continue

            target_path = os.path.abspath(os.path.join(extract_root, member_name))

            if os.path.commonpath([extract_root, target_path]) != extract_root:
                raise ValueError(f"Unsafe path in zip: {member_name}")

            if member.is_dir():
                os.makedirs(target_path, exist_ok=True)
                continue

            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with z.open(member, "r") as src, open(target_path, "wb") as dst:
                dst.write(src.read())


async def _reload_runtime_state(app) -> list[str]:
    errors = []

    try:
        from handlers import welcome

        for key, task in list(welcome.PENDING_VERIFY_TASKS.items()):
            try:
                if task and not task.done():
                    task.cancel()
            except Exception as e:
                log.warning("Failed to cancel welcome task %s: %s", key, e)

        welcome.PENDING_VERIFY_TASKS.clear()
        welcome.PENDING_VERIFY.clear()
        welcome.WELCOME_MESSAGES.clear()

        welcome.WELCOME_ENABLED_CHATS = welcome.load_welcome_chats()
        welcome.VERIFIED_USERS = welcome.load_verified()
        await welcome.restore_pending_verifications(app)
        log.info(
            "✓ Welcome runtime reloaded | chats=%s verified_chats=%s pending=%s",
            len(welcome.WELCOME_ENABLED_CHATS),
            len(welcome.VERIFIED_USERS),
            len(welcome.PENDING_VERIFY),
        )
    except Exception:
        log.exception("Failed to reload welcome runtime state after restore")
        errors.append("welcome")

    try:
        from handlers.asupan import load_asupan_groups, load_autodel_groups

        load_asupan_groups()
        load_autodel_groups()
        log.info("✓ Asupan runtime reloaded")
    except Exception:
        log.exception("Failed to reload asupan runtime state after restore")
        errors.append("asupan")

    try:
        from database import premium

        premium.init()
        log.info("✓ Premium runtime reloaded")
    except Exception:
        log.exception("Failed to reload premium runtime state after restore")
        errors.append("premium")

    return errors


async def run_backup(bot):
    if not LOG_CHAT_ID:
        raise ValueError("LOG_CHAT_ID kosong")

    now = datetime.now().strftime("%d-%m-%Y_%H-%M")
    filename = f"backup_data_{now}.zip"
    zip_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            zip_path = tmp.name

        await asyncio.to_thread(_zip_data, zip_path)

        with open(zip_path, "rb") as f:
            await bot.send_document(
                chat_id=LOG_CHAT_ID,
                document=f,
                filename=filename,
                caption=f"Auto backup\n<code>{filename}</code>",
                parse_mode="HTML"
            )

    finally:
        if zip_path and os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError as e:
                log.warning(f"Failed to remove temp backup file: {e}")


async def auto_backup_loop(app):
    await asyncio.sleep(10)

    try:
        while True:
            try:
                enabled = _get_setting("auto_backup", "0") == "1"

                if enabled:
                    await run_backup(app.bot)
                    log.info("✓ Auto backup sent")

            except Exception:
                log.exception("Auto backup failed")

            await asyncio.sleep(INTERVAL)

    except asyncio.CancelledError:
        log.info("✓ Auto backup loop cancelled")
        raise

    finally:
        current_task = app.bot_data.get("auto_backup_task")
        if current_task is asyncio.current_task():
            app.bot_data.pop("auto_backup_task", None)


def start_auto_backup(app):
    _init_db()

    existing_task = app.bot_data.get("auto_backup_task")
    if existing_task and not existing_task.done():
        log.info("✓ Auto backup loop already running")
        return existing_task

    task = asyncio.create_task(auto_backup_loop(app))
    app.bot_data["auto_backup_task"] = task
    log.info("✓ Auto backup loop started")
    return task


async def stop_auto_backup(app):
    task = app.bot_data.get("auto_backup_task")
    if not task or task.done():
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    if not msg or not user:
        return

    if not _is_owner(user.id):
        return

    status = await msg.reply_text("Creating backup...")

    try:
        await run_backup(context.bot)
        await status.edit_text("Backup sent to log chat.")

    except Exception as e:
        await status.edit_text(
            f"Error:\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML"
        )


async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    if not msg or not user:
        return

    if not _is_owner(user.id):
        return

    reply = msg.reply_to_message
    doc = reply.document if reply else None

    if not doc:
        return await msg.reply_text("Reply ke file .zip dengan /restore")

    file_name = (doc.file_name or "").lower()
    if not file_name.endswith(".zip"):
        return await msg.reply_text("File harus archive .zip")

    status = await msg.reply_text("Downloading and restoring...")
    zip_path = None

    try:
        tg_file = await doc.get_file()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            zip_path = tmp.name

        await tg_file.download_to_drive(zip_path)

        await asyncio.to_thread(_safe_extract_zip, zip_path, DATA_DIR)

        await status.edit_text("Restore extracted. Reloading runtime state...")

        reload_errors = await _reload_runtime_state(context.application)

        if reload_errors:
            await status.edit_text(
                "Restore completed with partial reload issues.\n\n"
                f"<code>{html.escape(', '.join(reload_errors))}</code>",
                parse_mode="HTML"
            )
        else:
            await status.edit_text("Restore completed and runtime state reloaded.")

        if LOG_CHAT_ID:
            await context.bot.send_message(
                chat_id=LOG_CHAT_ID,
                text=(
                    "Data restore completed.\n"
                    f"Reload errors: {', '.join(reload_errors) if reload_errors else 'none'}"
                )
            )

    except Exception as e:
        log.exception("Restore failed")
        await status.edit_text(
            f"Error:\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML"
        )

    finally:
        if zip_path and os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError as e:
                log.warning(f"Failed to remove temp restore file: {e}")


async def autobackup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    if not msg or not user:
        return

    if not _is_owner(user.id):
        return

    if not context.args:
        enabled = _get_setting("auto_backup", "0") == "1"
        status = "enabled" if enabled else "disabled"
        return await msg.reply_text(f"Auto backup is {status}")

    cmd = context.args[0].lower()

    if cmd == "enable":
        _set_setting("auto_backup", "1")
        return await msg.reply_text("Auto backup enabled")

    if cmd == "disable":
        _set_setting("auto_backup", "0")
        return await msg.reply_text("Auto backup disabled")

    if cmd == "status":
        enabled = _get_setting("auto_backup", "0") == "1"
        status = "enabled" if enabled else "disabled"
        return await msg.reply_text(f"Auto backup is {status}")

    return await msg.reply_text("Usage: /autobackup [enable|disable|status]")
    