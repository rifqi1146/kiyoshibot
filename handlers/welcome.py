import random
import time
import logging
import asyncio
import html as html_lib

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
)
from telegram.ext import ContextTypes

from utils.config import OWNER_ID
from database.welcome_db import (
    init_welcome_db,
    load_welcome_chats,
    save_welcome_chats,
    load_verified,
    save_verified_user,
    delete_verified_user,
    save_pending_welcome,
    load_pending_welcomes,
    pop_pending_welcome,
)

log = logging.getLogger(__name__)
logger = logging.getLogger(__name__)

WELCOME_ENABLED_CHATS = set()
VERIFIED_USERS = {}
PENDING_VERIFY = {}
PENDING_VERIFY_TASKS = {}
WELCOME_MESSAGES = {}
VERIFY_LOCKS = {}

VERIFY_TIMEOUT_SECONDS = 5 * 60
RESTORE_MAX_AGE_SECONDS = 15 * 60


def _verify_key(chat_id: int, user_id: int):
    return (chat_id, user_id)


def _get_verify_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    key = _verify_key(chat_id, user_id)
    lock = VERIFY_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        VERIFY_LOCKS[key] = lock
    return lock


def generate_math_question(user_id: int, chat_id: int):
    op = random.choice(["+", "-"])

    if op == "+":
        a = random.randint(10, 99)
        b = random.randint(10, 99)
        answer = a + b
    else:
        a = random.randint(20, 99)
        b = random.randint(1, 50)
        if b > a:
            a, b = b, a
        answer = a - b

    wrong = set()
    while len(wrong) < 3:
        x = random.randint(answer - 30, answer + 30)
        if x != answer and x >= 0:
            wrong.add(x)

    options = list(wrong) + [answer]
    random.shuffle(options)

    key = _verify_key(chat_id, user_id)
    current = PENDING_VERIFY.get(key) or {}
    PENDING_VERIFY[key] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "answer": answer,
        "created_at": current.get("created_at", time.time()),
    }

    buttons = [
        [InlineKeyboardButton(str(o), callback_data=f"verify_ans:{chat_id}:{user_id}:{o}")]
        for o in options
    ]

    text = (
        "Answer the following math question\n\n"
        f"<b>{a} {op} {b} = ?</b>\n\n"
    )

    return text, InlineKeyboardMarkup(buttons)


def verify_keyboard(user_id: int, chat_id: int, bot_username: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Verify",
                url=f"https://t.me/{bot_username}?start=verify_{chat_id}_{user_id}"
            )
        ]
    ])


def _cancel_verify_timeout(chat_id: int, user_id: int):
    key = _verify_key(chat_id, user_id)
    task = PENDING_VERIFY_TASKS.pop(key, None)
    if task and not task.done():
        task.cancel()


def _build_welcome_texts(user, chat):
    raw_username = f"@{user.username}" if user.username else "—"
    raw_fullname = user.full_name or "Unknown User"
    raw_chatname = chat.title or "this group"

    username_html = html_lib.escape(raw_username)
    fullname_html = html_lib.escape(raw_fullname)
    chatname_html = html_lib.escape(raw_chatname)

    username_plain = raw_username
    fullname_plain = raw_fullname
    chatname_plain = raw_chatname

    html_caption = (
        f"👋 <b>Hello {fullname_html}</b>\n"
        f"Welcome to <b>{chatname_html}</b>\n\n"
        f"🧾 <b>User Information</b>\n"
        f"🆔 ID       : <code>{user.id}</code>\n"
        f"👤 Name     : {fullname_html}\n"
        f"🔖 Username : {username_html}\n\n"
        f"🔐 <b>Please complete verification first</b>\n"
        f"⏳ <i>You have 5 minutes to verify.</i>"
    )

    plain_caption = (
        f"👋 Hello {fullname_plain}\n"
        f"Welcome to {chatname_plain}\n\n"
        f"🧾 User Information\n"
        f"🆔 ID       : {user.id}\n"
        f"👤 Name     : {fullname_plain}\n"
        f"🔖 Username : {username_plain}\n\n"
        f"🔐 Please complete verification first\n"
        f"⏳ You have 5 minutes to verify."
    )

    return html_caption, plain_caption


async def _send_welcome_message(context: ContextTypes.DEFAULT_TYPE, chat, user, bot_username: str):
    html_caption, plain_caption = _build_welcome_texts(user, chat)
    keyboard = verify_keyboard(user.id, chat.id, bot_username)

    photos = None
    try:
        photos = await context.bot.get_user_profile_photos(user_id=user.id, limit=1)
    except Exception as e:
        log.warning(f"Failed to get profile photos for user {user.id} in chat {chat.id}: {e}")

    if photos and photos.total_count > 0:
        try:
            return await context.bot.send_photo(
                chat_id=chat.id,
                photo=photos.photos[0][-1].file_id,
                caption=html_caption,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as e:
            log.warning(f"Failed to send welcome photo for user {user.id} in chat {chat.id}: {e}")

    try:
        return await context.bot.send_message(
            chat_id=chat.id,
            text=html_caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning(f"Failed to send HTML welcome text for user {user.id} in chat {chat.id}: {e}")

    return await context.bot.send_message(
        chat_id=chat.id,
        text=plain_caption,
        reply_markup=keyboard,
    )


async def _delete_welcome_message(bot, chat_id: int, user_id: int):
    key = _verify_key(chat_id, user_id)
    msg_id = WELCOME_MESSAGES.pop(key, None)

    if msg_id is None:
        try:
            msg_id = pop_pending_welcome(chat_id, user_id)
        except Exception as e:
            log.warning(
                f"Failed to restore pending welcome message id for user {user_id} in chat {chat_id}: {e}"
            )
            msg_id = None
    else:
        try:
            pop_pending_welcome(chat_id, user_id)
        except Exception as e:
            log.warning(
                f"Failed to remove pending welcome record for user {user_id} in chat {chat_id}: {e}"
            )

    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            log.warning(f"Failed to delete welcome message for user {user_id} in chat {chat_id}: {e}")


async def _cleanup_pending_state(bot, chat_id: int, user_id: int, delete_message: bool = True):
    key = _verify_key(chat_id, user_id)
    _cancel_verify_timeout(chat_id, user_id)
    PENDING_VERIFY.pop(key, None)

    if delete_message:
        await _delete_welcome_message(bot, chat_id, user_id)
    else:
        WELCOME_MESSAGES.pop(key, None)
        try:
            pop_pending_welcome(chat_id, user_id)
        except Exception as e:
            log.warning(
                f"Failed to clear pending welcome state for user {user_id} in chat {chat_id}: {e}"
            )


async def _kick_unverified_user(bot, chat_id: int, user_id: int):
    try:
        await bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            revoke_messages=False,
        )
        await bot.unban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            only_if_banned=True,
        )
        log.info(f"Auto-kicked unverified user {user_id} from chat {chat_id}")
    except Exception as e:
        log.warning(f"Failed to auto-kick user {user_id} from chat {chat_id}: {e}")


async def _should_enforce_verification(bot, chat_id: int, user_id: int) -> bool:
    if user_id in VERIFIED_USERS.get(chat_id, set()):
        return False

    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        log.warning(f"Failed to inspect member {user_id} in chat {chat_id}: {e}")
        return False

    status = getattr(member, "status", None)

    if status in ("left", "kicked"):
        return False

    return status == "restricted"


async def _verify_timeout_worker(app, chat_id: int, user_id: int, delay: float):
    key = _verify_key(chat_id, user_id)

    try:
        if delay > 0:
            await asyncio.sleep(delay)

        async with _get_verify_lock(chat_id, user_id):
            pending = PENDING_VERIFY.get(key)
            if not pending:
                return

            should_enforce = await _should_enforce_verification(app.bot, chat_id, user_id)
            if not should_enforce:
                await _cleanup_pending_state(app.bot, chat_id, user_id, delete_message=True)
                return

            PENDING_VERIFY.pop(key, None)

            await _delete_welcome_message(app.bot, chat_id, user_id)
            await _kick_unverified_user(app.bot, chat_id, user_id)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning(f"Verification timeout worker failed for user {user_id} in chat {chat_id}: {e}")
    finally:
        current_task = PENDING_VERIFY_TASKS.get(key)
        if current_task is asyncio.current_task():
            PENDING_VERIFY_TASKS.pop(key, None)


def _schedule_verify_timeout(app, chat_id: int, user_id: int, delay: float = VERIFY_TIMEOUT_SECONDS):
    key = _verify_key(chat_id, user_id)

    _cancel_verify_timeout(chat_id, user_id)

    task = asyncio.create_task(_verify_timeout_worker(app, chat_id, user_id, delay))
    PENDING_VERIFY_TASKS[key] = task
    return task


async def restore_pending_verifications(app):
    try:
        rows = load_pending_welcomes()
    except Exception as e:
        log.warning(f"Failed to load pending welcome verifications: {e}")
        return

    now = time.time()
    restored = 0
    skipped = 0
    expired = 0

    for row in rows:
        chat_id = int(row["chat_id"])
        user_id = int(row["user_id"])
        message_id = int(row["message_id"])
        created_at = float(row["created_at"])

        key = _verify_key(chat_id, user_id)

        async with _get_verify_lock(chat_id, user_id):
            elapsed = max(0, now - created_at)
            if elapsed > RESTORE_MAX_AGE_SECONDS:
                try:
                    pop_pending_welcome(chat_id, user_id)
                except Exception as e:
                    log.warning(
                        f"Failed to drop expired pending welcome for user {user_id} in chat {chat_id}: {e}"
                    )
                WELCOME_MESSAGES.pop(key, None)
                PENDING_VERIFY.pop(key, None)
                skipped += 1
                continue

            should_enforce = await _should_enforce_verification(app.bot, chat_id, user_id)
            if not should_enforce:
                await _cleanup_pending_state(app.bot, chat_id, user_id, delete_message=True)
                skipped += 1
                continue

            WELCOME_MESSAGES[key] = message_id
            PENDING_VERIFY[key] = {
                "chat_id": chat_id,
                "user_id": user_id,
                "answer": None,
                "created_at": created_at,
            }

            remaining = VERIFY_TIMEOUT_SECONDS - elapsed

            if remaining <= 0:
                _schedule_verify_timeout(app, chat_id, user_id, delay=0)
                expired += 1
            else:
                _schedule_verify_timeout(app, chat_id, user_id, delay=remaining)
                restored += 1

    if restored or skipped or expired:
        log.info(
            f"✓ Restored pending welcomes: active={restored}, skipped={skipped}, expired={expired}"
        )

async def _process_new_member(chat, user, context: ContextTypes.DEFAULT_TYPE):
    if chat.id not in WELCOME_ENABLED_CHATS:
        log.info(f"Welcome skipped in chat {chat.id}: not enabled")
        return

    bot_username = context.bot.username
    if not bot_username:
        try:
            me = await context.bot.get_me()
            bot_username = me.username or ""
        except Exception as e:
            log.warning(f"Failed to resolve bot username in chat {chat.id}: {e}")
            bot_username = ""

    async with _get_verify_lock(chat.id, user.id):
        if user.id in VERIFIED_USERS.get(chat.id, set()):
            VERIFIED_USERS.setdefault(chat.id, set()).discard(user.id)
            try:
                delete_verified_user(chat.id, user.id)
            except Exception as e:
                log.warning(
                    f"Failed to clear verified status for rejoined user {user.id} in chat {chat.id}: {e}"
                )

        await _cleanup_pending_state(context.bot, chat.id, user.id, delete_message=True)

        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
        except Exception as e:
            log.warning(f"Failed to restrict user {user.id} in chat {chat.id}: {e}")

        try:
            sent = await _send_welcome_message(
                context=context,
                chat=chat,
                user=user,
                bot_username=bot_username,
            )
        except Exception as e:
            log.warning(f"Welcome message failed for user {user.id} in chat {chat.id}: {e}")
            return

        key = _verify_key(chat.id, user.id)
        WELCOME_MESSAGES[key] = sent.message_id

        try:
            save_pending_welcome(chat.id, user.id, sent.message_id)
        except Exception as e:
            log.warning(f"Failed to save pending welcome for user {user.id} in chat {chat.id}: {e}")

        PENDING_VERIFY[key] = {
            "chat_id": chat.id,
            "user_id": user.id,
            "answer": None,
            "created_at": time.time(),
        }
        _schedule_verify_timeout(context.application, chat.id, user.id)
        

async def is_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    if user.id in OWNER_ID:
        return True

    if chat.type not in ("group", "supergroup"):
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.warning(
            f"Failed to check admin status for user {user.id} in chat {chat.id}: {e}"
        )
        return False


async def wlc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WELCOME_ENABLED_CHATS

    msg = update.message
    chat = update.effective_chat
    if not msg or not chat:
        return

    if not await is_admin_or_owner(update, context):
        return

    arg = ""
    if context.args:
        arg = (context.args[0] or "").strip().lower()
    else:
        raw = (msg.text or "").strip()
        parts = raw.split(maxsplit=1)
        if len(parts) == 2:
            arg = parts[1].strip().split()[0].lower()

    if not arg:
        return await msg.reply_text(
            "Usage:\n"
            "<code>/wlc enable</code>\n"
            "<code>/wlc disable</code>\n"
            "<code>/wlc status</code>",
            parse_mode="HTML"
        )

    if arg == "enable":
        WELCOME_ENABLED_CHATS.add(chat.id)
        save_welcome_chats(WELCOME_ENABLED_CHATS)
        log.info(f"Welcome enabled in chat {chat.id}")
        return await msg.reply_text("<b>Welcome message enabled.</b>", parse_mode="HTML")

    if arg == "disable":
        WELCOME_ENABLED_CHATS.discard(chat.id)
        save_welcome_chats(WELCOME_ENABLED_CHATS)
        log.info(f"Welcome disabled in chat {chat.id}")
        return await msg.reply_text("<b>Welcome message disabled.</b>", parse_mode="HTML")

    if arg == "status":
        enabled = chat.id in WELCOME_ENABLED_CHATS
        status_text = "enabled" if enabled else "disabled"
        return await msg.reply_text(
            f"<b>Welcome status:</b> <code>{status_text}</code>",
            parse_mode="HTML"
        )

    return await msg.reply_text(
        "Use <code>enable</code>, <code>disable</code>, or <code>status</code>.",
        parse_mode="HTML"
    )

async def welcome_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return

    chat = cmu.chat
    old_status = getattr(cmu.old_chat_member, "status", None)
    new_status = getattr(cmu.new_chat_member, "status", None)
    user = cmu.new_chat_member.user

    if chat.id not in WELCOME_ENABLED_CHATS:
        return

    joined = (
        old_status in ("left", "kicked")
        and new_status in ("member", "restricted")
    )

    if not joined:
        return

    await _process_new_member(chat, user, context)

async def welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not chat or not msg.new_chat_members:
        return

    if chat.id not in WELCOME_ENABLED_CHATS:
        log.info("Welcome skipped in chat %s: not enabled", chat.id)
        return

    for user in msg.new_chat_members:
        await _process_new_member(chat, user, context)


async def start_verify_pm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        return

    arg = context.args[0]
    if not arg.startswith("verify_"):
        return

    try:
        _, chat_id, user_id = arg.split("_")
        chat_id = int(chat_id)
        user_id = int(user_id)
    except Exception:
        return

    if update.effective_user.id != user_id:
        return await update.message.reply_text(
            "This verification request is not for you."
        )

    async with _get_verify_lock(chat_id, user_id):
        key = _verify_key(chat_id, user_id)
        pending = PENDING_VERIFY.get(key)
        if not pending:
            return await update.message.reply_text(
                "Verification expired or not found. Please rejoin the group."
            )

        text, keyboard = generate_math_question(user_id, chat_id)

        await update.message.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )


async def verify_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global VERIFIED_USERS

    q = update.callback_query
    if not q or not q.data:
        return

    try:
        _, chat_id, user_id, chosen = q.data.split(":")
        chat_id = int(chat_id)
        user_id = int(user_id)
        chosen = int(chosen)
    except Exception:
        await q.answer("Invalid verification data.", show_alert=True)
        return

    if q.from_user.id != user_id:
        await q.answer("This is not your verification button.", show_alert=True)
        return

    async with _get_verify_lock(chat_id, user_id):
        key = _verify_key(chat_id, user_id)
        pending = PENDING_VERIFY.get(key)

        if not pending or pending["chat_id"] != chat_id:
            await q.answer("Invalid verification.", show_alert=True)
            return

        if pending.get("answer") is None:
            await q.answer("Please start verification from the bot chat first.", show_alert=True)
            return

        if chosen != pending["answer"]:
            await q.answer("Wrong answer. Try again.", show_alert=True)
            text, keyboard = generate_math_question(user_id, chat_id)
            try:
                await q.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                log.warning(
                    f"Failed to edit verification question for user {user_id} in chat {chat_id}: {e}"
                )
                try:
                    await q.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
                except Exception as reply_err:
                    log.warning(
                        f"Failed to resend verification question for user {user_id} in chat {chat_id}: {reply_err}"
                    )
            return

        await q.answer("Verification successful!", show_alert=False)

        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_invite_users=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_messages=True,
                    can_send_other_messages=True,
                    can_send_photos=True,
                    can_send_polls=True,
                    can_send_video_notes=True,
                    can_send_videos=True,
                    can_send_voice_notes=True,
                )
            )
        except Exception as e:
            log.warning(f"Failed to unrestrict verified user {user_id} in chat {chat_id}: {e}")

        VERIFIED_USERS.setdefault(chat_id, set()).add(user_id)
        try:
            save_verified_user(chat_id, user_id)
        except Exception as e:
            log.warning(f"Failed to persist verified user {user_id} in chat {chat_id}: {e}")

        _cancel_verify_timeout(chat_id, user_id)
        PENDING_VERIFY.pop(key, None)

        await _delete_welcome_message(context.bot, chat_id, user_id)

        try:
            await q.message.edit_text("Verification successful. You may return to the group.")
        except Exception as e:
            log.warning(
                f"Failed to edit verification success message for user {user_id} in chat {chat_id}: {e}"
            )
            try:
                await context.bot.send_message(
                    chat_id=q.message.chat_id,
                    text="Verification successful. You may return to the group."
                )
            except Exception as send_err:
                log.warning(
                    f"Failed to send verification success fallback for user {user_id} in chat {chat_id}: {send_err}"
                )


try:
    init_welcome_db()
except Exception:
    log.exception("Failed to initialize welcome database")

try:
    WELCOME_ENABLED_CHATS = load_welcome_chats()
except Exception:
    log.exception("Failed to load welcome-enabled chats; using empty set")
    WELCOME_ENABLED_CHATS = set()

try:
    VERIFIED_USERS = load_verified()
except Exception:
    log.exception("Failed to load verified users; using empty cache")
    VERIFIED_USERS = {}