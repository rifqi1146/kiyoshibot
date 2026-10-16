
import os
import shutil
import signal
import subprocess
import sys
import time
from dotenv import load_dotenv

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

TGDATA_DIR = "tgdata"
os.makedirs(TGDATA_DIR, exist_ok=True)

BOT_API_BIN = shutil.which("telegram-bot-api")
USE_LOCAL_BOT_API = bool(BOT_API_BIN and API_ID and API_HASH)

BOT_API_CMD = []
if USE_LOCAL_BOT_API:
    BOT_API_CMD = [
        BOT_API_BIN,
        f"--api-id={API_ID}",
        f"--api-hash={API_HASH}",
        "--local",
        "--http-port=8081",
        f"--dir={TGDATA_DIR}",
    ]

BOT_CMD = [sys.executable, "bot.py"]

bot_api_proc = None
bot_proc = None


def _terminate_proc(proc, name: str):
    if not proc:
        return

    if proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    except Exception as e:
        print(f"[!] Failed to stop {name}: {e}")


def shutdown(signum=None, frame=None):
    print("\n[!] Shutting down...")
    _terminate_proc(bot_proc, "bot.py")
    _terminate_proc(bot_api_proc, "telegram-bot-api")
    sys.exit(0)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


if USE_LOCAL_BOT_API:
    print("[+] Starting Telegram Bot API...")
    bot_api_proc = subprocess.Popen(BOT_API_CMD)
    time.sleep(2)
else:
    if not BOT_API_BIN:
        print("[!] telegram-bot-api binary not found, using official Telegram Bot API")
    elif not API_ID or not API_HASH:
        print("[!] API_ID/API_HASH not set, using official Telegram Bot API")


print("[+] Starting bot.py...")
bot_proc = subprocess.Popen(BOT_CMD)

try:
    bot_exit = bot_proc.wait()
    if bot_exit not in (0, None):
        print(f"[!] bot.py exited with code {bot_exit}")
finally:
    _terminate_proc(bot_api_proc, "telegram-bot-api")