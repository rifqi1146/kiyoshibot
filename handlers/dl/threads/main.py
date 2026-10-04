import os
import re
import time
import uuid
import html
import shutil
import asyncio
import aiohttp
import aiofiles
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from telegram.error import RetryAfter
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR,MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename,format_size,check_media_size_limit,FileSizeLimitExceeded
from handlers.dl.progress import edit_status, TransferStats
from handlers.dl.ytdlp import ytdlp_download

log=logging.getLogger(__name__)

THREADS_DOMAIN_RE=re.compile(r"https?://(?:www\.)?threads\.(?:com|net)",re.I)
THREADS_POST_RE=re.compile(r"/(?:p|post)/([A-Za-z0-9_-]+)",re.I)

DEBUG_THREADS=os.getenv("THREADS_DEBUG","0").lower() in ("1","true","on","yes")
THREADS_PROGRESS_INTERVAL=float(os.getenv("THREADS_PROGRESS_INTERVAL","3"))
THREADS_HEADERS={
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language":"en-GB,en;q=0.9",
    "Cache-Control":"max-age=0",
    "Dnt":"1",
    "Priority":"u=0, i",
    "Sec-Ch-Ua":'"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile":"?0",
    "Sec-Ch-Ua-Platform":'"macOS"',
    "Sec-Fetch-Dest":"document",
    "Sec-Fetch-Mode":"navigate",
    "Sec-Fetch-Site":"none",
    "Sec-Fetch-User":"?1",
    "Upgrade-Insecure-Requests":"1",
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

def _dbg(msg:str,*args):
    if DEBUG_THREADS:
        log.warning("THREADSDBG | "+msg,*args)

def _clip(text:str,limit:int=300)->str:
    text=str(text or "").replace("\n","\\n").replace("\r","\\r")
    return text if len(text)<=limit else text[:limit]+"...<cut>"

def is_threads_url(url:str)->bool:
    return bool(THREADS_DOMAIN_RE.search((url or "").strip()))

def _extract_threads_post_id(url:str)->str:
    m=THREADS_POST_RE.search((url or "").strip())
    return (m.group(1) or "").strip() if m else ""

def _normalize_media_url(src:str)->str:
    src=(src or "").strip()
    if not src:
        return ""
    if src.startswith("//"):
        return "https:"+src
    return src

def _safe_remove_file(path:str|None,context:str=""):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("Threads temp deleted | context=%s file=%s",context,os.path.basename(path))
    except Exception as e:
        log.warning("Failed to remove Threads temp | context=%s path=%s err=%r",context,path,e)

def _guess_ext_from_url(url:str,media_type:str)->str:
    path=(urlparse(url or "").path or "").lower()
    for ext in (".mp4",".mov",".m4v",".webm",".jpg",".jpeg",".png",".webp"):
        if path.endswith(ext):
            return ext
    return ".mp4" if media_type=="video" else ".jpg"

async def _safe_edit_status(bot,chat_id,status_msg_id,text:str,min_interval:float=1.2):
    await edit_status(bot,chat_id,status_msg_id,text,min_interval=min_interval,label="Threads")

async def _fetch_threads_embed_html(post_id:str)->bytes:
    embed_url=f"https://www.threads.net/@_/post/{post_id}/embed"
    session=await get_http_session()
    _dbg("fetch embed start | post_id=%s url=%s",post_id,embed_url)
    async with session.get(embed_url,headers=THREADS_HEADERS,timeout=aiohttp.ClientTimeout(total=30),allow_redirects=True) as resp:
        body=await resp.read()
        _dbg("fetch embed done | status=%s final=%s body_len=%s",resp.status,str(resp.url),len(body))
        if resp.status!=200:
            raise RuntimeError(f"failed to get embed media: HTTP {resp.status}")
        return body

def _parse_threads_embed_media(body:bytes)->dict:
    if b"Thread not available" in body:
        raise RuntimeError("Thread not available")
    soup=BeautifulSoup(body,"html.parser")
    result={"caption":"","items":[]}
    caption_el=soup.select_one(".BodyTextContainer")
    if caption_el:
        result["caption"]=html.unescape(caption_el.get_text(" ",strip=True) or "").strip()
    seen=set()
    blocked_imgs=set()
    for container in soup.select(".MediaContainer, .SoloMediaContainer"):
        for vid in container.select("video"):
            source=vid.select_one("source")
            src=_normalize_media_url((source.get("src","") if source else "") or vid.get("src",""))
            poster=_normalize_media_url(vid.get("poster",""))
            if poster:
                blocked_imgs.add(poster)
            if not src or src in seen:
                continue
            seen.add(src)
            result["items"].append({"type":"video","url":src})
        for img in container.select("img"):
            src=_normalize_media_url(img.get("src",""))
            if not src or src in seen or src in blocked_imgs:
                continue
            seen.add(src)
            result["items"].append({"type":"photo","url":src})
    _dbg("parse embed done | caption=%s items=%s types=%s",bool(result["caption"]),len(result["items"]),[x.get("type") for x in result["items"]])
    if not result["items"]:
        raise RuntimeError("no media found in threads embed")
    return result

async def _probe_total_bytes(session,url:str,headers:dict|None=None)->int:
    try:
        async with session.head(url,headers=headers,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
            total=int(resp.headers.get("Content-Length",0) or 0)
            if total>0:
                return total
            log.debug("Threads HEAD size empty | status=%s url=%s",resp.status,_clip(url,160))
    except Exception as e:
        log.debug("Threads HEAD size probe failed | url=%s err=%r",_clip(url,160),e)
    try:
        h=dict(headers or {})
        h["Range"]="bytes=0-0"
        async with session.get(url,headers=h,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
            content_range=resp.headers.get("Content-Range","")
            m=re.search(r"/(\d+)$",content_range)
            if m:
                return int(m.group(1))
            total=int(resp.headers.get("Content-Length",0) or 0)
            if total>0:
                return total
            log.debug("Threads Range size empty | status=%s url=%s",resp.status,_clip(url,160))
    except Exception as e:
        log.debug("Threads Range size probe failed | url=%s err=%r",_clip(url,160),e)
    return 0

async def _aria2c_download_with_progress(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    aria2=shutil.which("aria2c")
    if not aria2:
        raise RuntimeError("aria2c not found in PATH")
    total=await _probe_total_bytes(session,media_url,headers=headers)
    if total:
        check_media_size_limit(total,"Threads media")
    out_dir=os.path.dirname(out_path) or "."
    out_name=os.path.basename(out_path)
    cmd=[
        aria2,"--dir",out_dir,"--out",out_name,"--file-allocation=none","--allow-overwrite=true",
        "--auto-file-renaming=false","--continue=true","--max-connection-per-server=16","--split=16",
        "--min-split-size=1M","--summary-interval=0","--download-result=hide","--console-log-level=warn",
    ]
    for k,v in (headers or {}).items():
        if v:
            cmd.extend(["--header",f"{k}: {v}"])
    cmd.append(media_url)
    log.info("Threads aria2c start | out=%s total=%s",out_path,format_size(total))
    _dbg("aria2c start | out=%s url=%s",out_path,_clip(media_url,200))
    proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
    stats=TransferStats(total)
    while proc.returncode is None:
        await asyncio.sleep(0.7)
        if not os.path.exists(out_path):
            continue
        try:
            downloaded=os.path.getsize(out_path)
        except Exception as e:
            log.debug("Threads aria2c size read failed | file=%s err=%r",out_path,e)
            continue
        if downloaded<=0:
            continue
        if downloaded>MAX_TG_SIZE:
            try: proc.kill()
            except Exception: pass
            raise FileSizeLimitExceeded("Threads media exceeds 2GB limit. Download canceled.")
        await stats.update(downloaded,bot,chat_id,status_msg_id,title_text,kind="Threads download",label=os.path.basename(out_path),log_interval=2.5,edit_interval=THREADS_PROGRESS_INTERVAL)
    _,stderr=await proc.communicate()
    stderr_text=stderr.decode(errors="ignore").strip() if stderr else ""
    if stderr_text:
        log.debug("Threads aria2c stderr | %s",stderr_text)
    if proc.returncode!=0:
        _dbg("aria2c failed | code=%s err=%s",proc.returncode,_clip(stderr_text,500))
        raise RuntimeError(stderr_text or f"aria2c exited with code {proc.returncode}")
    if not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
        raise RuntimeError("aria2c download output empty")
    stats.log_done("Threads download",label=os.path.basename(out_path),size=os.path.getsize(out_path))
    log.info("Threads aria2c success | file=%s size=%s",out_path,format_size(os.path.getsize(out_path)))

async def _aiohttp_download_with_progress(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    _dbg("aiohttp fallback start | out=%s url=%s",out_path,_clip(media_url,200))
    async with session.get(media_url,headers=headers,timeout=aiohttp.ClientTimeout(total=600),allow_redirects=True) as r:
        _dbg("aiohttp fallback response | status=%s final=%s",r.status,str(r.url))
        if r.status>=400:
            raise RuntimeError(f"Download failed: HTTP {r.status}")
        total=int(r.headers.get("Content-Length",0) or 0)
        if total:
            check_media_size_limit(total,"Threads media")
        downloaded=0
        stats=TransferStats(total)
        async with aiofiles.open(out_path,"wb") as f:
            async for chunk in r.content.iter_chunked(512*1024):
                if not chunk:
                    continue
                await f.write(chunk)
                downloaded+=len(chunk)
                if downloaded>MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("Threads media exceeds 2GB limit. Download canceled.")
                await stats.update(downloaded,bot,chat_id,status_msg_id,title_text,kind="Threads download",label=os.path.basename(out_path),log_interval=2.5,edit_interval=THREADS_PROGRESS_INTERVAL)
    if not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
        raise RuntimeError("aiohttp download output empty")
    stats.log_done("Threads download",label=os.path.basename(out_path),size=os.path.getsize(out_path))
    log.info("Threads aiohttp success | file=%s size=%s",out_path,format_size(os.path.getsize(out_path)))

async def _download_one_media(session,item:dict,bot,chat_id,status_msg_id,idx:int,total:int)->dict:
    media_type=str(item.get("type") or "").strip().lower()
    media_type=media_type if media_type in ("video","photo") else "photo"
    media_url=str(item.get("url") or "").strip()
    if not media_url:
        raise RuntimeError("media url kosong")
    ext=_guess_ext_from_url(media_url,media_type)
    out_path=os.path.join(TMP_DIR,f"{uuid.uuid4().hex}_{sanitize_filename(media_type or 'media')}{ext}")
    headers={"User-Agent":THREADS_HEADERS["User-Agent"],"Referer":"https://www.threads.net/"}
    title_text="Downloading Threads video..." if media_type=="video" else "Downloading Threads image..."
    try:
        await _aria2c_download_with_progress(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
    except FileSizeLimitExceeded:
        _safe_remove_file(out_path,"threads too large")
        raise
    except Exception as e:
        log.warning("Threads aria2c failed, fallback aiohttp | index=%s/%s url=%s err=%r",idx,total,_clip(media_url,180),e)
        _safe_remove_file(out_path,"threads aria2c partial")
        await _aiohttp_download_with_progress(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
    return {"type":media_type,"path":out_path,"url":media_url}

def _pick_items_for_format(items:list[dict],fmt_key:str)->list[dict]:
    if fmt_key=="mp3":
        for item in items:
            if str(item.get("type") or "").lower()=="video":
                return [item]
        raise RuntimeError("Threads post does not contain video/audio")
    return items

async def _download_threads_items(parsed:dict,fmt_key:str,bot,chat_id,status_msg_id)->dict:
    items=_pick_items_for_format(parsed.get("items") or [],fmt_key)
    caption=(parsed.get("caption") or "").strip()
    title=caption or "Threads Post"
    session=await get_http_session()
    downloaded_items=[]
    total=len(items)
    try:
        for idx,item in enumerate(items,start=1):
            await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Downloading Threads media...</b>")
            downloaded=await _download_one_media(session,item,bot,chat_id,status_msg_id,idx,total)
            downloaded_items.append(downloaded)
        if len(downloaded_items)==1:
            only=downloaded_items[0]
            return {"path":only["path"],"title":title,"source":"Threads"}
        return {"items":downloaded_items,"title":title,"desc":caption,"source":"Threads"}
    except Exception:
        for item in downloaded_items:
            _safe_remove_file(item.get("path"),"threads download failed cleanup")
        raise

async def threads_scrape_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    del format_id,has_audio
    if not metadata_ready:
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Scraping Threads post...</b>")
        
    post_id = _extract_threads_post_id(raw_url)
    
    if not post_id:
        try:
            session = await get_http_session()
            async with session.get(raw_url, headers=THREADS_HEADERS, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resolved_url = str(resp.url)
                post_id = _extract_threads_post_id(resolved_url)
        except Exception as e:
            log.warning("Failed to resolve threads share URL | url=%s err=%r", raw_url, e)

    _dbg("threads scrape start | url=%s post_id=%s",raw_url,post_id)
    if not post_id:
        raise RuntimeError("failed to extract threads post id")
        
    body=await _fetch_threads_embed_html(post_id)
    parsed=_parse_threads_embed_media(body)
    _dbg("threads parsed | items=%s caption=%s",len(parsed.get("items") or []),bool(parsed.get("caption")))
    result=await _download_threads_items(parsed,fmt_key,bot,chat_id,status_msg_id)
    _dbg("threads scrape success | result_type=%s","album" if isinstance(result,dict) and result.get("items") else "single")
    return result

async def threads_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    try:
        return await threads_scrape_download(raw_url=raw_url,fmt_key=fmt_key,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,format_id=format_id,has_audio=has_audio,metadata_ready=metadata_ready)
    except Exception as e:
        if isinstance(e, FileSizeLimitExceeded):
            raise
        log.exception("Threads scraping failed, fallback to yt-dlp | url=%s err=%r",raw_url,e)
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Threads scraping failed</b>\n\n<i>Fallback to yt-dlp...</i>")
        return await ytdlp_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio)
