import os
import uuid
import html
import time
import logging
import re
import json
import subprocess
import asyncio
from telegram import InputMediaPhoto,InputMediaVideo
from telegram.error import RetryAfter
from .constants import TMP_DIR,MAX_TG_SIZE
from .utils import detect_media_type
from .ytdlp import ytdlp_download
from .instagram.main import is_instagram_url,instagram_api_download
from .youtube.main import is_youtube_url
from .facebook.main import is_facebook_url,facebook_download
from .threads.main import is_threads_url,threads_download
from .twitter.main import is_x_url,twitter_download
from .reddit.main import is_reddit_url,reddit_download
from .pinterest.main import is_pinterest_url,pinterest_download
from .remux import video_meta,make_video_thumbnail
from .mtproto_uploader import try_send_video_via_mtproto
from .pyrogram_uploader import try_send_video_via_pyrogram
from .ytdlp import gallerydl_fallback

log=logging.getLogger(__name__)

_ALBUM_CHUNK_SIZE=10
_ALBUM_CHUNK_COOLDOWN=5

_UPLOAD_ENGINE_DEFAULT=os.getenv("UPLOAD_ENGINE","1").strip()
_UPLOAD_ENGINE_STATE_FILE=os.getenv("UPLOAD_ENGINE_STATE_FILE","data/upload_engine.json")
_UPLOAD_ENGINE_CACHE=None
_UPLOAD_ENGINE_NAMES={"0":"telethon","1":"pyrofork"}
_UPLOAD_ENGINE_ALIASES={"0":"0","telethon":"0","mtproto":"0","1":"1","pyrofork":"1","pyrogram":"1","pyro":"1"}

def _normalize_upload_engine(value:str|int|None)->str:
    raw=str(value if value is not None else _UPLOAD_ENGINE_DEFAULT).strip().lower()
    return _UPLOAD_ENGINE_ALIASES.get(raw,_UPLOAD_ENGINE_ALIASES.get(_UPLOAD_ENGINE_DEFAULT.strip().lower(),"1"))

def _load_upload_engine()->str:
    global _UPLOAD_ENGINE_CACHE
    if _UPLOAD_ENGINE_CACHE is not None:
        return _UPLOAD_ENGINE_CACHE
    engine=_normalize_upload_engine(_UPLOAD_ENGINE_DEFAULT)
    try:
        if os.path.exists(_UPLOAD_ENGINE_STATE_FILE):
            with open(_UPLOAD_ENGINE_STATE_FILE,"r",encoding="utf-8") as f:
                data=json.load(f)
            engine=_normalize_upload_engine(data.get("engine",engine))
    except Exception as e:
        log.warning("Failed to load upload engine state | file=%s err=%r",_UPLOAD_ENGINE_STATE_FILE,e)
    _UPLOAD_ENGINE_CACHE=engine
    return engine

def get_upload_engine()->str:
    return _load_upload_engine()

def get_upload_engine_name()->str:
    return _UPLOAD_ENGINE_NAMES.get(get_upload_engine(),"pyrofork")

def set_upload_engine(value:str)->str:
    global _UPLOAD_ENGINE_CACHE
    engine=_normalize_upload_engine(value)
    _UPLOAD_ENGINE_CACHE=engine
    try:
        os.makedirs(os.path.dirname(_UPLOAD_ENGINE_STATE_FILE) or ".",exist_ok=True)
        tmp=f"{_UPLOAD_ENGINE_STATE_FILE}.tmp"
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump({"engine":engine,"name":_UPLOAD_ENGINE_NAMES.get(engine,"pyrofork"),"updated_at":int(time.time())},f)
        os.replace(tmp,_UPLOAD_ENGINE_STATE_FILE)
        log.info("Upload engine state saved | engine=%s name=%s",engine,_UPLOAD_ENGINE_NAMES.get(engine,"pyrofork"))
    except Exception as e:
        log.warning("Failed to save upload engine state | file=%s err=%r",_UPLOAD_ENGINE_STATE_FILE,e)
    return engine

async def _try_send_video_via_upload_engine(bot,chat_id,status_msg_id,file_path,caption,reply_to=None,message_thread_id=None,duration=None,width=None,height=None,thumb_path=None):
    engine=get_upload_engine()
    try:
        if engine=="1":
            return await try_send_video_via_pyrogram(
                bot=bot,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                file_path=file_path,
                caption=caption,
                reply_to=reply_to,
                message_thread_id=message_thread_id,
                duration=duration,
                width=width,
                height=height,
                thumb_path=thumb_path,
            )
        if engine=="0":
            return await try_send_video_via_mtproto(
                bot=bot,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                file_path=file_path,
                caption=caption,
                reply_to=reply_to,
                message_thread_id=message_thread_id,
                duration=duration,
                width=width,
                height=height,
                thumb_path=thumb_path,
            )
        log.warning("Unsupported upload engine ignored | value=%s",engine)
        return False
    except Exception as e:
        log.warning("Upload engine failed, fallback to PTB | engine=%s name=%s file=%s err=%r",engine,_UPLOAD_ENGINE_NAMES.get(engine,"unknown"),os.path.basename(file_path or ""),e)
        return False
        
async def reencode_mp3(src_path:str)->str:
    fixed_path=f"{TMP_DIR}/{uuid.uuid4().hex}.mp3"
    def _run():
        result=subprocess.run(
            ["ffmpeg","-y","-i",src_path,"-vn","-acodec","libmp3lame","-ab","192k","-ar","44100",fixed_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode!=0:
            raise RuntimeError(f"FFmpeg re-encode failed with exit code {result.returncode}")
        if not os.path.exists(fixed_path) or os.path.getsize(fixed_path)<=0:
            raise RuntimeError("FFmpeg re-encode failed")
        return fixed_path
    return await asyncio.to_thread(_run)

async def _ensure_photo_size(file_path: str):
    if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) < 10000000:
        return
    
    log.info("Photo exceeds 10MB limit, compressing | file=%s", os.path.basename(file_path))
    tmp_path = f"{file_path}_compressed.jpg"
    
    def _run():
        cmd = [
            "ffmpeg", "-y", "-i", file_path,
            "-vf", "scale='min(3840,iw)':'min(3840,ih)':force_original_aspect_ratio=decrease",
            "-q:v", "4", tmp_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
    await asyncio.to_thread(_run)
    
    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
        os.replace(tmp_path, file_path)

def _clean_caption_from_path(path:str)->str:
    raw_name=os.path.splitext(os.path.basename(path))[0]
    parts=raw_name.split("_",1)
    if len(parts)==2 and len(parts[0])>=10 and all(c in "0123456789abcdef" for c in parts[0].lower()):
        text=parts[1]
    else:
        text=raw_name
    return text.strip() or "Media"

def _build_safe_caption(title:str,bot_name:str,max_len:int=1024)->str:
    clean_title=(title or "Media").strip()
    safe_bot=html.escape(bot_name or "Bot")
    suffix=f"\n\n🪄 <i>Powered by {safe_bot}</i>"
    prefix="<blockquote expandable>🎬 "
    closing="</blockquote>"
    full=f"{prefix}{html.escape(clean_title)}{closing}{suffix}"
    if len(full)<=max_len:
        return full
    allowed=max_len-len(prefix)-len(closing)-len(suffix)-3
    if allowed<1:
        allowed=1
    short_title=clean_title[:allowed].rstrip()+"..."
    return f"{prefix}{html.escape(short_title)}{closing}{suffix}"

def _build_safe_photo_caption(title:str,bot_name:str,max_len:int=1024)->str:
    clean_title=(title or "Image").strip()
    safe_bot=html.escape(bot_name or "Bot")
    suffix=f"\n\n🪄 <i>Powered by {safe_bot}</i>"
    prefix="<blockquote expandable>🖼️ "
    closing="</blockquote>"
    full=f"{prefix}{html.escape(clean_title)}{closing}{suffix}"
    if len(full)<=max_len:
        return full
    allowed=max_len-len(prefix)-len(closing)-len(suffix)-3
    if allowed<1:
        allowed=1
    short_title=clean_title[:allowed].rstrip()+"..."
    return f"{prefix}{html.escape(short_title)}{closing}{suffix}"

def _is_reply_not_found_error(exc:Exception)->bool:
    text=(str(exc) or "").lower()
    keys=("replied message not found","message to be replied not found","reply message not found","reply_to_message_id")
    return any(k in text for k in keys)

async def _get_bot_name(bot)->str:
    cached=getattr(bot,"_cached_first_name",None)
    if cached:
        return cached
    me=await bot.get_me()
    name=me.first_name or "Bot"
    setattr(bot,"_cached_first_name",name)
    return name

async def _safe_edit_status(bot,chat_id,message_id,text:str):
    try:
        await bot.edit_message_text(chat_id=chat_id,message_id=message_id,text=text,parse_mode="HTML")
    except RetryAfter as e:
        retry_after=max(int(getattr(e,"retry_after",3)),1)
        log.warning("RetryAfter while editing status skipped | chat_id=%s wait=%s",chat_id,retry_after)
    except Exception as e:
        if "message is not modified" in (str(e) or "").lower():
            return
        log.warning("Failed to edit status message | chat_id=%s message_id=%s err=%s",chat_id,message_id,e)

async def _set_uploading_status(bot,chat_id,status_msg_id,kind:str):
    label={
        "audio":"🎵 <b>Uploading audio...</b>",
        "video":"🎬 <b>Uploading video...</b>",
        "photo":"🖼️ <b>Uploading photo...</b>",
        "album":"🖼️ <b>Uploading album...</b>",
    }.get(kind,"<b>Uploading...</b>")
    action={
        "audio":"upload_audio",
        "video":"upload_video",
        "photo":"upload_photo",
        "album":"upload_photo",
    }.get(kind,"typing")
    await _safe_edit_status(bot=bot,chat_id=chat_id,message_id=status_msg_id,text=label)
    try:
        await bot.send_chat_action(chat_id=chat_id,action=action)
    except RetryAfter as e:
        retry_after=max(int(getattr(e,"retry_after",3)),1)
        log.warning("RetryAfter while sending chat action | chat_id=%s wait=%s",chat_id,retry_after)
        await asyncio.sleep(retry_after+1)
    except Exception as e:
        log.warning("Failed to send chat action | chat_id=%s action=%s err=%s",chat_id,action,e)

def _safe_seek(handle,label:str,chat_id):
    if not handle or not hasattr(handle,"seek"):
        return
    try:
        handle.seek(0)
    except Exception as e:
        log.warning("Failed to seek %s file handle | chat_id=%s err=%r",label,chat_id,e)

def _safe_close(handle,label:str,chat_id):
    if not handle:
        return
    try:
        handle.close()
    except Exception as e:
        log.warning("Failed to close %s file handle | chat_id=%s err=%r",label,chat_id,e)

async def _delete_file(path:str|None,label:str):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("Delete %s file %s successfully",label,os.path.basename(path))
    except Exception as e:
        log.warning("Failed to delete %s file | path=%s err=%r",label,path,e)

async def _send_media_group_with_fallback(bot,chat_id,media,reply_to=None,message_thread_id=None):
    while True:
        try:
            started=time.monotonic()
            result=await bot.send_media_group(chat_id=chat_id,media=media,reply_to_message_id=reply_to,message_thread_id=message_thread_id)
            log.info("Telegram send done | chat_id=%s func=send_media_group elapsed=%.2fs",chat_id,time.monotonic()-started)
            return result
        except RetryAfter as e:
            retry_after=max(int(getattr(e,"retry_after",3)),1)
            log.warning("RetryAfter send_media_group | chat_id=%s wait=%s",chat_id,retry_after)
            await asyncio.sleep(retry_after+1)
        except Exception as e:
            if reply_to and _is_reply_not_found_error(e):
                reply_to=None
                continue
            log.exception("Failed to send media group | chat_id=%s",chat_id)
            raise

async def _send_photo_with_fallback(bot,chat_id,photo,caption,reply_to=None,message_thread_id=None):
    kwargs={
        "chat_id":chat_id,
        "photo":photo,
        "caption":caption,
        "parse_mode":"HTML",
        "reply_to_message_id":reply_to,
        "message_thread_id":message_thread_id,
        "disable_notification":True,
    }
    while True:
        try:
            started=time.monotonic()
            result=await bot.send_photo(**kwargs)
            log.info("Telegram send done | chat_id=%s func=send_photo elapsed=%.2fs",chat_id,time.monotonic()-started)
            return result
        except RetryAfter as e:
            retry_after=max(int(getattr(e,"retry_after",3)),1)
            log.warning("RetryAfter send_photo | chat_id=%s wait=%s",chat_id,retry_after)
            await asyncio.sleep(retry_after+1)
        except Exception as e:
            if kwargs.get("reply_to_message_id") and _is_reply_not_found_error(e):
                kwargs.pop("reply_to_message_id",None)
                continue
            log.exception("Failed to send photo | chat_id=%s",chat_id)
            raise

async def _send_video_with_fallback(bot,chat_id,video,caption,reply_to=None,message_thread_id=None,supports_streaming=True,duration=None,width=None,height=None,thumbnail=None):
    kwargs={
        "chat_id":chat_id,
        "video":video,
        "caption":caption,
        "parse_mode":"HTML",
        "supports_streaming":supports_streaming,
        "reply_to_message_id":reply_to,
        "message_thread_id":message_thread_id,
        "disable_notification":True,
    }
    if duration:
        kwargs["duration"]=int(duration)
    if width:
        kwargs["width"]=int(width)
    if height:
        kwargs["height"]=int(height)
    if thumbnail:
        kwargs["thumbnail"]=thumbnail
    while True:
        try:
            started=time.monotonic()
            result=await bot.send_video(**kwargs)
            log.info("Telegram send done | chat_id=%s func=send_video elapsed=%.2fs",chat_id,time.monotonic()-started)
            return result
        except RetryAfter as e:
            retry_after=max(int(getattr(e,"retry_after",3)),1)
            log.warning("RetryAfter send_video | chat_id=%s wait=%s",chat_id,retry_after)
            await asyncio.sleep(retry_after+1)
        except Exception as e:
            if kwargs.get("reply_to_message_id") and _is_reply_not_found_error(e):
                _safe_seek(video,"video",chat_id)
                _safe_seek(thumbnail,"thumbnail",chat_id)
                kwargs.pop("reply_to_message_id",None)
                continue
            log.exception("Failed to send video | chat_id=%s",chat_id)
            raise

async def _send_audio_with_fallback(bot,chat_id,audio,title,performer,filename,reply_to=None,message_thread_id=None):
    kwargs={
        "chat_id":chat_id,
        "audio":audio,
        "title":title,
        "performer":performer,
        "filename":filename,
        "reply_to_message_id":reply_to,
        "message_thread_id":message_thread_id,
        "disable_notification":True,
    }
    while True:
        try:
            started=time.monotonic()
            result=await bot.send_audio(**kwargs)
            log.info("Telegram send done | chat_id=%s func=send_audio elapsed=%.2fs",chat_id,time.monotonic()-started)
            return result
        except RetryAfter as e:
            retry_after=max(int(getattr(e,"retry_after",3)),1)
            log.warning("RetryAfter send_audio | chat_id=%s wait=%s",chat_id,retry_after)
            await asyncio.sleep(retry_after+1)
        except Exception as e:
            if kwargs.get("reply_to_message_id") and _is_reply_not_found_error(e):
                kwargs.pop("reply_to_message_id",None)
                continue
            log.exception("Failed to send audio | chat_id=%s",chat_id)
            raise

async def _cleanup_album_files(items:list[dict]):
    for item in items:
        await _delete_file(item.get("path"),"download")

async def _cleanup_single_file(path:str|None):
    await _delete_file(path,"download")

async def _send_media_group_result(bot,chat_id,reply_to,result:dict,message_thread_id=None):
    items=result.get("items") or []
    if not items:
        raise RuntimeError("Album result kosong")
    title=(result.get("title") or "Media").strip() or "Media"
    bot_name=await _get_bot_name(bot)
    caption=_build_safe_photo_caption(title,bot_name)
    chunks=[items[i:i+_ALBUM_CHUNK_SIZE] for i in range(0,len(items),_ALBUM_CHUNK_SIZE)]
    for idx,chunk in enumerate(chunks):
        media=[]
        handles=[]
        try:
            for i,item in enumerate(chunk):
                file_path=item.get("path")
                media_url=str(item.get("url") or "").strip()
                media_type=str(item.get("type") or "").strip().lower()
                is_first=idx==0 and i==0
                item_caption=caption if is_first else None
                item_parse_mode="HTML" if is_first else None
                if file_path and os.path.exists(file_path):
                    detected=detect_media_type(file_path)
                    if detected == "photo":
                        await _ensure_photo_size(file_path)
                    fh=open(file_path,"rb")
                    handles.append((fh,os.path.basename(file_path)))
                    if detected=="video":
                        media.append(InputMediaVideo(media=fh,caption=item_caption,parse_mode=item_parse_mode,supports_streaming=True))
                    else:
                        media.append(InputMediaPhoto(media=fh,caption=item_caption,parse_mode=item_parse_mode))
                    continue
                if media_url:
                    if media_type=="video":
                        media.append(InputMediaVideo(media=media_url,caption=item_caption,parse_mode=item_parse_mode,supports_streaming=True))
                    else:
                        media.append(InputMediaPhoto(media=media_url,caption=item_caption,parse_mode=item_parse_mode))
                    continue
                log.warning("Skipping media group item because file/url is missing | chat_id=%s item=%s",chat_id,item)
            if not media:
                log.warning("No valid media items to send in chunk | chat_id=%s chunk_index=%s",chat_id,idx)
                continue
            await _send_media_group_with_fallback(bot=bot,chat_id=chat_id,media=media,reply_to=reply_to if idx==0 else None,message_thread_id=message_thread_id)
            if idx<len(chunks)-1 and _ALBUM_CHUNK_COOLDOWN>0:
                await asyncio.sleep(_ALBUM_CHUNK_COOLDOWN)
        finally:
            for fh,name in handles:
                _safe_close(fh,f"album media {name}",chat_id)

async def send_downloaded_media(bot,chat_id,reply_to,status_msg_id,path,fmt_key,message_thread_id=None):
    if isinstance(path,dict) and path.get("items"):
        items=path.get("items") or []
        first=items[0] if items else {}
        first_path=first.get("path")
        first_type=str(first.get("type") or "").strip().lower()
        if first_path and os.path.exists(first_path):
            first_type=detect_media_type(first_path)
        await _set_uploading_status(bot,chat_id,status_msg_id,"album" if len(items)>1 else ("video" if first_type=="video" else "photo"))
        try:
            await _send_media_group_result(bot=bot,chat_id=chat_id,reply_to=reply_to,result=path,message_thread_id=message_thread_id)
        finally:
            await _cleanup_album_files(items)
        return

    meta=path if isinstance(path,dict) else {"path":path,"title":None}
    file_path=meta.get("path")
    original_title=(meta.get("title") or "").strip()
    if not file_path or not os.path.exists(file_path):
        raise RuntimeError("Download gagal")
    if os.path.getsize(file_path)>MAX_TG_SIZE:
        raise RuntimeError("File exceeds 2GB. Please choose a lower resolution.")
    bot_name=await _get_bot_name(bot)
    caption_text=original_title or _clean_caption_from_path(file_path)
    media_type=detect_media_type(file_path)
    fixed_audio=None
    try:
        if fmt_key=="mp3":
            await _set_uploading_status(bot,chat_id,status_msg_id,"audio")
            fixed_audio=await reencode_mp3(file_path)
            await _send_audio_with_fallback(bot=bot,chat_id=chat_id,audio=fixed_audio,title=caption_text[:64],performer=bot_name,filename=f"{caption_text[:50]}.mp3",reply_to=reply_to,message_thread_id=message_thread_id)
            return
        if media_type=="photo":
            await _set_uploading_status(bot,chat_id,status_msg_id,"photo")
            await _ensure_photo_size(file_path)
            await _send_photo_with_fallback(bot=bot,chat_id=chat_id,photo=file_path,caption=_build_safe_photo_caption(caption_text,bot_name),reply_to=reply_to,message_thread_id=message_thread_id)
            return
        if media_type=="video":
            await _set_uploading_status(bot,chat_id,status_msg_id,"video")
            thumb_path=None
            video_fh=None
            thumb_fh=None
            try:
                meta_video=await asyncio.to_thread(video_meta,file_path)
                thumb_path=await asyncio.to_thread(make_video_thumbnail,file_path)
                caption=_build_safe_caption(caption_text,bot_name)
                sent=await _try_send_video_via_upload_engine(
                    bot=bot,
                    chat_id=chat_id,
                    status_msg_id=status_msg_id,
                    file_path=file_path,
                    caption=caption,
                    reply_to=reply_to,
                    message_thread_id=message_thread_id,
                    duration=meta_video.get("duration"),
                    width=meta_video.get("width"),
                    height=meta_video.get("height"),
                    thumb_path=thumb_path,
                )
                if sent:
                    return
                video_fh=open(file_path,"rb")
                thumb_fh=open(thumb_path,"rb") if thumb_path and os.path.exists(thumb_path) else None
                await _send_video_with_fallback(
                    bot=bot,
                    chat_id=chat_id,
                    video=video_fh,
                    caption=caption,
                    reply_to=reply_to,
                    message_thread_id=message_thread_id,
                    supports_streaming=True,
                    duration=meta_video.get("duration"),
                    width=meta_video.get("width"),
                    height=meta_video.get("height"),
                    thumbnail=thumb_fh,
                )
            finally:
                _safe_close(video_fh,"video",chat_id)
                _safe_close(thumb_fh,"thumbnail",chat_id)
                await _delete_file(thumb_path,"thumbnail")
            return
        raise RuntimeError("Media tidak didukung")
    finally:
        if fixed_audio:
            await _delete_file(fixed_audio,"temp audio")
        await _cleanup_single_file(file_path)

async def download_non_tiktok(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id:str|None,has_audio:bool,engine:str|None=None,metadata_ready:bool=False):
    if is_instagram_url(raw_url):
        try:
            return await instagram_api_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,metadata_ready=metadata_ready)
        except Exception as e:
            log.warning("Instagram API download failed, falling back to yt-dlp | url=%s err=%r",raw_url,e)
    if is_pinterest_url(raw_url):
        return await pinterest_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
    if is_facebook_url(raw_url):
        return await facebook_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
    if is_reddit_url(raw_url):
        return await reddit_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
    if is_x_url(raw_url):
        return await twitter_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
    if is_threads_url(raw_url):
        return await threads_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
    if re.search(r'(?:pixiv\.net|pixiv\.me)', raw_url, re.I):
        log.info("Pixiv URL detected, routing directly to gallery-dl | url=%s", raw_url)
        try:
            job_id = uuid.uuid4().hex[:10]
            return await gallerydl_fallback(
                url=raw_url,
                job_id=job_id,
                bot=bot,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                status_text="<b>Downloading from Pixiv...</b>"
            )
        except Exception as e:
            raise RuntimeError(f"Gallery-dl Pixiv download failed: {e}")
    if is_youtube_url(raw_url):
        if (engine or "").strip().lower() not in ("","ytdlp"):
            log.warning("Unsupported YouTube engine ignored | url=%s engine=%s",raw_url,engine)
        result=await ytdlp_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio)
        file_path=result.get("path") if isinstance(result,dict) else result
        if not file_path:
            raise RuntimeError("yt-dlp returned no file")
        return result

    return await ytdlp_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio)
