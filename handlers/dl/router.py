import os
import re
import uuid
import time
import html
import logging
import asyncio
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from utils.config import OWNER_ID
from database.premium import init_premium_db
from .constants import TMP_DIR,DL_FORMATS,PREMIUM_ONLY_DOMAINS,AUTO_DOWNLOAD_DOMAINS
from .state import DL_CACHE
from database.download_db import load_auto_dl,save_auto_dl,is_premium_user,is_premium_required
from .utils import normalize_url,is_invalid_video
from .keyboards import dl_keyboard,res_keyboard,autodl_detect_keyboard,tiktok_slideshow_keyboard
from .probe import get_resolutions,supports_resolution_picker,supports_ytdlp_resolution
from .tiktok.main import is_tiktok,douyin_download,tiktok_download
from .service import download_non_tiktok,send_downloaded_media
from database.user_settings_db import get_user_settings
from .remux import prepare_download_result_for_send
from .youtube.main import is_youtube_url

log=logging.getLogger(__name__)
os.makedirs(TMP_DIR,exist_ok=True)
TIKTOK_LOCK=asyncio.Semaphore(3)
YTDLP_SEM=asyncio.Semaphore(4)
_MAX_FLOOD_RETRY=2

DL_LIMIT_CACHE = {}

def _check_and_consume_limit(user_id: int) -> int:
    if is_premium_user(user_id):
        return 0
    now = time.time()
    history = DL_LIMIT_CACHE.get(user_id, [])
    history = [ts for ts in history if now - ts < 60]
    if len(history) >= 3:
        DL_LIMIT_CACHE[user_id] = history
        return max(1, 60 - int(now - history[0]))
    history.append(now)
    DL_LIMIT_CACHE[user_id] = history
    return 0

def _host(url:str)->str:
    try:
        u=urlparse((url or "").strip())
        return (u.hostname or "").lower()
    except Exception as e:
        log.debug("Failed to parse URL host | url=%r err=%r",url,e)
        return ""

def _host_match(host:str,domain:str)->bool:
    host=(host or "").lower()
    domain=(domain or "").lower()
    return host==domain or host.endswith("."+domain)

def is_supported_platform(url:str)->bool:
    host=_host(url)
    if not host:
        return False
    return any(_host_match(host,d) for d in AUTO_DOWNLOAD_DOMAINS)

def _format_id_for_engine(engine:str|None,height:int,picked:dict)->str:
    return str(picked.get("format_id") or "")

def _pick_auto_resolution(res_map:dict[int,dict],preferred_height:int):
    try:
        preferred_height=int(preferred_height or 0)
    except (TypeError,ValueError):
        preferred_height=0
    if preferred_height<=0 or not res_map:
        return None,None
    candidates=[]
    for h,item in res_map.items():
        try:
            height=int(h)
        except (TypeError,ValueError):
            continue
        candidates.append((height,item))
    if not candidates:
        return None,None
    candidates.sort(key=lambda x:x[0],reverse=True)
    for height,item in candidates:
        if height==preferred_height:
            return height,item
    lower=[(height,item) for height,item in candidates if height<=preferred_height]
    if lower:
        lower.sort(key=lambda x:x[0],reverse=True)
        return lower[0]
    return candidates[0]

def _platform_label(url:str)->str:
    host=_host(url)
    checks=(
        (("tiktok.com","vt.tiktok.com","vm.tiktok.com","douyin.com"),"TikTok"),
        (("instagram.com","instagr.am"),"Instagram"),
        (("youtube.com","youtu.be","music.youtube.com"),"YouTube"),
        (("facebook.com","fb.watch","m.facebook.com"),"Facebook"),
        (("x.com","twitter.com","vxtwitter.com","fxtwitter.com"),"X"),
        (("reddit.com","redd.it"),"Reddit"),
        (("threads.net","threads.com"),"Threads"),
        (("pinterest.com","pin.it"),"Pinterest"),
    )
    for domains,label in checks:
        if any(_host_match(host,d) for d in domains):
            return label
    return "Media"

def _metadata_status(url:str)->str:
    return f"<b>Scraping {_platform_label(url)} metadata...</b>"
    
async def _safe_delete_message(bot,chat_id,message_id,label:str="message"):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id,message_id)
        log.debug("Deleted %s | chat_id=%s message_id=%s",label,chat_id,message_id)
    except Exception as e:
        log.debug("Failed to delete %s | chat_id=%s message_id=%s err=%r",label,chat_id,message_id,e)

async def _safe_edit_error(bot,chat_id,message_id,text:str):
    if not message_id:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception: pass
        return
    try:
        await bot.edit_message_text(chat_id=chat_id,message_id=message_id,text=text,parse_mode="HTML")
    except Exception as e:
        log.warning("Failed to edit downloader error status | chat_id=%s message_id=%s err=%r",chat_id,message_id,e)

async def _remove_file(path:str|None,label:str):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("Deleted %s temp file | file=%s",label,os.path.basename(path))
    except Exception as e:
        log.warning("Failed to delete %s temp file | path=%s err=%r",label,path,e)

async def _start_dl_task(context,message,data,fmt_key,format_id=None,has_audio=False,label=None,engine:str|None=None,status_ready:bool=False):
    log.info(
        "Start download task | url=%s fmt_key=%s format_id=%s has_audio=%s engine=%s label=%s status_ready=%s",
        data.get("url"),fmt_key,format_id,has_audio,engine,label,status_ready,
    )
    if not status_ready and message:
        await message.edit_text(_metadata_status(data["url"]),parse_mode="HTML")
        status_ready=True
        
    chat_id = data.get("chat_id")
    status_msg_id = message.message_id if message else None

    context.application.create_task(
        _dl_worker(
            app=context.application,
            chat_id=chat_id,
            reply_to=data.get("reply_to"),
            raw_url=data["url"],
            fmt_key=fmt_key,
            status_msg_id=status_msg_id,
            format_id=format_id,
            has_audio=has_audio,
            engine=engine,
            message_thread_id=data.get("message_thread_id",getattr(message,"message_thread_id",None) if message else None),
            metadata_ready=status_ready,
            user_id=data.get("user"),
        )
    )

async def _show_resolution_picker(context,message,dl_id:str,data:dict,engine:str|None=None,status_ready:bool=False):
    res_list=await get_resolutions(data["url"],engine=engine)
    if not res_list:
        DL_CACHE.pop(dl_id,None)
        if message:
            return await message.edit_text("No valid resolutions available.",parse_mode="HTML")
        return await context.bot.send_message(chat_id=data["chat_id"], text="No valid resolutions available.", parse_mode="HTML", reply_to_message_id=data["reply_to"])
        
    res_map={}
    for r in res_list:
        h=int(r.get("height") or 0)
        fid=str(r.get("format_id") or "")
        if h and fid:
            res_map[h]={
                "format_id":fid,
                "has_audio":bool(r.get("has_audio")),
                "filesize":int(r.get("filesize") or 0),
                "total_size":int(r.get("total_size") or 0),
            }
    
    settings=get_user_settings(data["user"])
    preferred_height = int(settings.get("youtube_resolution") or 0)
    
    silent_mode = bool(settings.get("silent_download", 0))
    if preferred_height == 0 and silent_mode:
        preferred_height = 720

    if preferred_height > 720 and not is_premium_user(data["user"]):
        preferred_height = 720

    if preferred_height>0:
        picked_height,picked=_pick_auto_resolution(res_map,preferred_height)
        if picked_height and picked:
            DL_CACHE.pop(dl_id,None)
            return await _start_dl_task(
                context=context,
                message=message,
                data=data,
                fmt_key="video",
                format_id=_format_id_for_engine(engine,picked_height,picked),
                has_audio=bool(picked.get("has_audio")),
                label=f"{picked_height}p",
                engine=engine,
                status_ready=status_ready,
            )
            
    DL_CACHE[dl_id]["res_map"]=res_map
    if engine:
        DL_CACHE[dl_id]["engine"]=engine
        
    if message:
        return await message.edit_text("<b>Select resolution</b>",reply_markup=res_keyboard(dl_id,res_list),parse_mode="HTML")
    else:
        return await context.bot.send_message(chat_id=data["chat_id"], text="<b>Select resolution</b>",reply_markup=res_keyboard(dl_id,res_list),parse_mode="HTML", reply_to_message_id=data["reply_to"])


async def _process_choice(context,message,dl_id:str,data:dict,choice:str,user_id:int,status_ready:bool=False):
    url=data["url"]
    if choice=="video" and supports_resolution_picker(url):
        DL_CACHE[dl_id]["fmt_key"]="video"
        if supports_ytdlp_resolution(url):
            if not status_ready and message:
                await message.edit_text(_metadata_status(url),parse_mode="HTML")
                status_ready=True
            return await _show_resolution_picker(context,message,dl_id,data,engine="ytdlp",status_ready=status_ready)
    DL_CACHE.pop(dl_id,None)
    return await _start_dl_task(context=context,message=message,data=data,fmt_key=choice,format_id=None,has_audio=False,status_ready=status_ready)

async def _is_admin_or_owner(update:Update,context:ContextTypes.DEFAULT_TYPE)->bool:
    user=update.effective_user
    chat=update.effective_chat
    if user and user.id in OWNER_ID:
        return True
    if not user or not chat or chat.type not in ("group","supergroup"):
        return False
    try:
        member=await context.bot.get_chat_member(chat.id,user.id)
        return member.status in ("administrator","creator")
    except Exception as e:
        log.debug("Failed to check admin status | chat_id=%s user_id=%s err=%r",getattr(chat,"id",None),getattr(user,"id",None),e)
        return False

async def autodl_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    chat=update.effective_chat
    msg=update.message
    if not chat or not msg or not msg.from_user:
        return
    user_id=msg.from_user.id
    if chat.type=="private":
        return
    if not await _is_admin_or_owner(update,context):
        return await msg.reply_text("<b>You are not an admin.</b>",parse_mode="HTML")
    groups=load_auto_dl()
    arg=context.args[0].lower() if context.args else ""
    if arg=="enable":
        groups.add(chat.id)
        save_auto_dl(groups)
        return await msg.reply_text("Auto-detect link <b>ENABLED</b> in this group.",parse_mode="HTML")
    if arg=="disable":
        groups.discard(chat.id)
        save_auto_dl(groups)
        return await msg.reply_text("Auto-detect link <b>DISABLED</b> in this group.",parse_mode="HTML")
    if arg=="status":
        if chat.id in groups:
            return await msg.reply_text("Auto-detect Status: <b>ENABLED</b>",parse_mode="HTML")
        return await msg.reply_text("Auto-detect Status: <b>DISABLED</b>",parse_mode="HTML")
    if arg=="list":
        if user_id not in OWNER_ID:
            return
        if not groups:
            return await msg.reply_text("No groups with auto-detect enabled.",parse_mode="HTML")
        lines=["<b>Groups with Auto-detect Enabled:</b>\n"]
        for gid in groups:
            try:
                c=await context.bot.get_chat(gid)
                title=html.escape(c.title or str(gid))
                lines.append(f"• {title}")
            except Exception as e:
                log.debug("Failed to get autodl group title | chat_id=%s err=%r",gid,e)
                lines.append(f"• <code>{gid}</code>")
        return await msg.reply_text("\n".join(lines),parse_mode="HTML")
    return await msg.reply_text(
        "<b>Usage:</b>\n"
        "<code>/autodl enable</code>\n"
        "<code>/autodl disable</code>\n"
        "<code>/autodl status</code>\n"
        "<code>/autodl list</code>",
        parse_mode="HTML",
    )

async def auto_dl_detect(update:Update,context:ContextTypes.DEFAULT_TYPE):
    msg=update.message
    if not msg or not msg.text or not update.effective_user:
        return
    chat=update.effective_chat
    if not chat:
        return
    text=normalize_url(msg.text)
    if text.startswith("/"):
        return
    if not is_supported_platform(text):
        return
    settings=get_user_settings(update.effective_user.id)
    if chat.type in ("group","supergroup"):
        groups=load_auto_dl()
        if chat.id not in groups and not bool(settings.get("force_autodl")):
            return
    if not await require_join_or_block(update,context):
        return
        
    user_id = update.effective_user.id
    if is_premium_required(text,PREMIUM_ONLY_DOMAINS) and not is_premium_user(user_id):
        return await msg.reply_text("🔞 This link can only be downloaded by premium users.")
        
    wait_time = _check_and_consume_limit(user_id)
    if wait_time > 0:
        return await msg.reply_text(f"You are not a premium user. Please wait for a {wait_time}s cooldown.")

    dl_id=uuid.uuid4().hex[:8]
    DL_CACHE[dl_id]={
        "url":text,
        "user":user_id,
        "chat_id":chat.id,
        "reply_to":msg.message_id,
        "message_thread_id":getattr(msg,"message_thread_id",None),
        "ts":time.time(),
    }
    
    auto_choice=str(settings.get("autodl_format") or "ask").lower()
    
    # +++ FIX SILENT MODE YOUTUBE: Hapus "and not is_yt" +++
    silent_mode = bool(settings.get("silent_download")) and auto_choice == "video"
    # ++++++++++++++++++++++++++++++++++++++++++++++++++++++
    
    if auto_choice in ("video","mp3"):
        if silent_mode:
            try: await msg.set_reaction("😍")
            except Exception: pass
            status_msg = None
            status_ready = True
        else:
            status_msg = await msg.reply_text(_metadata_status(text),parse_mode="HTML")
            status_ready = True
            
        return await _process_choice(
            context=context,
            message=status_msg,
            dl_id=dl_id,
            data=DL_CACHE[dl_id],
            choice=auto_choice,
            user_id=user_id,
            status_ready=status_ready,
        )
        
    await msg.reply_text(
        "👀 <b>Link detected</b>\n\nDo you want me to download it?",
        reply_markup=autodl_detect_keyboard(dl_id),
        parse_mode="HTML",
    )


async def dl_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    msg=update.message
    if not msg or not update.effective_user:
        return
    if not context.args:
        return await msg.reply_text(
            "Send a video link to download.\n\n"
            "By using this downloader, you are responsible for the content you download and how you use it."
        )
    url=context.args[0]
    
    user_id = update.effective_user.id
    if is_premium_required(url,PREMIUM_ONLY_DOMAINS) and not is_premium_user(user_id):
        return await msg.reply_text("🔞 Download from this website is for premium users only.")
        
    wait_time = _check_and_consume_limit(user_id)
    if wait_time > 0:
        return await msg.reply_text(f"You are not a premium user. Please wait for a {wait_time}s cooldown.")
    
    dl_id=uuid.uuid4().hex[:8]
    DL_CACHE[dl_id]={
        "url":url,
        "user":user_id,
        "chat_id":msg.chat.id,
        "reply_to":msg.message_id,
        "message_thread_id":getattr(msg,"message_thread_id",None),
    }
    settings=get_user_settings(user_id)
    auto_choice=str(settings.get("autodl_format") or "ask").lower()
    silent_mode = bool(settings.get("silent_download")) and auto_choice == "video"
    if auto_choice in ("video","mp3"):
        if silent_mode:
            try: await msg.set_reaction("😍")
            except Exception: pass
            status_msg = None
            status_ready = True
        else:
            status_msg = await msg.reply_text(_metadata_status(url),parse_mode="HTML")
            status_ready = True
            
        return await _process_choice(
            context=context,
            message=status_msg,
            dl_id=dl_id,
            data=DL_CACHE[dl_id],
            choice=auto_choice,
            user_id=user_id,
            status_ready=status_ready,
        )
        
    await msg.reply_text("📥 <b>Select format</b>",reply_markup=dl_keyboard(dl_id),parse_mode="HTML")


async def dlask_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    q=update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    _,dl_id,action=q.data.split(":",2)
    data=DL_CACHE.get(dl_id)
    if not data:
        return await q.edit_message_text("Request expired.")
    if q.from_user.id!=data["user"]:
        return await q.answer("This request does not belong to you.",show_alert=True)
    if action=="close":
        DL_CACHE.pop(dl_id,None)
        return await _safe_delete_message(context.bot,q.message.chat.id,q.message.message_id,"download request menu")
    await q.edit_message_text("📥 <b>Select format</b>",reply_markup=dl_keyboard(dl_id),parse_mode="HTML")

async def _show_tiktok_slideshow_picker(bot,chat_id,status_msg_id,data:dict,media:dict,message_thread_id=None):
    dl_id=uuid.uuid4().hex[:8]
    media["slideshow_choice_ready"]=True
    DL_CACHE[dl_id]={
        "url":data.get("url"),
        "user":data.get("user"),
        "chat_id":chat_id,
        "reply_to":data.get("reply_to"),
        "message_thread_id":message_thread_id or data.get("message_thread_id"),
        "media":media,
        "ts":time.time(),
    }
    has_audio=bool(media.get("music_url") or media.get("music_urls"))
    text="<b>TikTok Slideshow Detected</b>\n\nChoose output format:"
    
    if not status_msg_id:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, reply_markup=tiktok_slideshow_keyboard(dl_id,has_audio=has_audio), parse_mode="HTML", reply_to_message_id=data.get("reply_to"))
        except Exception: pass
        return
        
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg_id,
        text=text,
        reply_markup=tiktok_slideshow_keyboard(dl_id,has_audio=has_audio),
        parse_mode="HTML",
    )
    
async def _dl_worker(app,chat_id,reply_to,raw_url,fmt_key,status_msg_id,format_id:str|None=None,has_audio:bool=False,engine:str|None=None,message_thread_id:int|None=None,metadata_ready:bool=False,user_id:int|None=None,_flood_retry:int=0):
    bot=app.bot
    path=None
    try:
        log.info(
            "Download worker start | url=%s fmt_key=%s format_id=%s has_audio=%s engine=%s",
            raw_url,fmt_key,format_id,has_audio,engine,
        )
        if is_tiktok(raw_url):
            async with TIKTOK_LOCK:
                path=await tiktok_download(raw_url,bot,chat_id,status_msg_id,fmt_key,metadata_ready=metadata_ready)
                
                if isinstance(path,dict) and path.get("choice_required")=="tiktok_slideshow":
                    data={
                        "url":raw_url,
                        "user":user_id,
                        "reply_to":reply_to,
                        "message_thread_id":message_thread_id,
                        "chat_id": chat_id,
                    }
                    
                    settings = get_user_settings(user_id) if user_id else {}
                    auto_tt_format = settings.get("tiktok_slideshow", "ask").lower()
                    
                    if auto_tt_format != "ask":
                        log.info(f"Auto-selecting TikTok Slideshow format ({auto_tt_format}) for user {user_id}")
                        fmt_map={"images":"slideshow_images","video":"slideshow_video","audio":"mp3"}
                        mapped_fmt = fmt_map.get(auto_tt_format, "slideshow_video")
                        
                        if status_msg_id:
                            from .tiktok.main import _metadata_status
                            try: await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=_metadata_status(raw_url), parse_mode="HTML")
                            except Exception: pass
                            
                        mock_context = type('MockContext', (object,), {'application': app})()
                        mock_chat = type('MockChat', (object,), {'id': chat_id})()
                        mock_msg = type('MockMessage', (object,), {'chat': mock_chat, 'message_id': status_msg_id})() if status_msg_id else None
                        
                        return await _start_dl_task(
                            context=mock_context,
                            message=mock_msg,
                            data=data,
                            fmt_key=mapped_fmt,
                            format_id=None,
                            has_audio=False,
                            status_ready=True,
                        )
                    await _show_tiktok_slideshow_picker(
                        bot=bot,
                        chat_id=chat_id,
                        status_msg_id=status_msg_id,
                        data=data,
                        media=path.get("media") or {},
                        message_thread_id=message_thread_id,
                    )
                    return
        
                actual_path=path.get("path") if isinstance(path,dict) else path
                result_kind=str((path or {}).get("kind") or "").lower() if isinstance(path,dict) else ""
                should_validate_video=fmt_key!="mp3" and result_kind!="audio"
        
                if should_validate_video and actual_path and is_invalid_video(actual_path):
                    await _remove_file(actual_path,"invalid TikTok")
                    raise RuntimeError("Static video")
        else:
            async with YTDLP_SEM:
                path=await download_non_tiktok(
                    raw_url=raw_url,
                    fmt_key=fmt_key,
                    bot=bot,
                    chat_id=chat_id,
                    status_msg_id=status_msg_id,
                    format_id=format_id,
                    has_audio=has_audio,
                    engine=engine,
                    metadata_ready=metadata_ready,
                )
        prepare_started=time.monotonic()
        path=await prepare_download_result_for_send(path,fmt_key=fmt_key)
        log.info("Prepare media done | url=%s elapsed=%.2fs",raw_url,time.monotonic()-prepare_started)
        await send_downloaded_media(
            bot=bot,
            chat_id=chat_id,
            reply_to=reply_to,
            status_msg_id=status_msg_id,
            path=path,
            fmt_key=fmt_key,
            message_thread_id=message_thread_id,
        )
        if status_msg_id:
            await _safe_delete_message(bot,chat_id,status_msg_id,"download status")
    except Exception as e:
        err=str(e) or repr(e)
        if "Flood control exceeded" in err and "Retry in" in err and _flood_retry<_MAX_FLOOD_RETRY:
            m=re.search(r"Retry in (\d+)",err)
            wait_time=int(m.group(1)) if m else 5
            log.warning("Download worker flood retry | chat_id=%s wait=%s retry=%s url=%s",chat_id,wait_time,_flood_retry+1,raw_url)
            await asyncio.sleep(wait_time)
            return await _dl_worker(
                app=app,
                chat_id=chat_id,
                reply_to=reply_to,
                raw_url=raw_url,
                fmt_key=fmt_key,
                status_msg_id=status_msg_id,
                format_id=format_id,
                has_audio=has_audio,
                engine=engine,
                message_thread_id=message_thread_id,
                metadata_ready=metadata_ready,
                user_id=user_id,
                _flood_retry=_flood_retry+1,
            )
        log.warning("Download worker failed | chat_id=%s url=%s err=%r",chat_id,raw_url,e)
        public_err=html.escape(err.strip())[:3500] or "Unknown downloader error"
        await _safe_edit_error(bot, chat_id, status_msg_id, f"<b>Download failed</b>\n\n<code>{public_err}</code>")


async def dl_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    msg=update.message
    if not msg or not update.effective_user:
        return
    if not context.args:
        return await msg.reply_text(
            "Send a video link to download.\n\n"
            "By using this downloader, you are responsible for the content you download and how you use it."
        )
    url=context.args[0]
    
    user_id = update.effective_user.id
    if is_premium_required(url,PREMIUM_ONLY_DOMAINS) and not is_premium_user(user_id):
        return await msg.reply_text("🔞 Download from this website is for premium users only.")
        
    wait_time = _check_and_consume_limit(user_id)
    if wait_time > 0:
        return await msg.reply_text(f"You are not a premium user. Please wait for a {wait_time}s cooldown.")
    
    dl_id=uuid.uuid4().hex[:8]
    DL_CACHE[dl_id]={
        "url":url,
        "user":user_id,
        "chat_id":msg.chat.id,
        "reply_to":msg.message_id,
        "message_thread_id":getattr(msg,"message_thread_id",None),
    }
    settings=get_user_settings(user_id)
    auto_choice=str(settings.get("autodl_format") or "ask").lower()
    
    is_yt = is_youtube_url(url)
    silent_mode = bool(settings.get("silent_download")) and auto_choice == "video" and not is_yt
    
    if auto_choice in ("video","mp3"):
        if silent_mode:
            try: await msg.set_reaction("😍")
            except Exception: pass
            status_msg = None
            status_ready = True
        else:
            status_msg = await msg.reply_text(_metadata_status(url),parse_mode="HTML")
            status_ready = True
            
        return await _process_choice(
            context=context,
            message=status_msg,
            dl_id=dl_id,
            data=DL_CACHE[dl_id],
            choice=auto_choice,
            user_id=user_id,
            status_ready=status_ready,
        )
        
    await msg.reply_text("📥 <b>Select format</b>",reply_markup=dl_keyboard(dl_id),parse_mode="HTML")

async def dlengine_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    q=update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    _,dl_id,_engine=q.data.split(":",2)
    data=DL_CACHE.get(dl_id)
    if not data:
        return await q.edit_message_text("Request expired.")
    if q.from_user.id!=data["user"]:
        return await q.answer("This request does not belong to you.",show_alert=True)
    data["engine"]="ytdlp"
    log.info("Engine callback selected | url=%s engine=ytdlp",data.get("url"))
    await q.edit_message_text("<b>Fetching video formats...</b>",parse_mode="HTML")
    return await _show_resolution_picker(context,q.message,dl_id,data,engine="ytdlp")

async def dl_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    q=update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    _,dl_id,choice=q.data.split(":",2)
    data=DL_CACHE.get(dl_id)
    if not data:
        return await q.edit_message_text("Data expired.")
    if q.from_user.id!=data["user"]:
        return await q.answer("This request does not belong to you.",show_alert=True)
    if choice=="cancel":
        DL_CACHE.pop(dl_id,None)
        return await q.edit_message_text("Cancelled.")
    return await _process_choice(context=context,message=q.message,dl_id=dl_id,data=data,choice=choice,user_id=q.from_user.id)

async def tiktok_slideshow_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    q=update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    _,dl_id,choice=q.data.split(":",2)
    data=DL_CACHE.get(dl_id)
    if not data:
        return await q.edit_message_text("Request expired.")
    if q.from_user.id!=data["user"]:
        return await q.answer("This request does not belong to you.",show_alert=True)
    if choice=="cancel":
        DL_CACHE.pop(dl_id,None)
        return await q.edit_message_text("Cancelled.")
    fmt_map={
        "images":"slideshow_images",
        "video":"slideshow_video",
        "audio":"mp3",
    }
    fmt_key=fmt_map.get(choice)
    if not fmt_key:
        return await q.edit_message_text("Invalid slideshow option.")
    await q.edit_message_text(_metadata_status(data["url"]),parse_mode="HTML")
    DL_CACHE.pop(dl_id,None)
    return await _start_dl_task(
        context=context,
        message=q.message,
        data=data,
        fmt_key=fmt_key,
        format_id=None,
        has_audio=False,
        status_ready=True,
    )
    
async def dlres_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    q=update.callback_query
    if not q or not q.data:
        return
        
    _,dl_id,height_raw=q.data.split(":",2)
    data=DL_CACHE.get(dl_id)
    
    if not data:
        await q.answer()
        return await q.edit_message_text("Request expired.")
        
    if q.from_user.id!=data["user"]:
        return await q.answer("This request does not belong to you.",show_alert=True)
        
    try:
        height=int(height_raw)
    except (TypeError,ValueError):
        await q.answer()
        return await q.edit_message_text("Invalid resolution.")
        
    if height > 720 and not is_premium_user(q.from_user.id):
        return await q.answer("You are not a premium user! Upgrade to download resolutions above 720p.", show_alert=True)

    await q.answer()

    res_map=data.get("res_map") or {}
    picked=res_map.get(height)
    if not picked:
        return await q.edit_message_text("Resolution is no longer available.")
        
    engine=data.get("engine")
    DL_CACHE.pop(dl_id,None)
    
    log.info(
        "Resolution callback selected | url=%s height=%s format_id=%s has_audio=%s engine=%s picked=%s",
        data.get("url"),height,picked.get("format_id"),picked.get("has_audio"),engine,picked,
    )
    
    return await _start_dl_task(
        context=context,
        message=q.message,
        data=data,
        fmt_key="video",
        format_id=_format_id_for_engine(engine,height,picked),
        has_audio=bool(picked.get("has_audio")),
        label=f"{height}p",
        engine=engine,
    )

try:
    init_premium_db()
except Exception as e:
    log.warning("Premium DB init from downloader router failed | err=%r",e)
