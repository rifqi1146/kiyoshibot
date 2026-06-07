import os
import time
import uuid
import logging
import asyncio
import aiohttp
from aiohttp import web
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

# --- WEB CONFIG ---
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
PUBLIC_URL = os.getenv("PUBLIC_URL", f"http://{WEB_HOST}:{WEB_PORT}")
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")

WELCOME_ENABLED_CHATS = set()
VERIFIED_USERS = {}
PENDING_VERIFY = {}
PENDING_VERIFY_TASKS = {}
WELCOME_MESSAGES = {}
VERIFY_LOCKS = {}
VERIFY_TOKENS = {}  # nyimpen token web
BOT_INSTANCE = None # buat dipake di web handler

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
        log.warning(f"Failed to get profile photos for user {user.id}: {e}")

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
            log.warning(f"Failed to send welcome photo: {e}")

    try:
        return await context.bot.send_message(
            chat_id=chat.id,
            text=html_caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning(f"Failed to send HTML welcome text: {e}")

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
        except Exception:
            msg_id = None
    else:
        try:
            pop_pending_welcome(chat_id, user_id)
        except Exception:
            pass

    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

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
        except Exception:
            pass

async def _kick_unverified_user(bot, chat_id: int, user_id: int):
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, revoke_messages=False)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        log.info(f"Auto-kicked unverified user {user_id} from chat {chat_id}")
    except Exception as e:
        log.warning(f"Failed to auto-kick user {user_id}: {e}")

async def _should_enforce_verification(bot, chat_id: int, user_id: int) -> bool:
    if user_id in VERIFIED_USERS.get(chat_id, set()):
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
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
        log.warning(f"Verification timeout worker failed: {e}")
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
    except Exception:
        return

    now = time.time()
    for row in rows:
        chat_id = int(row["chat_id"])
        user_id = int(row["user_id"])
        message_id = int(row["message_id"])
        created_at = float(row["created_at"])

        key = _verify_key(chat_id, user_id)
        async with _get_verify_lock(chat_id, user_id):
            elapsed = max(0, now - created_at)
            if elapsed > RESTORE_MAX_AGE_SECONDS:
                try: pop_pending_welcome(chat_id, user_id)
                except Exception: pass
                continue

            should_enforce = await _should_enforce_verification(app.bot, chat_id, user_id)
            if not should_enforce:
                await _cleanup_pending_state(app.bot, chat_id, user_id, delete_message=True)
                continue

            WELCOME_MESSAGES[key] = message_id
            PENDING_VERIFY[key] = {
                "chat_id": chat_id,
                "user_id": user_id,
                "created_at": created_at,
            }

            remaining = VERIFY_TIMEOUT_SECONDS - elapsed
            if remaining <= 0:
                _schedule_verify_timeout(app, chat_id, user_id, delay=0)
            else:
                _schedule_verify_timeout(app, chat_id, user_id, delay=remaining)

async def _process_new_member(chat, user, context: ContextTypes.DEFAULT_TYPE):
    if chat.id not in WELCOME_ENABLED_CHATS:
        return

    bot_username = context.bot.username
    if not bot_username:
        me = await context.bot.get_me()
        bot_username = me.username or ""

    async with _get_verify_lock(chat.id, user.id):
        if user.id in VERIFIED_USERS.get(chat.id, set()):
            VERIFIED_USERS.setdefault(chat.id, set()).discard(user.id)
            try: delete_verified_user(chat.id, user.id)
            except Exception: pass

        await _cleanup_pending_state(context.bot, chat.id, user.id, delete_message=True)

        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
        except Exception as e:
            log.warning(f"Failed to restrict user: {e}")

        try:
            sent = await _send_welcome_message(context=context, chat=chat, user=user, bot_username=bot_username)
        except Exception as e:
            log.warning(f"Welcome message failed: {e}")
            return

        key = _verify_key(chat.id, user.id)
        WELCOME_MESSAGES[key] = sent.message_id
        try: save_pending_welcome(chat.id, user.id, sent.message_id)
        except Exception: pass

        PENDING_VERIFY[key] = {
            "chat_id": chat.id,
            "user_id": user.id,
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
    except Exception:
        return False

async def wlc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WELCOME_ENABLED_CHATS
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat or not await is_admin_or_owner(update, context):
        return

    arg = context.args[0].lower() if context.args else ""
    if arg == "enable":
        WELCOME_ENABLED_CHATS.add(chat.id)
        save_welcome_chats(WELCOME_ENABLED_CHATS)
        return await msg.reply_text("<b>Welcome message enabled.</b>", parse_mode="HTML")
    elif arg == "disable":
        WELCOME_ENABLED_CHATS.discard(chat.id)
        save_welcome_chats(WELCOME_ENABLED_CHATS)
        return await msg.reply_text("<b>Welcome message disabled.</b>", parse_mode="HTML")
    else:
        status_text = "enabled" if chat.id in WELCOME_ENABLED_CHATS else "disabled"
        return await msg.reply_text(f"<b>Welcome status:</b> <code>{status_text}</code>", parse_mode="HTML")

async def welcome_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu or cmu.chat.id not in WELCOME_ENABLED_CHATS:
        return
    old_status = getattr(cmu.old_chat_member, "status", None)
    new_status = getattr(cmu.new_chat_member, "status", None)
    if old_status in ("left", "kicked") and new_status in ("member", "restricted"):
        await _process_new_member(cmu.chat, cmu.new_chat_member.user, context)

async def welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat or not msg.new_chat_members or chat.id not in WELCOME_ENABLED_CHATS:
        return
    for user in msg.new_chat_members:
        await _process_new_member(chat, user, context)


# ===============================================
# WEB VERIFICATION HANDLERS
# ===============================================

async def start_verify_pm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        return

    arg = context.args[0]
    if not arg.startswith("verify_"):
        return

    try:
        _, chat_id, user_id = arg.split("_")
        chat_id, user_id = int(chat_id), int(user_id)
    except Exception:
        return

    if update.effective_user.id != user_id:
        return await update.message.reply_text("This verification request is not for you.")

    async with _get_verify_lock(chat_id, user_id):
        key = _verify_key(chat_id, user_id)
        pending = PENDING_VERIFY.get(key)
        if not pending:
            return await update.message.reply_text("Verification expired or not found. Please rejoin the group.")

        # Generate unique URL token
        token = str(uuid.uuid4())
        VERIFY_TOKENS[token] = key

        verify_url = f"{PUBLIC_URL}/verify?token={token}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Click here to Verify", url=verify_url)]
        ])

        await update.message.reply_text(
            "<b>Verify you are human</b>\n\nClick the button below to complete the CAPTCHA.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )


async def _on_verification_success(bot, chat_id: int, user_id: int):
    """Callback pas Web Server dapet validasi sukses dari Cloudflare"""
    async with _get_verify_lock(chat_id, user_id):
        key = _verify_key(chat_id, user_id)
        pending = PENDING_VERIFY.get(key)

        if not pending or pending["chat_id"] != chat_id:
            return

        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_invite_users=True, can_send_audios=True,
                    can_send_documents=True, can_send_messages=True,
                    can_send_other_messages=True, can_send_photos=True,
                    can_send_polls=True, can_send_video_notes=True,
                    can_send_videos=True, can_send_voice_notes=True,
                )
            )
        except Exception as e:
            log.warning(f"Failed to unrestrict user: {e}")

        VERIFIED_USERS.setdefault(chat_id, set()).add(user_id)
        try: save_verified_user(chat_id, user_id)
        except Exception: pass

        _cancel_verify_timeout(chat_id, user_id)
        PENDING_VERIFY.pop(key, None)

        await _delete_welcome_message(bot, chat_id, user_id)

        try:
            await bot.send_message(chat_id=user_id, text="<b>Verification successful!</b>\nYou can now return and chat in the group.", parse_mode="HTML")
        except Exception:
            pass

# --- WEB APP AIOHTTP ---
async def web_verify_get(request):
    token = request.query.get("token")
    if not token or token not in VERIFY_TOKENS:
        return web.Response(text="Invalid or expired verification token.", status=400)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Verification</title>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; background-color: #f0f2f5; margin: 0; }}
            .container {{ background: white; padding: 40px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); text-align: center; }}
        </style>
        <script>
            function onCaptchaSuccess() {{
                document.getElementById("verify-form").submit();
            }}
        </script>
    </head>
    <body>
        <div class="container">
            <h2>Human Verification</h2>
            <p style="color: #666; margin-bottom: 20px;">Please solve the captcha below to join the group.</p>
            <form id="verify-form" action="/verify" method="POST">
                <input type="hidden" name="token" value="{token}">
                <div class="cf-turnstile" data-sitekey="{TURNSTILE_SITE_KEY}" data-callback="onCaptchaSuccess"></div>
                <noscript>
                    <button type="submit" style="margin-top:15px; padding: 10px 20px; border: none; background: #0088cc; color: white; border-radius: 5px;">Submit</button>
                </noscript>
            </form>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

async def web_verify_post(request):
    data = await request.post()
    token = data.get("token")
    cf_response = data.get("cf-turnstile-response")

    log.info(f"WEB_DEBUG: Post received. Token: {token}, CF_Response: {bool(cf_response)}")

    if not token or token not in VERIFY_TOKENS:
        log.warning("WEB_DEBUG: Invalid token")
        return web.Response(text="Invalid or expired token.", status=400)

    if not cf_response:
        log.warning("WEB_DEBUG: Captcha empty")
        return web.Response(text="Captcha validation missing.", status=400)

    # Validasi ke server Cloudflare
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": TURNSTILE_SECRET_KEY,
                "response": cf_response
            }
        ) as resp:
            result = await resp.json()
            log.info(f"WEB_DEBUG: Cloudflare result: {result}")

    if not result.get("success"):
        log.warning("WEB_DEBUG: Cloudflare rejected")
        return web.Response(text="Verification failed.", status=403)

    chat_id, user_id = VERIFY_TOKENS.pop(token)
    
    if BOT_INSTANCE:
        log.info(f"WEB_DEBUG: Triggering unban for {user_id} in {chat_id}")
        await _on_verification_success(BOT_INSTANCE, chat_id, user_id)
    else:
        log.error("WEB_DEBUG: BOT_INSTANCE is None!")

    return web.Response(text="Success! You can go back.", content_type='text/html')



async def start_welcome_server(bot):
    """Fungsi ini dipanggil di post_init buat nyalain web server"""
    global BOT_INSTANCE
    BOT_INSTANCE = bot
    
    app = web.Application()
    app.add_routes([
        web.get('/verify', web_verify_get),
        web.post('/verify', web_verify_post)
    ])
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    log.info(f"Web Verification Server running on {WEB_HOST}:{WEB_PORT}")


# --- DB INIT ---
try: init_welcome_db()
except Exception: pass
try: WELCOME_ENABLED_CHATS = load_welcome_chats()
except Exception: WELCOME_ENABLED_CHATS = set()
try: VERIFIED_USERS = load_verified()
except Exception: VERIFIED_USERS = {}
