import os
import uuid
import time
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes
from handlers.dl.service import _try_send_video_via_upload_engine
from handlers.dl.constants import TMP_DIR

log = logging.getLogger(__name__)

ACTIVE_RECORDINGS = {}

async def recordlive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    msg = update.effective_message
    if not msg:
        return

    args = context.args
    if not args:
        await msg.reply_text(
            "Invalid format.\nUse: `/recordlive <@tiktok_username> [duration_minutes]`\nExample: `/recordlive @jkt48.freya 10`",
            parse_mode="Markdown"
        )
        return

    url_or_user = args[0]
    if not url_or_user.startswith("http"):
        target_username = url_or_user.lstrip("@")
        url = f"https://www.tiktok.com/@{target_username}/live"
    else:
        url = url_or_user
        target_username = url.split("tiktok.com/@")[-1].split("/")[0] if "@" in url else "Tiktok_Live"

    duration_mins = 30
    if len(args) > 1 and args[1].isdigit():
        duration_mins = int(args[1])
        if duration_mins > 30:
            duration_mins = 30
        elif duration_mins < 1:
            duration_mins = 1

    if user_id in ACTIVE_RECORDINGS:
        await msg.reply_text("You already have an active Live recording in progress! Please wait until it finishes or use `/stoprecord` first.", parse_mode="Markdown")
        return

    duration_secs = duration_mins * 60
    out_name = f"tiktok_live_{uuid.uuid4().hex[:8]}.mp4"
    out_path = os.path.join(TMP_DIR, out_name)

    status_msg = await msg.reply_text(
        f"**Starting Live Recording**\n\n"
        f"Target: `@{target_username}`\n"
        f"Duration: {duration_mins} Minutes\n\n"
        f"Please wait, the system is recording...",
        parse_mode="Markdown"
    )

    cmd = [
        "yt-dlp",
        "--no-live-from-start",
        "--no-part",
        "--downloader", "ffmpeg",
        "--downloader-args", f"ffmpeg:-t {duration_secs}",
        "-o", out_path,
        url
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
    except Exception as e:
        await status_msg.edit_text(f"Failed to start recording: {e}")
        return

    ACTIVE_RECORDINGS[user_id] = {
        "proc": proc,
        "out_path": out_path,
        "status_msg_id": status_msg.message_id,
        "url": url,
        "target": target_username,
        "chat_id": chat_id,
        "user_id": user_id,
        "bot": context.bot,
        "start_time": time.time(),
        "duration_mins": duration_mins
    }

    context.application.create_task(_wait_and_upload(user_id))

async def _wait_and_upload(user_id):
    record_data = ACTIVE_RECORDINGS.get(user_id)
    if not record_data:
        return
    
    proc = record_data["proc"]
    out_path = record_data["out_path"]
    status_msg_id = record_data["status_msg_id"]
    bot = record_data["bot"]
    url = record_data["url"]
    chat_id = record_data["chat_id"]

    await proc.wait()
    ACTIVE_RECORDINGS.pop(user_id, None)

    if not os.path.exists(out_path) or os.path.getsize(out_path) < 10000:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, 
                message_id=status_msg_id, 
                text=f"Recording failed.\nThe creator is not currently Live, has restricted viewer access, or the recording duration was too short.\n🔗 Target: {url}"
            )
        except Exception:
            pass
        if os.path.exists(out_path):
            os.remove(out_path)
        return

    try:
        await bot.edit_message_text(
            chat_id=chat_id, 
            message_id=status_msg_id, 
            text="*Preparing video*", 
            parse_mode="Markdown"
        )
        from handlers.dl.remux import remux_video_for_telegram
        out_path = await asyncio.to_thread(remux_video_for_telegram, out_path)
    except Exception as e:
        log.warning(f"Live remux failed: {e}")

    file_size_mb = os.path.getsize(out_path) / (1024 * 1024)
    try:
        await bot.edit_message_text(
            chat_id=chat_id, 
            message_id=status_msg_id, 
            text=f"**Recording Complete!**\n\nFile Size: {file_size_mb:.1f} MB\nUploading...", 
            parse_mode="Markdown"
        )
    except Exception:
        pass

    caption = f"🎥 <b>Live Record</b>\n🔗 {url}\n📏 Size: {file_size_mb:.1f} MB"
    
    try:
        success = await _try_send_video_via_upload_engine(
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            file_path=out_path,
            caption=caption
        )
        if not success:
            with open(out_path, "rb") as f:
                await bot.send_video(chat_id=chat_id, video=f, caption=caption, parse_mode="HTML", write_timeout=300)
        
        try:
            await bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception: 
            pass
    except Exception as e:
        log.warning(f"Failed to upload live record: {e}")
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=f"❌ Failed to send video to Telegram: {e}")
        except Exception: 
            pass
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)
            log.info(f"Cleaned up live record file: {out_path}")

async def stoprecord_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.effective_message

    record_data = ACTIVE_RECORDINGS.get(user_id)
    if not record_data:
        await msg.reply_text("You do not have any active Live recording in progress.")
        return

    proc = record_data["proc"]
    try:
        proc.terminate()
        await msg.reply_text("Recording has been forcefully stopped! The video will now be processed (Remux) and uploaded.")
    except Exception as e:
        await msg.reply_text(f"Failed to stop recording: {e}")

async def statusrecord_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    
    if not ACTIVE_RECORDINGS:
        await msg.reply_text("There are currently no active TikTok Live recordings running on the *server*.", parse_mode="Markdown")
        return

    text = "📡 **TikTok Live Recording Dashboard**\n\n"
    for uid, data in ACTIVE_RECORDINGS.items():
        elapsed_secs = int(time.time() - data["start_time"])
        elapsed_mins = elapsed_secs // 60
        target = data["target"]
        max_dur = data["duration_mins"]
        
        try:
            user = await context.bot.get_chat(uid)
            requester = f"@{user.username}" if user.username else user.first_name
        except:
            requester = str(uid)
            
        text += f"▪️ **Target**: `@{target}`\n"
        text += f"   Requested by: {requester}\n"
        text += f"   Elapsed Time: {elapsed_mins} min / {max_dur} min\n\n"

    await msg.reply_text(text, parse_mode="Markdown")