import os
import time
import json
import asyncio
import logging
from utils.config import BOT_TOKEN
from telegram.error import RetryAfter
from .utils import progress_bar

log=logging.getLogger(__name__)
try:
    from telethon import TelegramClient
    from telethon.tl.types import DocumentAttributeVideo
except Exception as e:
    TelegramClient=None
    DocumentAttributeVideo=None
    log.warning("Telethon import failed | err=%r",e)
try:
    from FastTelethonhelper.FastTelethon import upload_file as fast_upload_file
except Exception as e:
    fast_upload_file=None
    log.warning("FastTelethonhelper import failed | err=%r",e)

_CLIENT=None
_CLIENT_LOCK=asyncio.Lock()
_PROGRESS_LOCKS={}
_SESSION_NAME=os.getenv("MTPROTO_SESSION","tgdata/mtproto_bot")
_ENABLED=os.getenv("MTPROTO_UPLOAD","1").lower() not in ("0","false","off","no")
_FAST_STATE_FILE=os.getenv("MTPROTO_FAST_STATE_FILE","tgdata/fasttelethon.json")
_FAST_UPLOAD_DEFAULT=os.getenv("MTPROTO_FAST_UPLOAD","0").lower() in ("1","true","on","yes")
_PROGRESS_MIN_BYTES=int(os.getenv("MTPROTO_PROGRESS_MIN_BYTES",str(5*1024*1024)))
_PROGRESS_SMALL_LIMIT=int(os.getenv("MTPROTO_PROGRESS_SMALL_LIMIT",str(100*1024*1024)))
_PROGRESS_SMALL_INTERVAL=float(os.getenv("MTPROTO_PROGRESS_SMALL_INTERVAL","5.0"))
_PROGRESS_LARGE_INTERVAL=float(os.getenv("MTPROTO_PROGRESS_LARGE_INTERVAL","10.0"))
_PROGRESS_STEP=float(os.getenv("MTPROTO_PROGRESS_STEP","5"))
_PART_SIZE_KB=max(32,min(int(os.getenv("MTPROTO_PART_SIZE_KB","512")),512))
_FAST_UPLOAD_ENABLED=None

def _format_size(num:int|float)->str:
    value=float(num or 0)
    for unit in ("B","KB","MB","GB"):
        if value<1024 or unit=="GB":
            return f"{int(value)} {unit}" if unit=="B" else f"{value:.1f} {unit}"
        value/=1024
    return f"{value:.1f} GB"

def _format_eta(seconds:int|float)->str:
    seconds=int(max(float(seconds or 0),0))
    if seconds<=0:
        return "-"
    h,rem=divmod(seconds,3600)
    m,s=divmod(rem,60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def _progress_interval(file_size:int)->float:
    return _PROGRESS_SMALL_INTERVAL if file_size<_PROGRESS_SMALL_LIMIT else _PROGRESS_LARGE_INTERVAL

def _get_progress_lock(key):
    lock=_PROGRESS_LOCKS.get(key)
    if lock is None:
        lock=asyncio.Lock()
        _PROGRESS_LOCKS[key]=lock
    return lock

def _drop_progress_lock(key):
    lock=_PROGRESS_LOCKS.get(key)
    if lock and not lock.locked():
        _PROGRESS_LOCKS.pop(key,None)

def _load_fast_upload_enabled()->bool:
    try:
        if os.path.exists(_FAST_STATE_FILE):
            with open(_FAST_STATE_FILE,"r",encoding="utf-8") as f:
                data=json.load(f)
            return bool(data.get("enabled",_FAST_UPLOAD_DEFAULT))
    except Exception as e:
        log.warning("Failed to load FastTelethon state | file=%s err=%r",_FAST_STATE_FILE,e)
    return bool(_FAST_UPLOAD_DEFAULT)

def is_fasttelethon_enabled()->bool:
    global _FAST_UPLOAD_ENABLED
    if _FAST_UPLOAD_ENABLED is None:
        _FAST_UPLOAD_ENABLED=_load_fast_upload_enabled()
    return bool(_FAST_UPLOAD_ENABLED)

def is_fasttelethon_available()->bool:
    return fast_upload_file is not None

def set_fasttelethon_enabled(enabled:bool)->bool:
    global _FAST_UPLOAD_ENABLED
    _FAST_UPLOAD_ENABLED=bool(enabled)
    try:
        os.makedirs(os.path.dirname(_FAST_STATE_FILE) or ".",exist_ok=True)
        tmp=f"{_FAST_STATE_FILE}.tmp"
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump({"enabled":bool(enabled),"updated_at":int(time.time())},f)
        os.replace(tmp,_FAST_STATE_FILE)
    except Exception as e:
        log.warning("Failed to save FastTelethon state | file=%s err=%r",_FAST_STATE_FILE,e)
    return _FAST_UPLOAD_ENABLED

async def _disconnect_client(client,label:str):
    try:
        if client and client.is_connected():
            await client.disconnect()
            log.info("MTProto uploader disconnected | reason=%s",label)
    except Exception as e:
        log.warning("MTProto uploader disconnect failed | reason=%s err=%r",label,e)

async def _get_client():
    global _CLIENT
    if not _ENABLED:
        raise RuntimeError("MTProto upload disabled")
    if TelegramClient is None:
        raise RuntimeError("Telethon is not installed")
    api_id=os.getenv("TG_API_ID") or os.getenv("API_ID")
    api_hash=os.getenv("TG_API_HASH") or os.getenv("API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("TG_API_ID/TG_API_HASH is not configured")
    async with _CLIENT_LOCK:
        if _CLIENT and _CLIENT.is_connected():
            return _CLIENT
        if _CLIENT:
            await _disconnect_client(_CLIENT,"reconnect")
            _CLIENT=None
        os.makedirs(os.path.dirname(_SESSION_NAME) or ".",exist_ok=True)
        _CLIENT=TelegramClient(_SESSION_NAME,int(api_id),api_hash)
        await _CLIENT.start(bot_token=BOT_TOKEN)
        log.info(
            "MTProto uploader ready | session=%s part_size=%sKB fast=%s fast_available=%s",
            _SESSION_NAME,
            _PART_SIZE_KB,
            is_fasttelethon_enabled(),
            is_fasttelethon_available(),
        )
        return _CLIENT

async def _resolve_entity(client,chat_id):
    try:
        return await client.get_input_entity(chat_id)
    except Exception as e:
        log.warning("MTProto get_input_entity failed | chat_id=%s err=%r",chat_id,e)
    try:
        async for dialog in client.iter_dialogs(limit=300):
            entity=dialog.entity
            entity_id=getattr(entity,"id",None)
            if entity_id and int(f"-100{entity_id}")==int(chat_id):
                return await client.get_input_entity(entity)
    except Exception as e:
        log.warning("MTProto iter_dialogs resolve failed | chat_id=%s err=%r",chat_id,e)
    return chat_id

async def warmup_mtproto_uploader(app=None):
    if not _ENABLED:
        log.info("MTProto uploader warmup skipped | disabled")
        return
    try:
        await _get_client()
        log.info("MTProto uploader warmup done")
    except Exception as e:
        log.warning("MTProto uploader warmup failed | err=%r",e)

async def shutdown_mtproto_uploader(app=None):
    global _CLIENT
    await _disconnect_client(_CLIENT,"shutdown")
    _CLIENT=None

async def _safe_edit_upload(bot,chat_id,message_id,current,total,started,label="Uploading video"):
    if not message_id:
            return
    key=(int(chat_id),int(message_id))
    lock=_get_progress_lock(key)
    async with lock:
        try:
            current=max(int(current or 0),0)
            total=max(int(total or 0),0)
            percent=(current/total*100) if total else 0
            elapsed=max(time.monotonic()-started,0.001)
            speed=current/elapsed
            remaining=max(total-current,0)
            eta=(remaining/speed) if speed>0 and total else 0
            text=(
                f"<b>{label}...</b>\n\n"
                f"<code>{progress_bar(percent)}</code>\n"
                f"<code>{_format_size(current)}/{_format_size(total)}</code>\n"
                f"<code>Speed: {_format_size(speed)}/s</code>\n"
                f"<code>ETA: {_format_eta(eta)}</code>"
            )
            await bot.edit_message_text(chat_id=chat_id,message_id=message_id,text=text,parse_mode="HTML")
            log.info(
                "MTProto upload progress | chat_id=%s %.1f%% %s/%s speed=%s/s eta=%s label=%s",
                chat_id,
                percent,
                _format_size(current),
                _format_size(total),
                _format_size(speed),
                _format_eta(eta),
                label,
            )
        except RetryAfter as e:
            wait=int(getattr(e,"retry_after",1))
            log.warning("MTProto progress RetryAfter | chat_id=%s wait=%s",chat_id,wait)
            await asyncio.sleep(wait+1)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                log.warning("MTProto upload progress edit failed | chat_id=%s err=%r",chat_id,e)

def _make_progress_callback(bot,chat_id,status_msg_id,file_size,started,show_progress,interval,label):
    state={"last_ts":0.0,"last_pct":-1.0,"task":None}
    loop=asyncio.get_running_loop()
    def progress_callback(current,total):
        if not show_progress or not total:
            return
        if file_size and total<file_size*0.8:
            return
        now=time.monotonic()
        pct=(current/total*100) if total else 0
        if pct<100 and now-state["last_ts"]<interval:
            return
        if pct<100 and state["last_pct"]>=0 and pct-state["last_pct"]<_PROGRESS_STEP:
            return
        if state["task"] and not state["task"].done():
            return
        state["last_ts"]=now
        state["last_pct"]=pct
        state["task"]=loop.create_task(_safe_edit_upload(bot,chat_id,status_msg_id,current,total,started,label=label))
    return progress_callback,state

async def _wait_last_progress_task(state:dict):
    task=state.get("task")
    if task:
        await asyncio.gather(task,return_exceptions=True)

def _close_file_handle(fh,path:str):
    try:
        fh.close()
    except Exception as e:
        log.warning("Failed to close upload file handle | file=%s err=%r",os.path.basename(path),e)

async def _fast_upload_video(client,file_path,progress_callback):
    if not is_fasttelethon_enabled():
        return None
    if fast_upload_file is None:
        raise RuntimeError("FastTelethonhelper is not installed")
    name=os.path.basename(file_path) or "video.mp4"
    log.info("FastTelethon upload start | file=%s",name)
    f=open(file_path,"rb")
    try:
        uploaded=await fast_upload_file(client=client,file=f,name=name,progress_callback=progress_callback)
    finally:
        _close_file_handle(f,file_path)
    log.info("FastTelethon upload done | file=%s",name)
    return uploaded

async def try_send_video_via_mtproto(bot,chat_id,status_msg_id,file_path,caption,reply_to=None,message_thread_id=None,duration=None,width=None,height=None,thumb_path=None):
    if not _ENABLED:
        return False
    if not file_path or not os.path.exists(file_path):
        return False
    key=(int(chat_id),int(status_msg_id) if status_msg_id else 0)
    file_size=os.path.getsize(file_path)
    show_progress=file_size>=_PROGRESS_MIN_BYTES
    interval=_progress_interval(file_size)
    started=time.monotonic()
    fast_used=False
    state={"task":None}
    try:
        client=await _get_client()
        entity=await _resolve_entity(client,chat_id)
        attrs=[]
        if DocumentAttributeVideo and duration and width and height:
            attrs.append(DocumentAttributeVideo(duration=int(duration),w=int(width),h=int(height),supports_streaming=True))
        label="FastTelethon uploading video" if is_fasttelethon_enabled() else "Uploading video"
        progress_callback,state=_make_progress_callback(bot,chat_id,status_msg_id,file_size,started,show_progress,interval,label)
        log.info(
            "MTProto upload start | chat_id=%s file=%s size=%s progress=%s interval=%.1fs part_size=%sKB fast=%s fast_available=%s",
            chat_id,
            os.path.basename(file_path),
            _format_size(file_size),
            show_progress,
            interval,
            _PART_SIZE_KB,
            is_fasttelethon_enabled(),
            is_fasttelethon_available(),
        )
        uploaded_file=None
        if is_fasttelethon_enabled():
            try:
                uploaded_file=await _fast_upload_video(client,file_path,progress_callback)
                fast_used=uploaded_file is not None
                await _wait_last_progress_task(state)
            except Exception as e:
                log.warning(
                    "FastTelethon upload failed, fallback to normal Telethon | chat_id=%s file=%s err=%r",
                    chat_id,
                    os.path.basename(file_path),
                    e,
                )
                started=time.monotonic()
                progress_callback,state=_make_progress_callback(bot,chat_id,status_msg_id,file_size,started,show_progress,interval,"Uploading video")
        send_kwargs={
            "entity":entity,
            "file":uploaded_file or file_path,
            "caption":caption,
            "parse_mode":"html",
            "force_document":False,
            "supports_streaming":True,
            "attributes":attrs or None,
            "thumb":thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            "reply_to":reply_to,
        }
        if uploaded_file is None:
            send_kwargs["progress_callback"]=progress_callback
            send_kwargs["part_size_kb"]=_PART_SIZE_KB
        await client.send_file(**send_kwargs)
        await _wait_last_progress_task(state)
        elapsed=time.monotonic()-started
        speed=file_size/max(elapsed,0.001)
        log.info(
            "Telegram MTProto send done | chat_id=%s file=%s size=%s elapsed=%.2fs avg_speed=%s/s fast=%s",
            chat_id,
            os.path.basename(file_path),
            _format_size(file_size),
            elapsed,
            _format_size(speed),
            fast_used,
        )
        return True
    except Exception as e:
        log.warning("MTProto upload failed, fallback to PTB | chat_id=%s file=%s err=%r",chat_id,os.path.basename(file_path),e)
        return False
    finally:
        await _wait_last_progress_task(state)
        _drop_progress_lock(key)