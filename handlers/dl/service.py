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
from .utils import detect_media_type,FileSizeLimitExceeded
from .stages import stage
from .ytdlp import ytdlp_download
from .instagram.main import is_instagram_url,instagram_api_download
from .youtube.main import is_youtube_url, is_youtube_post_url, download_youtube_post
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

_BOT_FIRST_NAME_CACHE=None

_ALBUM_CHUNK_SIZE=10
_ALBUM_CHUNK_COOLDOWN=5

_UPLOAD_ENGINE_DEFAULT=os.getenv("UPLOAD_ENGINE","pyrofork").strip()
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

import threading
_engine_lock = threading.Lock()

def get_upload_engine_name()->str:
    return _UPLOAD_ENGINE_NAMES.get(get_upload_engine(),"pyrofork")

def set_upload_engine(value:str)->str:
    global _UPLOAD_ENGINE_CACHE
    engine=_normalize_upload_engine(value)
    with _engine_lock:
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
    if not status_msg_id:
        log.debug("Silent mode detected, using custom upload engine without progress updates")
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

        
async def reencode_mp3(src_path:str, cover_path:str|None=None, title:str="", artist:str="")->str:
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
        _embed_audio_metadata(fixed_path, cover_path, title, artist)
        return fixed_path
    return await asyncio.to_thread(_run)


def _embed_audio_metadata(path:str, cover_path:str|None, title:str, artist:str):
    """Tulis tag ID3v2 (judul, artis) + cover art APIC ke file MP3.

    Pakai mutagen (in-process, tanpa spawn ffmpeg ekstra). Gagal sekalipun
    tidak merusak audio, cukup log warning.
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, error as id3_error
    except Exception as e:
        log.debug("mutagen unavailable, skipping ID3 embed | err=%r", e)
        return
    try:
        audio = MP3(path, ID3=ID3)
        try:
            audio.add_tags()
        except id3_error:
            pass
        if title:
            audio.tags.add(TIT2(encoding=3, text=[title[:120]]))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=[artist[:80]]))
        if cover_path and os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
            # Telegram butuh thumbnail persegi <200KB. Cover besar di-ekstrak
            # jadi JPG 320px via ffmpeg agar aman dan kecil.
            thumb_out = f"{TMP_DIR}/{uuid.uuid4().hex}_cover.jpg"
            try:
                subprocess.run(
                    ["ffmpeg","-y","-i",cover_path,"-vf","scale='min(320,iw)':-2","-q:v","4",thumb_out],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                )
                data = None
                if os.path.exists(thumb_out) and os.path.getsize(thumb_out) > 0:
                    with open(thumb_out, "rb") as fh:
                        data = fh.read()
                if data:
                    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=data))
            finally:
                _delete_file_silent(thumb_out)
        audio.save(v2_version=3)
        log.info("MP3 metadata embedded | file=%s title=%r artist=%r cover=%s",
                 os.path.basename(path), title, artist, bool(cover_path))
    except Exception as e:
        log.warning("Failed to embed MP3 metadata | file=%s err=%r", os.path.basename(path), e)


def _delete_file_silent(path:str|None):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

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
        await asyncio.to_thread(os.replace, tmp_path, file_path)

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
    global _BOT_FIRST_NAME_CACHE

    if _BOT_FIRST_NAME_CACHE:
        return _BOT_FIRST_NAME_CACHE

    try:
        me=await bot.get_me()
        _BOT_FIRST_NAME_CACHE=me.first_name or "Bot"
    except Exception as e:
        log.warning("Failed to resolve bot name | err=%r",e)
        _BOT_FIRST_NAME_CACHE="Bot"

    return _BOT_FIRST_NAME_CACHE

async def _safe_edit_status(bot,chat_id,message_id,text:str):
    if not message_id:
        return
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
    
    if status_msg_id: 
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

async def _send_audio_with_fallback(bot,chat_id,audio,title,performer,filename,reply_to=None,message_thread_id=None,thumbnail=None):
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
    if thumbnail:
        kwargs["thumbnail"]=thumbnail
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

def _rich_slideshow_label(fmt_key: str, source: str) -> str:
    """Label platform untuk caption slideshow (bukan hardcode TikTok)."""
    key = (fmt_key or "").strip().lower()
    src = (source or "").strip().lower()
    if key in ("slideshow_images", "slideshow_video") or src == "tiktok":
        return "TikTok Slideshow"
    if src in ("instagram", "ig"):
        return "Instagram"
    if src in ("x", "twitter"):
        return "X / Twitter"
    if src == "threads":
        return "Threads"
    if src == "reddit":
        return "Reddit"
    if src == "pinterest":
        return "Pinterest"
    if src == "facebook":
        return "Facebook"
    if src in ("pixiv", "gallery-dl", "gallerydl"):
        return "Pixiv"
    if src:
        return src.capitalize()
    return "Media"


async def _try_send_rich_slideshow(bot, chat_id, reply_to, result: dict, fmt_key: str, message_thread_id=None) -> bool:
    """Kirim album foto sebagai Rich Slideshow (swipe horizontal).

    Digunakan untuk SEMUA platform (TikTok, Instagram, X/Twitter, Threads,
    Reddit, Pinterest, dst) supaya tampilan album konsisten.

    Return True jika berhasil SEMUA chunk, False jika gagal/harus fallback
    ke sendMediaGroup (fallback menangani seluruh album dari awal).
    """
    items = result.get("items") or []
    if len(items) < 2:
        return False
    # Cek apakah semua item berupa photo (slideshow tidak mendukung video)
    paths: list[str] = []
    for it in items:
        p = it.get("path")
        if not p or not os.path.exists(p):
            return False
        if detect_media_type(p) != "photo":
            return False
        paths.append(p)

    from utils.rich_stream import send_rich_slideshow
    title = (result.get("title") or "").strip()
    source = str(result.get("source") or "").strip()
    bot_name = await _get_bot_name(bot)
    credit = f"🪄 Powered by {bot_name or 'Bot'}"
    caption = title or _rich_slideshow_label(fmt_key, source)

    # Telegram sendMediaGroup dipecah per 10; slideshow juga dipecah agar
    # jumlah foto besar tidak ditolak server.
    chunks = [paths[i:i + _ALBUM_CHUNK_SIZE] for i in range(0, len(paths), _ALBUM_CHUNK_SIZE)]
    for idx, chunk in enumerate(chunks):
        try:
            await send_rich_slideshow(
                bot=bot,
                chat_id=chat_id,
                photos=chunk,
                caption=caption,
                credit=credit,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to if idx == 0 else None,
            )
        except Exception as e:
            log.warning(
                "Rich slideshow failed | chat_id=%s chunk=%s/%s err=%r",
                chat_id, idx + 1, len(chunks), e,
            )
            if idx == 0:
                # Belum ada yang terkirim -> fallback penuh ke sendMediaGroup.
                return False
            # Sebagian sudah terkirim -> kirim SISA item via media group
            # agar tidak menggandakan foto chunk yang sudah sukses.
            await _send_media_group_result(
                bot=bot,
                chat_id=chat_id,
                reply_to=reply_to,
                result={"items": items[idx * _ALBUM_CHUNK_SIZE:], "title": title},
                message_thread_id=message_thread_id,
            )
            return True
        if idx < len(chunks) - 1 and _ALBUM_CHUNK_COOLDOWN > 0:
            await asyncio.sleep(_ALBUM_CHUNK_COOLDOWN)
    log.info(
        "Rich slideshow sent successfully | chat_id=%s count=%s chunks=%s source=%s",
        chat_id, len(paths), len(chunks), source or fmt_key,
    )
    return True


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
            # Coba kirim via Rich Slideshow jika semua item adalah foto
            sent = await _try_send_rich_slideshow(
                bot=bot, chat_id=chat_id, reply_to=reply_to,
                result=path, fmt_key=fmt_key, message_thread_id=message_thread_id,
            )
            if not sent:
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
            cover_path=(meta.get("thumb") if isinstance(meta,dict) else None)
            artist_name=(meta.get("artist") if isinstance(meta,dict) else "") or bot_name
            log.info("Sending audio | title=%r performer=%r cover=%s",caption_text[:60],artist_name,bool(cover_path and os.path.exists(cover_path)))
            fixed_audio=await reencode_mp3(
                file_path,
                cover_path=cover_path if cover_path and os.path.exists(cover_path) else None,
                title=caption_text,
                artist=artist_name,
            )
            thumb_fh=None
            try:
                if cover_path and os.path.exists(cover_path):
                    thumb_fh=open(cover_path,"rb")
                await _send_audio_with_fallback(bot=bot,chat_id=chat_id,audio=fixed_audio,title=caption_text[:64],performer=artist_name,filename=f"{caption_text[:50]}.mp3",reply_to=reply_to,message_thread_id=message_thread_id,thumbnail=thumb_fh)
            finally:
                _safe_close(thumb_fh,"audio cover",chat_id)
                if cover_path:
                    await _delete_file(cover_path,"audio cover")
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
                pre_thumb=meta.get("thumb") if isinstance(meta,dict) else None
                pre_meta=meta.get("meta") if isinstance(meta,dict) else None
                if pre_thumb and os.path.exists(pre_thumb):
                    # Thumbnail sudah dibuat paralel dengan remux di tahap processing.
                    thumb_path=pre_thumb
                    meta_video=pre_meta or await asyncio.to_thread(video_meta,file_path)
                else:
                    meta_video,thumb_path=await asyncio.gather(
                        asyncio.to_thread(video_meta,file_path),
                        asyncio.to_thread(make_video_thumbnail,file_path),
                    )
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

async def download_non_tiktok(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id:str|None,has_audio:bool,engine:str|None=None,metadata_ready:bool=False,known_size:int=0):
    if is_instagram_url(raw_url):
        try:
            t0=time.monotonic()
            result=await instagram_api_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,metadata_ready=metadata_ready)
            stage("scrape+download:instagram",t0,job=raw_url)
            return result
        except FileSizeLimitExceeded:
            raise
        except Exception as e:
            log.warning("Instagram API download failed, falling back to yt-dlp | url=%s err=%r",raw_url,e)
    if is_pinterest_url(raw_url):
        t0=time.monotonic()
        result=await pinterest_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
        stage("scrape+download:pinterest",t0,job=raw_url)
        return result
    if is_facebook_url(raw_url):
        t0=time.monotonic()
        result=await facebook_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
        stage("scrape+download:facebook",t0,job=raw_url)
        return result
    if is_reddit_url(raw_url):
        t0=time.monotonic()
        result=await reddit_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
        stage("scrape+download:reddit",t0,job=raw_url)
        return result
    if is_x_url(raw_url):
        t0=time.monotonic()
        result=await twitter_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
        stage("scrape+download:twitter",t0,job=raw_url)
        return result
    if is_threads_url(raw_url):
        t0=time.monotonic()
        result=await threads_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
        stage("scrape+download:threads",t0,job=raw_url)
        return result
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
    if is_youtube_post_url(raw_url):
        t0=time.monotonic()
        result=await download_youtube_post(raw_url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id)
        stage("scrape+download:youtube_post",t0,job=raw_url)
        return result
    if is_youtube_url(raw_url):
        if (engine or "").strip().lower() not in ("","ytdlp"):
            log.warning("Unsupported YouTube engine ignored | url=%s engine=%s",raw_url,engine)
        t0=time.monotonic()
        result=await ytdlp_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio,known_size=known_size)
        stage("scrape+download:youtube",t0,job=raw_url)
        file_path=result.get("path") if isinstance(result,dict) else result
        if not file_path:
            raise RuntimeError("yt-dlp returned no file")
        return result

    t0=time.monotonic()
    result=await ytdlp_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio,known_size=known_size)
    stage("scrape+download:ytdlp",t0,job=raw_url)
    return result


async def send_batch_downloaded_media(bot,chat_id,reply_to,status_msg_id,results:list,message_thread_id=None):
    """Kirim hasil batch (multi-link).

    Foto/video dikumpulkan jadi satu album Telegram. Audio dikirim terpisah
    setelah album supaya tetap rapi.
    """
    album_items:list[dict]=[]
    audio_entries:list[dict]=[]
    extra_files:list[str]=[]

    for res in results:
        if not res:
            continue
        if isinstance(res,dict) and res.get("items"):
            for it in res.get("items") or []:
                p=it.get("path")
                if p and os.path.exists(p):
                    album_items.append({"path":p,"type":detect_media_type(p)})
            continue
        meta=res if isinstance(res,dict) else {"path":res,"title":None}
        p=meta.get("path")
        if not p or not os.path.exists(p):
            continue
        if str(meta.get("kind") or "").lower()=="audio" or detect_media_type(p)=="unknown" and p.lower().endswith((".mp3",".flac")):
            audio_entries.append(meta)
        else:
            album_items.append({"path":p,"type":detect_media_type(p)})

    log.info("Batch send | chat_id=%s album=%s audio=%s",chat_id,len(album_items),len(audio_entries))

    if album_items:
        payload={"items":[{"path":it["path"],"type":it["type"]} for it in album_items],"title":"Batch Download"}
        try:
            await _send_media_group_result(bot=bot,chat_id=chat_id,reply_to=reply_to,result=payload,message_thread_id=message_thread_id)
        except Exception as e:
            log.warning("Batch album send failed, sending individually | chat_id=%s err=%r",chat_id,e)
            for it in album_items:
                try:
                    await send_downloaded_media(bot=bot,chat_id=chat_id,reply_to=reply_to,status_msg_id=None,path={"path":it["path"],"title":None},fmt_key="mp4",message_thread_id=message_thread_id)
                except Exception as e2:
                    log.warning("Batch item send failed | path=%s err=%r",it["path"],e2)
        finally:
            await _cleanup_album_files(album_items)

    for meta in audio_entries:
        p=meta.get("path")
        try:
            await _set_uploading_status(bot,chat_id,status_msg_id,"audio")
            cover=meta.get("thumb")
            artist=(meta.get("artist") or "") or await _get_bot_name(bot)
            title=(meta.get("title") or _clean_caption_from_path(p) or "Audio")
            fixed=await reencode_mp3(p,cover_path=cover if cover and os.path.exists(cover) else None,title=title,artist=artist)
            thumb_fh=None
            try:
                if cover and os.path.exists(cover):
                    thumb_fh=open(cover,"rb")
                await _send_audio_with_fallback(bot=bot,chat_id=chat_id,audio=fixed,title=title[:64],performer=artist,filename=f"{title[:50]}.mp3",reply_to=reply_to,message_thread_id=message_thread_id,thumbnail=thumb_fh)
            finally:
                _safe_close(thumb_fh,"audio cover",chat_id)
            await _delete_file(fixed,"temp audio")
            if cover:
                await _delete_file(cover,"audio cover")
        except Exception as e:
            log.warning("Batch audio send failed | path=%s err=%r",p,e)
        finally:
            await _cleanup_single_file(p)
