import os,re,uuid,html,shutil,asyncio,logging
from telegram import Update
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from handlers.dl.proc import spawn_process, PIPE

log=logging.getLogger(__name__)
TMP_DIR=os.getenv("TMP_DIR","downloads")
GETSTICKER_TIMEOUT=int(os.getenv("GETSTICKER_TIMEOUT","90"))

def _safe_name(name:str,default:str="sticker")->str:
    name=os.path.basename(str(name or "").strip()) or default
    name=re.sub(r"[^a-zA-Z0-9._-]+","_",name)
    return name[:120] or default

def _help_text()->str:
    return "<b>Get Sticker</b>\n\n<code>/getsticker</code> reply to a sticker"

async def _run_cmd(cmd:list[str],timeout:int=GETSTICKER_TIMEOUT):
    proc=await spawn_process(*cmd,stdout=PIPE,stderr=PIPE)
    try:
        stdout,stderr=await asyncio.wait_for(proc.communicate(),timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(f"Process timeout after {timeout}s")
    if proc.returncode!=0:
        err=(stderr.decode(errors="ignore") or stdout.decode(errors="ignore") or f"Process exited with code {proc.returncode}").strip()
        raise RuntimeError(err[-1000:])

async def _download_sticker(bot,sticker)->str:
    os.makedirs(TMP_DIR,exist_ok=True)
    ext=".webm" if sticker.is_video else ".tgs" if sticker.is_animated else ".webp"
    path=os.path.join(TMP_DIR,f"sticker_{uuid.uuid4().hex}{ext}")
    tg_file=await bot.get_file(sticker.file_id)
    await tg_file.download_to_drive(path)
    if not os.path.exists(path) or os.path.getsize(path)<=0:
        raise RuntimeError("Failed to download sticker from Telegram.")
    return path

async def _webp_to_png(src:str)->str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required to convert sticker to PNG.")
    out=os.path.join(TMP_DIR,f"sticker_{uuid.uuid4().hex}.png")
    await _run_cmd(["ffmpeg","-y","-i",src,"-frames:v","1",out])
    if not os.path.exists(out) or os.path.getsize(out)<=0:
        raise RuntimeError("Failed to convert sticker to PNG.")
    return out

async def _tgs_to_webm(src:str)->str:
    converter=shutil.which("lottie_convert.py") or shutil.which("lottie_convert")
    if not converter:
        raise RuntimeError("Animated sticker conversion requires python-lottie. Install it with: pip install lottie")
    out=os.path.join(TMP_DIR,f"sticker_{uuid.uuid4().hex}.webm")
    await _run_cmd([converter,src,out])
    if not os.path.exists(out) or os.path.getsize(out)<=0:
        raise RuntimeError("Failed to convert animated sticker to WEBM.")
    return out

async def getsticker_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    msg=update.effective_message
    if not msg:
        return
    if not msg.reply_to_message or not msg.reply_to_message.sticker:
        return await msg.reply_text(_help_text(),parse_mode="HTML",reply_to_message_id=msg.message_id)
    sticker=msg.reply_to_message.sticker
    input_path=None
    output_path=None
    status=None
    try:
        status=await msg.reply_text("<b>Getting sticker...</b>",parse_mode="HTML",reply_to_message_id=msg.message_id)
        input_path=await _download_sticker(context.bot,sticker)
        if sticker.is_video:
            output_path=input_path
            filename="sticker.webm"
        elif sticker.is_animated:
            output_path=await _tgs_to_webm(input_path)
            filename="sticker.webm"
        else:
            output_path=await _webp_to_png(input_path)
            filename="sticker.png"
        with open(output_path,"rb") as f:
            await msg.reply_document(document=f,filename=filename,reply_to_message_id=msg.reply_to_message.message_id)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        err=html.escape(str(e) or repr(e))[:3500]
        if status:
            try:
                await status.edit_text(f"<b>Get sticker failed</b>\n\n<code>{err}</code>",parse_mode="HTML")
            except Exception:
                pass
        else:
            await msg.reply_text(f"<b>Get sticker failed</b>\n\n<code>{err}</code>",parse_mode="HTML")
    finally:
        for path in (input_path,output_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    log.info("GetSticker temp deleted | file=%s",path)
            except Exception as e:
                log.warning("Failed to delete getsticker temp | file=%s err=%r",path,e)