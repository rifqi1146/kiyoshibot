import os,re,time,uuid,html,shutil,asyncio,aiohttp,aiofiles,logging
from urllib.parse import urlparse,urlunparse
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR
try:
    from handlers.dl.constants import BASE_DIR
except Exception:
    BASE_DIR=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from handlers.dl.utils import sanitize_filename,progress_bar
from handlers.dl.ytdlp import ytdlp_download
from PIL import Image
import subprocess

log=logging.getLogger(__name__)

DEBUG_REDDIT=True
REDDIT_COOKIES_PATH=os.path.abspath(os.path.join(BASE_DIR,"..","..","data","cookies.txt"))
_REDDIT_COOKIE_HEADER_CACHE=None
REDDIT_HOSTS={"reddit.com","www.reddit.com","old.reddit.com","new.reddit.com","redd.it"}
SHORT_RE=re.compile(r"https?://(?:(?:www|old|new)\.)?reddit\.com/r/[^/]+/s/[A-Za-z0-9]+/?",re.I)
COMMENTS_RE=re.compile(r"https?://(?:(?:www|old|new)\.)?reddit\.com/r/[^/]+/comments/([a-z0-9]+)/[^/?#]*/?",re.I)
COMMENTS_ANY_RE=re.compile(r"https?://(?:(?:www|old|new)\.)?reddit\.com/(?:r/[^/]+/)?comments/([a-z0-9]+)/[^?#]*/?",re.I)
REDDIT_HEADERS={
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":"application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":"en-US,en;q=0.9",
    "Referer":"https://www.reddit.com/",
}

def _dbg(msg:str,*args):
    if DEBUG_REDDIT: log.warning("RDDDBG | "+msg,*args)

def _clip(text:str,limit:int=300)->str:
    text=str(text or "").replace("\n","\\n").replace("\r","\\r")
    return text if len(text)<=limit else text[:limit]+"...<cut>"

def is_reddit_url(url:str)->bool:
    text=(url or "").strip()
    if not text: return False
    host=(urlparse(text).hostname or "").lower()
    return host in REDDIT_HOSTS or "reddit.com" in host or "redd.it" in host

def _build_reddit_media_headers()->dict:
    return {
        "User-Agent": REDDIT_HEADERS["User-Agent"],
        "Referer": "https://www.reddit.com/",
        "Accept": "image/jpeg,image/png,image/*;q=0.9,*/*;q=0.8",
    }
    
async def _safe_edit_status(bot,chat_id,status_msg_id,text:str):
    if not status_msg_id:
        return
    try:
        await bot.edit_message_text(chat_id=chat_id,message_id=status_msg_id,text=text,parse_mode="HTML",disable_web_page_preview=True)
    except Exception:
        pass

def _load_reddit_cookie_header(path:str)->str:
    global _REDDIT_COOKIE_HEADER_CACHE
    if _REDDIT_COOKIE_HEADER_CACHE is not None:
        return _REDDIT_COOKIE_HEADER_CACHE
    if not path or not os.path.exists(path):
        _dbg("reddit cookie file not found | path=%s",path)
        _REDDIT_COOKIE_HEADER_CACHE=""
        return _REDDIT_COOKIE_HEADER_CACHE
    pairs=[]
    try:
        with open(path,"r",encoding="utf-8",errors="ignore") as f:
            for raw in f:
                line=raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts=line.split("\t")
                if len(parts)>=7:
                    domain=(parts[0] or "").strip().lower()
                    name=(parts[5] or "").strip()
                    value=(parts[6] or "").strip()
                    if not name:
                        continue
                    if "reddit.com" not in domain:
                        continue
                    pairs.append(f"{name}={value}")
                    continue
        _REDDIT_COOKIE_HEADER_CACHE="; ".join(pairs)
        _dbg("reddit cookie loaded | path=%s pairs=%s",path,len(pairs))
        return _REDDIT_COOKIE_HEADER_CACHE
    except Exception as e:
        _dbg("reddit cookie load failed | path=%s err=%r",path,e)
        _REDDIT_COOKIE_HEADER_CACHE=""
        return _REDDIT_COOKIE_HEADER_CACHE

def _build_reddit_headers(referer:str|None=None)->dict:
    headers=dict(REDDIT_HEADERS)
    if referer:
        headers["Referer"]=referer
    cookie=_load_reddit_cookie_header(REDDIT_COOKIES_PATH)
    if cookie:
        headers["Cookie"]=cookie
    return headers
    
def _format_size(num_bytes:int)->str:
    if num_bytes<=0: return "0 B"
    value=float(num_bytes)
    for unit in ("B","KB","MB","GB","TB"):
        if value<1024 or unit=="TB": return f"{int(value)} {unit}" if unit=="B" else f"{value:.1f} {unit}"
        value/=1024
    return f"{value:.1f} TB"

def _format_speed(bytes_per_sec:float)->str:
    if bytes_per_sec<=0: return "0 B/s"
    value=float(bytes_per_sec)
    for unit in ("B/s","KB/s","MB/s","GB/s"):
        if value<1024 or unit=="GB/s": return f"{int(value)} {unit}" if unit=="B/s" else f"{value:.1f} {unit}"
        value/=1024
    return f"{value:.1f} GB/s"

def _format_eta(seconds:float)->str:
    if seconds<=0: return "0s"
    seconds=int(seconds); h,m,s=seconds//3600,(seconds%3600)//60,seconds%60
    if h>0: return f"{h}h {m}m {s}s"
    if m>0: return f"{m}m {s}s"
    return f"{s}s"

async def _safe_edit_progress(bot,chat_id,status_msg_id,title:str,downloaded:int,total:int=0,speed_bps:float=0.0,eta_seconds:float|None=None):
    if not status_msg_id:
        return
    pct=min(downloaded*100/total,100.0) if total>0 else 0.0
    lines=[f"<b>{html.escape(title)}</b>",""]
    if total>0:
        lines.append(f"<code>{html.escape(progress_bar(pct))}</code>")
        lines.append(f"<code>{html.escape(_format_size(downloaded))}/{html.escape(_format_size(total))} downloaded</code>")
    else:
        lines.append(f"<code>{html.escape(_format_size(downloaded))} downloaded</code>")
    if speed_bps>0: lines.append(f"<code>Speed: {html.escape(_format_speed(speed_bps))}</code>")
    if eta_seconds is not None and eta_seconds>=0 and total>0 and speed_bps>0: lines.append(f"<code>ETA: {html.escape(_format_eta(eta_seconds))}</code>")
    try:
        await bot.edit_message_text(chat_id=chat_id,message_id=status_msg_id,text="\n".join(lines),parse_mode="HTML",disable_web_page_preview=True)
    except Exception:
        pass

def _inspect_image_dims(path:str):
    try:
        with Image.open(path) as img:
            _dbg("image dims | path=%s width=%s height=%s mode=%s format=%s",path,img.size[0],img.size[1],img.mode,img.format)
    except Exception as e:
        _dbg("image dims inspect failed | path=%s err=%r",path,e)
        
def _extract_post_id(url:str)->str:
    text=(url or "").strip()
    m=COMMENTS_RE.search(text)
    if m: return (m.group(1) or "").strip()
    m=COMMENTS_ANY_RE.search(text)
    if m: return (m.group(1) or "").strip()
    return ""

async def _resolve_short_url(url:str)->str:
    session=await get_http_session()
    headers=_build_reddit_headers("https://www.reddit.com/")
    _dbg("short resolve start | url=%s cookie=%s",url,bool(headers.get("Cookie")))
    async with session.get(url,headers=headers,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
        final=str(resp.url)
        _dbg("short resolve done | status=%s final=%s",resp.status,final)
        return final

def _normalize_comments_url(url:str)->str:
    p=urlparse((url or "").strip())
    scheme=p.scheme or "https"
    netloc="www.reddit.com"
    path=p.path.rstrip("/")
    if not path:
        return ""
    if not path.endswith(".json"):
        path=path+"/.json"
    normalized=urlunparse((scheme,netloc,path,"","",""))
    _dbg("normalize comments url | before=%s after=%s",url,normalized)
    return normalized

async def _fetch_json_from_candidates(urls:list[str])->dict:
    session=await get_http_session()
    headers=_build_reddit_headers("https://www.reddit.com/")
    last_err=None
    for url in urls:
        try:
            _dbg("json fetch start | url=%s cookie=%s",url,bool(headers.get("Cookie")))
            async with session.get(url,headers=headers,timeout=aiohttp.ClientTimeout(total=30),allow_redirects=True) as resp:
                text=await resp.text()
                _dbg("json fetch done | status=%s url=%s body_len=%s",resp.status,url,len(text))
                if resp.status!=200:
                    last_err=RuntimeError(f"reddit http {resp.status}")
                    continue
                data=await resp.json(content_type=None)
                if isinstance(data,list) and data:
                    return data
                last_err=RuntimeError("invalid reddit json payload")
        except Exception as e:
            _dbg("json fetch failed | url=%s err=%r",url,e)
            last_err=e
    if last_err: raise last_err
    raise RuntimeError("reddit json fetch failed")

def _html_unescape_url(url:str)->str:
    return html.unescape((url or "").strip()).replace("&amp;","&")

def _pick_post_data(payload:list)->dict:
    if not isinstance(payload,list) or not payload:
        raise RuntimeError("invalid reddit payload")
    try:
        children=payload[0]["data"]["children"]
        if not children: raise RuntimeError("reddit post not found")
        post=children[0]["data"]
        if not isinstance(post,dict): raise RuntimeError("invalid reddit post data")
        return post
    except Exception as e:
        raise RuntimeError(f"failed parsing reddit post: {e}") from e

def _inspect_downloaded_file(path:str):
    try:
        size=os.path.getsize(path)
    except Exception as e:
        _dbg("file inspect size failed | path=%s err=%r",path,e)
        return
    try:
        with open(path,"rb") as f:
            sig=f.read(32)
        _dbg("file inspect | path=%s size=%s sig_hex=%s sig_text=%s",path,size,sig.hex(),repr(sig[:16]))
    except Exception as e:
        _dbg("file inspect read failed | path=%s err=%r",path,e)
        
def _safe_title(post:dict,items:list)->str:
    title=(post.get("title") or "").strip()
    if title: return title
    if len(items)==1 and str(items[0].get("type") or "").strip().lower()=="video": return "Reddit Video"
    return "Reddit Media"

def _gallery_order(post:dict)->list[str]:
    gallery=post.get("gallery_data") or {}
    out=[]
    for item in gallery.get("items") or []:
        media_id=(item.get("media_id") or "").strip()
        if media_id: out.append(media_id)
    return out

def _parse_gallery_items(post:dict)->list[dict]:
    media_meta=post.get("media_metadata") or {}
    if not isinstance(media_meta,dict) or not media_meta:
        return []
    order=_gallery_order(post)
    keys=order or list(media_meta.keys())
    items=[]
    for key in keys:
        meta=media_meta.get(key) or {}
        kind=(meta.get("e") or "").strip()
        if kind=="Image":
            src=_html_unescape_url((((meta.get("s") or {}).get("u")) or ""))
            if src: items.append({"type":"photo","url":src})
            continue
        if kind=="AnimatedImage":
            src=_html_unescape_url((((meta.get("s") or {}).get("mp4")) or ""))
            if src: items.append({"type":"video","url":src})
            continue
    return items

def _parse_preview_image(post:dict)->list[dict]:
    preview=post.get("preview") or {}
    images=preview.get("images") or []
    if not images: return []
    image=images[0] or {}
    src=_html_unescape_url((((image.get("source") or {}).get("url")) or ""))
    if src: return [{"type":"photo","url":src}]
    return []

def _parse_preview_video(post:dict)->list[dict]:
    preview=post.get("preview") or {}
    rv=preview.get("reddit_video_preview") or {}
    fallback=_html_unescape_url(rv.get("fallback_url") or "")
    if fallback: return [{"type":"video","url":fallback}]
    images=preview.get("images") or []
    if not images: return []
    variants=((images[0] or {}).get("variants") or {})
    mp4=((variants.get("mp4") or {}).get("source") or {}).get("url") or ""
    mp4=_html_unescape_url(mp4)
    if mp4: return [{"type":"video","url":mp4}]
    gif=((variants.get("gif") or {}).get("source") or {}).get("url") or ""
    gif=_html_unescape_url(gif)
    if gif: return [{"type":"video","url":gif}]
    return []

def _parse_reddit_video(post:dict)->list[dict]:
    media=post.get("media") or {}
    secure=post.get("secure_media") or {}
    rv=(media.get("reddit_video") or secure.get("reddit_video") or {})
    if rv:
        raise RuntimeError("Reddit DASH video requires yt-dlp for audio merging")
    return []

def _parse_direct_url(post:dict)->list[dict]:
    url=_html_unescape_url(post.get("url_overridden_by_dest") or post.get("url") or "")
    if not url: return []
    low=url.lower()
    if any(low.endswith(ext) for ext in (".jpg",".jpeg",".png",".webp")):
        return [{"type":"photo","url":url}]
    if any(low.endswith(ext) for ext in (".mp4",".mov",".webm",".gif")):
        return [{"type":"video","url":url}]
    return []

def _parse_reddit_post(post:dict)->dict:
    items=[]
    gallery_items=_parse_gallery_items(post)
    if gallery_items: items=gallery_items
    if not items:
        direct_video=_parse_reddit_video(post)
        if direct_video: items=direct_video
    if not items:
        preview_video=_parse_preview_video(post)
        if preview_video: items=preview_video
    if not items:
        direct_url=_parse_direct_url(post)
        if direct_url: items=direct_url
    if not items:
        preview_image=_parse_preview_image(post)
        if preview_image: items=preview_image
    if not items: raise RuntimeError("reddit post has no downloadable media")
    title=_safe_title(post,items)
    _dbg("reddit parsed | title=%s count=%s types=%s",_clip(title,120),len(items),[x.get("type") for x in items])
    return {"items":items,"title":title}

async def _probe_total_bytes(session,url:str,headers:dict|None=None)->int:
    total=0
    try:
        async with session.head(url,headers=headers,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
            total=int(resp.headers.get("Content-Length",0) or 0)
    except Exception:
        total=0
    if total>0: return total
    try:
        h=dict(headers or {}); h["Range"]="bytes=0-0"
        async with session.get(url,headers=h,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
            m=re.search(r"/(\d+)$",resp.headers.get("Content-Range",""))
            if m: return int(m.group(1))
    except Exception:
        pass
    return 0

async def _aria2c_download_with_progress(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    aria2=shutil.which("aria2c")
    if not aria2: raise RuntimeError("aria2c not found in PATH")
    total=await _probe_total_bytes(session,media_url,headers=headers)
    out_dir=os.path.dirname(out_path) or "."
    out_name=os.path.basename(out_path)
    cmd=[aria2,"--dir",out_dir,"--out",out_name,"--file-allocation=none","--allow-overwrite=true","--auto-file-renaming=false","--continue=true","--max-connection-per-server=8","--split=8","--min-split-size=1M","--summary-interval=0","--download-result=hide","--console-log-level=warn"]
    for k,v in (headers or {}).items():
        if v: cmd.extend(["--header",f"{k}: {v}"])
    cmd.append(media_url)
    log.info("Reddit aria2c start | url=%s out=%s",media_url,out_path)
    log.debug("Reddit aria2c cmd | %s"," ".join(cmd))
    proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
    last_edit=-10.0; last_sample_size=0; last_sample_ts=time.time()
    while proc.returncode is None:
        await asyncio.sleep(0.7)
        if not os.path.exists(out_path): continue
        try: downloaded=os.path.getsize(out_path)
        except Exception: continue
        if downloaded<=0: continue
        now=time.time(); elapsed=max(now-last_sample_ts,0.001); speed_bps=max(downloaded-last_sample_size,0)/elapsed
        eta_seconds=((total-downloaded)/speed_bps) if total>0 and speed_bps>0 and downloaded<=total else None
        if now-last_edit<3 and last_edit>=0: continue
        await _safe_edit_progress(bot,chat_id,status_msg_id,title_text,downloaded,total,speed_bps,eta_seconds)
        last_edit=now; last_sample_size=downloaded; last_sample_ts=now
    _,stderr=await proc.communicate()
    stderr_text=stderr.decode(errors="ignore").strip() if stderr else ""
    if stderr_text: log.debug("Reddit aria2c stderr | %s",stderr_text)
    if proc.returncode!=0: raise RuntimeError(stderr_text or f"aria2c exited with code {proc.returncode}")
    log.info("Reddit aria2c success | out=%s",out_path)

async def _aiohttp_download_with_progress(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    async with session.get(media_url,headers=headers,timeout=aiohttp.ClientTimeout(total=600),allow_redirects=True) as r:
        if r.status>=400: raise RuntimeError(f"Download failed: HTTP {r.status}")
        total=int(r.headers.get("Content-Length",0) or 0); downloaded=0; last_edit=-10.0; last_sample_size=0; last_sample_ts=time.time()
        async with aiofiles.open(out_path,"wb") as f:
            async for chunk in r.content.iter_chunked(64*1024):
                if not chunk: continue
                await f.write(chunk); downloaded+=len(chunk)
                now=time.time(); elapsed=max(now-last_sample_ts,0.001); speed_bps=max(downloaded-last_sample_size,0)/elapsed
                eta_seconds=((total-downloaded)/speed_bps) if total>0 and speed_bps>0 and downloaded<=total else None
                if now-last_edit<3 and last_edit>=0: continue
                await _safe_edit_progress(bot,chat_id,status_msg_id,title_text,downloaded,total,speed_bps,eta_seconds)
                last_edit=now; last_sample_size=downloaded; last_sample_ts=now

def _guess_ext(media_type:str,url:str)->str:
    low=(url or "").lower()
    if media_type=="video":
        if ".mp4" in low: return ".mp4"
        if ".webm" in low: return ".webm"
        if ".mov" in low: return ".mov"
        if ".m3u8" in low: return ".mp4"
        return ".mp4"
    if ".png" in low: return ".png"
    if ".webp" in low: return ".webp"
    if ".jpeg" in low: return ".jpeg"
    return ".jpg"

def _fix_photo_for_telegram(path:str)->str:
    fixed_path=os.path.splitext(path)[0]+"_tg.jpg"
    max_side=1280
    min_side=32
    max_ratio=20
    try:
        with Image.open(path) as img:
            img=img.convert("RGB")
            w,h=img.size
            if w<=0 or h<=0:
                raise RuntimeError("invalid image size")
            ratio=max(w,h)/max(1,min(w,h))
            if ratio>max_ratio:
                if w>h:
                    h=max(min_side,int(w/max_ratio))
                else:
                    w=max(min_side,int(h/max_ratio))
                img=img.resize((w,h),Image.LANCZOS)
            if max(w,h)>max_side:
                if w>=h:
                    nh=max(min_side,int(h*max_side/w))
                    nw=max_side
                else:
                    nw=max(min_side,int(w*max_side/h))
                    nh=max_side
                img=img.resize((nw,nh),Image.LANCZOS)
            if min(img.size)<min_side:
                w,h=img.size
                if w<h:
                    nw=min_side
                    nh=max(min_side,int(h*(min_side/w)))
                else:
                    nh=min_side
                    nw=max(min_side,int(w*(min_side/h)))
                img=img.resize((nw,nh),Image.LANCZOS)
            img.save(fixed_path,format="JPEG",quality=92,optimize=True)
        return fixed_path
    except Exception:
        result=subprocess.run(
            ["ffmpeg","-y","-i",path,"-vf","scale='min(1280,iw)':-2","-frames:v","1","-q:v","2",fixed_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode!=0 or not os.path.exists(fixed_path):
            raise RuntimeError("failed to convert reddit image for telegram")
        return fixed_path

async def _download_one_media(session,item:dict,bot,chat_id,status_msg_id,idx:int,total:int)->dict:
    media_type=str(item.get("type") or "").strip().lower()
    media_url=str(item.get("url") or "").strip()
    if not media_url: raise RuntimeError("media url kosong")
    ext=_guess_ext(media_type,media_url)
    title="Reddit Video" if media_type=="video" else "Reddit Media"
    out_path=os.path.join(TMP_DIR,f"{uuid.uuid4().hex}_{sanitize_filename(title)}{ext}")
    headers=_build_reddit_media_headers()
    title_text=f"Downloading {'Reddit Video' if media_type=='video' else 'Reddit Media'}... ({idx}/{total})"
    try:
        await _aria2c_download_with_progress(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
    except Exception as e:
        log.warning("Reddit aria2c failed, fallback aiohttp | idx=%s url=%s err=%r",idx,media_url,e)
        if os.path.exists(out_path):
            try: os.remove(out_path)
            except Exception: pass
        await _aiohttp_download_with_progress(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
    if media_type=="photo":
        _inspect_downloaded_file(out_path)
        _inspect_image_dims(out_path)
        try:
            out_path=await asyncio.to_thread(_fix_photo_for_telegram,out_path)
            _inspect_image_dims(out_path)
        except Exception as e:
            log.warning("Reddit image normalize failed, redownload with aiohttp | path=%s err=%r",out_path,e)
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except Exception:
                pass
            await _aiohttp_download_with_progress(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
            _inspect_downloaded_file(out_path)
            _inspect_image_dims(out_path)
            out_path=await asyncio.to_thread(_fix_photo_for_telegram,out_path)
            _inspect_image_dims(out_path)
    return {"type":media_type if media_type in {"video","photo"} else "photo","path":out_path,"url":media_url}

async def _download_reddit_items(parsed:dict,bot,chat_id,status_msg_id)->dict:
    items=parsed.get("items") or []
    title=(parsed.get("title") or "").strip() or ("Reddit Video" if len(items)==1 and str(items[0].get("type") or "").strip().lower()=="video" else "Reddit Media")
    session=await get_http_session()
    downloaded_items=[]; total=len(items)
    for idx,item in enumerate(items,start=1):
        downloaded=await _download_one_media(session,item,bot,chat_id,status_msg_id,idx,total)
        downloaded_items.append(downloaded)
    if len(downloaded_items)==1:
        return {"path":downloaded_items[0]["path"],"title":title}
    return {"items":downloaded_items,"title":title}

async def reddit_scrape_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    del fmt_key,format_id,has_audio
    if not metadata_ready:
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Scraping Reddit media...</b>")
    text=(raw_url or "").strip()
    if SHORT_RE.match(text):
        resolved=await _resolve_short_url(text)
        if resolved==text or not _extract_post_id(resolved):
            raise RuntimeError("failed to resolve reddit short url")
        text=resolved
    comments_url=_normalize_comments_url(text)
    if not comments_url:
        raise RuntimeError("failed to normalize reddit url")
    urls=[
        comments_url,
        comments_url.replace("www.reddit.com","old.reddit.com"),
        comments_url.replace("www.reddit.com","new.reddit.com"),
    ]
    payload=await _fetch_json_from_candidates(urls)
    post=_pick_post_data(payload)
    parsed=_parse_reddit_post(post)
    result=await _download_reddit_items(parsed,bot,chat_id,status_msg_id)
    _dbg("reddit scrape success | result_type=%s","album" if isinstance(result,dict) and result.get("items") else "single")
    return result

async def reddit_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    try:
        return await reddit_scrape_download(
            raw_url=raw_url,
            fmt_key=fmt_key,
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            format_id=format_id,
            has_audio=has_audio,
            metadata_ready=metadata_ready,
        )
    except Exception as e:
        log.exception("Reddit scraping failed, fallback to yt-dlp | url=%s err=%r",raw_url,e)
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Reddit scraping failed</b>\n\n<i>Fallback to yt-dlp...</i>")
        return await ytdlp_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio)
        