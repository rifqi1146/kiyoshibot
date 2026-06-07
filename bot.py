#!/usr/bin/env python3
import os
import re
import socket
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder,JobQueue
from utils.http import close_http_session
from handlers.commands import register_commands
from handlers.callbacks import register_callbacks
from handlers.messages import register_messages
from utils.startup import startup_tasks
from utils.config import BOT_TOKEN
from handlers.dl.mtproto_uploader import warmup_mtproto_uploader,shutdown_mtproto_uploader
from handlers.dl.pyrogram_uploader import warmup_pyrogram_uploader,shutdown_pyrogram_uploader
from handlers.welcome import start_welcome_server

BOT_USERNAME=None
LOCAL_BOT_API_HOST=os.getenv("LOCAL_BOT_API_HOST","127.0.0.1")
LOCAL_BOT_API_PORT=int(os.getenv("LOCAL_BOT_API_PORT","8081"))
PREFER_LOCAL_BOT_API=os.getenv("PREFER_LOCAL_BOT_API","1").strip().lower() not in ("0","false","no")

BOT_COMMANDS=[
    ("start","Check bot status"),
    ("aidetect","Detect AI-generated image"),
    ("aitext","Detect AI-generated text"),
    ("donate","Support bot"),
    ("help","Show help menu"),
    ("settings","User settings"),
    ("nobg","Remove background"),
    ("upscale","Upscale image"),
    ("quiz","Random quiz"),
    ("ping","Check latency"),
    ("kang","Add sticker to pack"),
    ("ship","Choose couple"),
    ("quoteanime","Random anime quote"),
    ("susunkata","Play word arrangement game"),
    ("stats","System statistics"),
    ("dl","Download video"),
    ("manga","Read manga"),
    ("ask","Ask Gemini AI"),
    ("music","Search music"),
    ("caca","Chat with Caca"),
    ("groq","Ask Groq AI"),
    ("gsearch","Google search"),
    ("asupan","Random asupan"),
    ("tr","Translate text"),
]

class TokenRedactFilter(logging.Filter):
    TOKEN_RE=re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
    TOKEN_TEXT_RE=re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")

    def _clean_text(self,text:str)->str:
        text=self.TOKEN_RE.sub("/bot<redacted>",text)
        text=self.TOKEN_TEXT_RE.sub("<bot-token-redacted>",text)
        return text

    def _clean_arg(self,value):
        return self._clean_text(value) if isinstance(value,str) else value

    def filter(self,record):
        if isinstance(record.msg,str):
            record.msg=self._clean_text(record.msg)
        if record.args:
            if isinstance(record.args,dict):
                record.args={key:self._clean_arg(value) for key,value in record.args.items()}
            elif isinstance(record.args,tuple):
                record.args=tuple(self._clean_arg(arg) for arg in record.args)
            else:
                record.args=self._clean_arg(record.args)
        return True

class EmojiFormatter(logging.Formatter):
    EMOJI={
        logging.INFO:"➜",
        logging.WARNING:"⚠️",
        logging.ERROR:"❌",
        logging.CRITICAL:"💥",
    }

    def format(self,record):
        emoji=self.EMOJI.get(record.levelno,"•")
        msg=str(record.msg)
        if not msg.startswith(("➜","⚠️","❌","💥","•")):
            record.msg=f"{emoji} {msg}"
        return super().format(record)

def setup_logger():
    handler=logging.StreamHandler()
    handler.addFilter(TokenRedactFilter())
    handler.setFormatter(EmojiFormatter("[%(asctime)s] %(message)s","%H:%M:%S"))

    root=logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)

    quiet_error_loggers=(
        "pyrogram",
        "pyrogram.client",
        "pyrogram.connection",
        "pyrogram.connection.connection",
        "pyrogram.connection.transport",
        "pyrogram.connection.transport.tcp",
        "pyrogram.connection.transport.tcp.tcp",
        "pyrogram.session",
        "pyrogram.session.session",
        "pyrogram.dispatcher",
        "pyrogram.syncer",
        "pyrogram.crypto",
    )
    quiet_warning_loggers=(
        "httpx",
        "httpcore",
        "telegram",
        "telegram.ext",
        "telegram.request",
        "telegram.vendor",
        "apscheduler",
        "telethon",
        "telethon.network",
        "telethon.network.mtprotosender",
        "telethon.network.connection",
        "telethon.client",
        "telethon.client.uploads",
    )
    for name in quiet_error_loggers:
        logging.getLogger(name).setLevel(logging.ERROR)
    for name in quiet_warning_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)

log=logging.getLogger(__name__)

def _local_bot_api_available(host:str,port:int,timeout:float=1.0)->bool:
    try:
        with socket.create_connection((host,port),timeout=timeout):
            return True
    except OSError as e:
        log.debug("Local Telegram Bot API check failed | host=%s port=%s err=%r",host,port,e)
        return False

async def post_init(app):
    global BOT_USERNAME
    try:
        await start_welcome_server(app.bot)
    except Exception:
        log.exception("Welcome Failed")
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        log.exception("Failed to clear webhook/pending updates")
    try:
        me=await app.bot.get_me()
        BOT_USERNAME=(me.username or "").lower()
        if BOT_USERNAME:
            log.info("✓ Bot username loaded: @%s",BOT_USERNAME)
        else:
            log.info("✓ Bot username loaded")
    except Exception:
        log.exception("Failed to get bot username")
    try:
        await warmup_mtproto_uploader(app)
    except Exception:
        log.exception("Failed to warmup MTProto uploader")
    try:
        await warmup_pyrogram_uploader(app)
    except Exception:
        log.exception("Failed to warmup Pyrogram uploader")
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        log.info("✓ Bot commands set")
    except Exception:
        log.exception("Failed to set bot commands")
    try:
        cmds=await app.bot.get_my_commands()
        app.bot_data["commands"]=cmds
        log.info("✓ Cached bot commands: %s",", ".join(c.command for c in cmds))
    except Exception:
        log.exception("Failed to cache bot commands")
    await startup_tasks(app)
    log.info("✓ Startup tasks executed")

async def post_shutdown(app):
    try:
        await shutdown_mtproto_uploader(app)
    except Exception:
        log.exception("Failed to shutdown MTProto uploader")
    try:
        await shutdown_pyrogram_uploader(app)
    except Exception:
        log.exception("Failed to shutdown Pyrogram uploader")
    await close_http_session()
    log.info("HTTP session closed")

def _build_application():
    builder=(
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .concurrent_updates(True)
        .connection_pool_size(512)
        .connect_timeout(30)
        .read_timeout(60*20)
        .write_timeout(60*20)
        .pool_timeout(60*10)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if PREFER_LOCAL_BOT_API and _local_bot_api_available(LOCAL_BOT_API_HOST,LOCAL_BOT_API_PORT):
        base=f"http://{LOCAL_BOT_API_HOST}:{LOCAL_BOT_API_PORT}"
        log.info("✓ Using local Telegram Bot API at %s",base)
        builder=builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
    else:
        if PREFER_LOCAL_BOT_API:
            log.warning("Local Telegram Bot API unavailable, falling back to official Telegram Bot API")
        else:
            log.info("✓ Local Telegram Bot API disabled, using official Telegram Bot API")
    return builder.build()

def main():
    setup_logger()
    log.info("Initializing bot")
    app=_build_application()
    register_commands(app)
    register_messages(app)
    register_callbacks(app)
    banner=r"""
 ／l、
（ﾟ､ ｡ ７   < Nya~ Master! Bot waking up…
  l  ~ヽ       • Loading neko engine
  じしf_, )     • Warming up whiskers
               • Injecting kawaii into memory…
 💖 Ready to serve!
"""
    print(banner)
    log.info("Handlers registered")
    log.info("Polling started")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__=="__main__":
    main()