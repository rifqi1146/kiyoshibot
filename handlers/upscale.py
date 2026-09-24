import os,re,html,uuid,json,shutil,inspect,mimetypes,asyncio,logging,subprocess,aiohttp
from telegram import Update
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from utils.http import get_http_session

log=logging.getLogger(__name__)

TMP_DIR=os.getenv("TMP_DIR","downloads")
NEOXR_UPSCALE_API=os.getenv("NEOXR_UPSCALE_API","https://api.neoxr.eu/api/upscale").strip()
NEOXR_REMINI_API=os.getenv("NEOXR_REMINI_API","https://api.neoxr.eu/api/remini").strip()
TMPFILES_UPLOAD_API=os.getenv("TMPFILES_UPLOAD_API","https://tmpfiles.org/api/v1/upload").strip()
UPSCALE_MAX_SIZE=int(os.getenv("UPSCALE_MAX_SIZE",str(10*1024*1024)))
UPSCALE_TIMEOUT=int(os.getenv("UPSCALE_TIMEOUT","120"))

async def _shared_http_session():
    session=get_http_session()
    if inspect.isawaitable(session):
        session=await session
    return session

def _safe_name(name:str,default:str="image.jpg")->str:
    name=os.path.basename(str(name or "").strip()) or default
    name=re.sub(r"[^a-zA-Z0-9._-]+","_",name)
    return name[:120] or default

def _is_image_mime(mime:str)->bool:
    return str(mime or "").lower().startswith("image/")

def _guess_content_type(path:str)->str:
    mime,_=mimetypes.guess_type(path)
    mime=str(mime or "").lower()
    if mime.startswith("image/"):
        return mime
    ext=os.path.splitext(path)[1].lower()
    if ext in (".jpg",".jpeg"):
        return "image/jpeg"
    if ext==".png":
        return "image/png"
    if ext==".webp":
        return "image/webp"
    if ext==".gif":
        return "image/gif"
    return "image/jpeg"

def _guess_ext(mime:str,fallback:str=".jpg")->str:
    mime=str(mime or "").lower()
    if "png" in mime:
        return ".png"
    if "webp" in mime:
        return ".webp"
    if "jpeg" in mime or "jpg" in mime:
        return ".jpg"
    return fallback

def _tmpfiles_direct_url(url:str)->str:
    url=str(url or "").strip()
    if url.startswith("http://tmpfiles.org/"):
        url="https://"+url[len("http://"):]
    if url.startswith("https://tmpfiles.org/") and "/dl/" not in url:
        return url.replace("https://tmpfiles.org/","https://tmpfiles.org/dl/",1)
    return url

def _help_text()->str:
    return (
        "<b>Image Upscale</b>\n\n"
        "<code>/upscale</code> default mode\n"
        "<code>/upscale remini</code> upscale image with Remini"
    )

def _parse_mode(args:list[str])->str:
    if not args:
        return "upscale"
    mode=str(args[0]).lower().strip()
    if mode in ("remini","enhance","hd"):
        return "remini"
    if mode in ("upscale","up","2x"):
        return "upscale"
    return "help"

async def _download_replied_media(bot,msg)->tuple[str,str]:
    target=msg.reply_to_message
    if not target:
        raise RuntimeError("NO_REPLY")
    os.makedirs(TMP_DIR,exist_ok=True)
    if target.photo:
        photo=target.photo[-1]
        tg_file=await bot.get_file(photo.file_id)
        filename=f"upscale_{uuid.uuid4().hex}.jpg"
    elif target.document:
        doc=target.document
        if not _is_image_mime(doc.mime_type):
            raise RuntimeError("The replied document is not an image.")
        if doc.file_size and doc.file_size>UPSCALE_MAX_SIZE:
            raise RuntimeError(f"Image is too large. Max size is {UPSCALE_MAX_SIZE//1024//1024}MB.")
        tg_file=await bot.get_file(doc.file_id)
        ext=os.path.splitext(doc.file_name or "")[1] or _guess_ext(doc.mime_type)
        filename=f"upscale_{uuid.uuid4().hex}{ext}"
    elif target.sticker:
        sticker=target.sticker
        if sticker.is_animated or sticker.is_video:
            raise RuntimeError("Animated/video stickers are not supported. Use a static sticker.")
        tg_file=await bot.get_file(sticker.file_id)
        filename=f"upscale_{uuid.uuid4().hex}.webp"
    else:
        raise RuntimeError("NO_REPLY")
    input_path=os.path.join(TMP_DIR,_safe_name(filename))
    await tg_file.download_to_drive(input_path)
    if not os.path.exists(input_path) or os.path.getsize(input_path)<=0:
        raise RuntimeError("Failed to download image from Telegram.")
    if os.path.getsize(input_path)>UPSCALE_MAX_SIZE:
        raise RuntimeError(f"Image is too large. Max size is {UPSCALE_MAX_SIZE//1024//1024}MB.")
    return input_path,filename

def _convert_image_to_jpg(src_path:str)->str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required to convert image before upload.")
    out_path=os.path.join(TMP_DIR,f"upscale_{uuid.uuid4().hex}.jpg")
    result=subprocess.run(["ffmpeg","-y","-i",src_path,"-frames:v","1","-q:v","2",out_path],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if result.returncode!=0 or not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
        raise RuntimeError("Failed to convert image to JPG.")
    return out_path

async def _upload_to_tmpfiles(path:str)->str:
    if not os.path.exists(path):
        raise RuntimeError("Upload file does not exist.")
    if os.path.getsize(path)<=0:
        raise RuntimeError("Upload file is empty.")
    content_type=_guess_content_type(path)
    filename=_safe_name(os.path.basename(path),"image.jpg")
    session=await _shared_http_session()
    log.info("Tmpfiles upload start | file=%s size=%s content_type=%s",path,os.path.getsize(path),content_type)
    with open(path,"rb") as fh:
        form=aiohttp.FormData()
        form.add_field("file",fh,filename=filename,content_type=content_type)
        async with session.post(TMPFILES_UPLOAD_API,data=form,timeout=aiohttp.ClientTimeout(total=UPSCALE_TIMEOUT)) as resp:
            text=(await resp.text()).strip()
            log.info("Tmpfiles upload response | status=%s body=%s",resp.status,text[:800])
            if resp.status!=200:
                raise RuntimeError(f"Tmpfiles upload failed {resp.status}: {text[:500]}")
            try:
                data=json.loads(text)
            except Exception:
                data=None
    raw_url=""
    if isinstance(data,dict):
        raw_url=str(((data.get("data") or {}).get("url")) or data.get("url") or "").strip()
    if not raw_url:
        m=re.search(r"https?://tmpfiles\.org/[^\s\"'<>]+",text)
        raw_url=m.group(0).strip() if m else ""
    direct_url=_tmpfiles_direct_url(raw_url)
    if not direct_url.startswith(("http://","https://")):
        raise RuntimeError(f"Invalid Tmpfiles response: {text[:500] or 'empty response'}")
    log.info("Tmpfiles upload success | url=%s direct=%s",raw_url,direct_url)
    return direct_url

async def _call_neoxr_image_api(image_url:str,mode:str)->dict:
    api_key=os.getenv("NEOXR_API_KEY","").strip()
    if not api_key:
        raise RuntimeError("NEOXR_API_KEY is not set.")
    url=NEOXR_REMINI_API if mode=="remini" else NEOXR_UPSCALE_API
    session=await _shared_http_session()
    params={"image":image_url,"apikey":api_key}
    async with session.get(url,params=params,timeout=aiohttp.ClientTimeout(total=UPSCALE_TIMEOUT)) as resp:
        text=await resp.text()
        if resp.status!=200:
            raise RuntimeError(f"Neoxr API error {resp.status}: {text[:500]}")
        try:
            data=await resp.json(content_type=None)
        except Exception:
            raise RuntimeError(f"Invalid Neoxr JSON: {text[:500]}")
    if not isinstance(data,dict):
        raise RuntimeError("Invalid Neoxr response.")
    if not data.get("status"):
        raise RuntimeError(data.get("message") or data.get("msg") or f"{mode.title()} failed.")
    result=data.get("data") or data.get("result") or {}
    if isinstance(result,str):
        result={"url":result}
    if not isinstance(result,dict):
        raise RuntimeError(f"Invalid {mode} result.")
    return result

def _pick_result_url(result:dict)->str:
    for key in ("downloadUrl","download_url","url","image","result","output"):
        value=str(result.get(key) or "").strip()
        if value.startswith(("http://","https://")):
            return value
    raise RuntimeError("API result has no download URL.")

async def _download_with_aria2c(url:str,out_path:str):
    aria2c=shutil.which("aria2c")
    if not aria2c:
        raise RuntimeError("aria2c is not installed or not found in PATH.")
    cmd=[
        aria2c,"--allow-overwrite=true","--auto-file-renaming=false","--summary-interval=0",
        "--console-log-level=warn","-x","8","-s","8","-k","1M",
        "-o",os.path.basename(out_path),"-d",os.path.dirname(out_path),url
    ]
    log.info("Upscale aria2c download start | out=%s",out_path)
    proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        stdout,stderr=await asyncio.wait_for(proc.communicate(),timeout=UPSCALE_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(f"aria2c timeout after {UPSCALE_TIMEOUT}s")
    stdout_text=stdout.decode(errors="ignore") if stdout else ""
    stderr_text=stderr.decode(errors="ignore") if stderr else ""
    if proc.returncode!=0:
        err=(stderr_text or stdout_text or f"aria2c exited with code {proc.returncode}").strip()
        raise RuntimeError(err[-1000:])
    if not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
        raise RuntimeError("Failed to download result file.")

async def upscale_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    msg=update.effective_message
    if not msg:
        return
    args=context.args or []
    mode=_parse_mode(args)
    if mode=="help" or not msg.reply_to_message:
        return await msg.reply_text(_help_text(),parse_mode="HTML",reply_to_message_id=msg.message_id)
    input_path=None
    converted_path=None
    output_path=None
    status=None
    title="Remini" if mode=="remini" else "Upscale"
    try:
        status=await msg.reply_text(f"<b>{html.escape(title)} image...</b>\n\nPlease wait.",reply_to_message_id=msg.message_id,parse_mode="HTML")
        input_path,filename=await _download_replied_media(context.bot,msg)
        converted_path=await asyncio.to_thread(_convert_image_to_jpg,input_path)
        image_url=await _upload_to_tmpfiles(converted_path)
        result=await _call_neoxr_image_api(image_url,mode)
        download_url=_pick_result_url(result)
        original=_safe_name(result.get("filename") or result.get("original") or filename)
        ext=os.path.splitext(original)[1] or ".jpg"
        output_path=os.path.join(TMP_DIR,f"{mode}_{uuid.uuid4().hex}{ext}")
        await _download_with_aria2c(download_url,output_path)
        with open(output_path,"rb") as f:
            await msg.reply_document(document=f,filename="upscale.png",caption="<b>Upscale Result</b>",parse_mode="HTML",reply_to_message_id=msg.reply_to_message.message_id)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        err_raw=str(e) or repr(e)
        if err_raw=="NO_REPLY":
            err_raw="Reply to an image, image document, or static sticker first."
        err=html.escape(err_raw.strip())[:3500]
        if status:
            try:
                await status.edit_text(f"<b>{html.escape(title)} failed</b>\n\n<code>{err}</code>",parse_mode="HTML")
            except Exception:
                pass
        else:
            try:
                await msg.reply_text(f"<b>{html.escape(title)} failed</b>\n\n<code>{err}</code>",parse_mode="HTML")
            except Exception:
                pass
    finally:
        for path in (input_path,converted_path,output_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    log.info("Upscale temp deleted | file=%s",path)
            except Exception as e:
                log.warning("Failed to delete upscale temp | file=%s err=%r",path,e)