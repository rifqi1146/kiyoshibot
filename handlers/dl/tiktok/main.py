import os
import re
import time
import uuid
import html
import shutil
import json
import hashlib
import base64
import asyncio
import aiohttp
import aiofiles
import logging
import subprocess
from telegram.error import RetryAfter
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR
from handlers.dl.utils import sanitize_filename,is_invalid_video,progress_bar
from utils.config import LOG_CHAT_ID
from .fallback import _tikwm_result

log=logging.getLogger(__name__)
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
WEB_HEADERS={
    "User-Agent":USER_AGENT,
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":"en-US,en;q=0.9",
    "Sec-Fetch-Mode":"navigate",
}
UNIVERSAL_RE=re.compile(r'<script[^>]+\bid="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',re.S|re.I)
SIGI_RE=re.compile(r'<script[^>]+\bid="SIGI_STATE"[^>]*>(.*?)</script>',re.S|re.I)
NEXT_RE=re.compile(r'<script[^>]+\bid="__NEXT_DATA__"[^>]*>(.*?)</script>',re.S|re.I)
MODERN_SSR_RE=re.compile(r'<script[^>]+\bid="__MODERN_SSR_DATA__"[^>]*>(.*?)</script>',re.S|re.I)
SHORT_TIKTOK_RE=re.compile(r"https?://(?:vm|vt)\.tiktok\.com/",re.I)

try:
    from handlers.dl.constants import BASE_DIR
except Exception as e:
    BASE_DIR=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    log.warning("BASE_DIR fallback used for TikTok downloader | err=%r",e)

TIKTOK_COOKIES_PATH=os.path.abspath(os.path.join(BASE_DIR,"..","..","data","cookies.txt"))
TIKTOK_COOKIE_DOMAINS=("tiktok.com","tiktokv.com","byteoversea.com","ibyteimg.com","muscdn.com","tikwm.com")
_TIKTOK_COOKIE_HEADER_CACHE=None
USE_GOVD_FAST=os.getenv("TIKTOK_GOVD_FAST","1").lower() not in ("0","false","off","no")
USE_SCRAPLING=os.getenv("TIKTOK_USE_SCRAPLING","1").lower() not in ("0","false","off","no")
DEBUG_TIKTOK_LOG=os.getenv("TIKTOK_DEBUG","0").lower() in ("1","true","on","yes")
DEBUG_TIKTOK_DUMP=os.getenv("TIKTOK_DEBUG_DUMP","0").lower() in ("1","true","on","yes")
TIKTOK_DOWNLOAD_ENGINE=os.getenv("TIKTOK_DOWNLOAD_ENGINE","aria2c").lower()
TIKTOK_PROGRESS=os.getenv("TIKTOK_PROGRESS","1").lower() in ("1","true","on","yes")
TIKTOK_PROGRESS_INTERVAL=float(os.getenv("TIKTOK_PROGRESS_INTERVAL","1.5"))
TIKTOK_AIOHTTP_CHUNK_SIZE=int(os.getenv("TIKTOK_AIOHTTP_CHUNK_SIZE",str(256*1024)))
TIKTOK_ALBUM_CHUNK_SIZE=int(os.getenv("TIKTOK_ALBUM_CHUNK_SIZE",str(256*1024)))
ARIA2C_TIMEOUT=int(os.getenv("TIKTOK_ARIA2C_TIMEOUT","600"))
AIOHTTP_DOWNLOAD_TIMEOUT=int(os.getenv("TIKTOK_AIOHTTP_TIMEOUT","600"))

TIKTOK_SLIDESHOW_IMAGE_DURATION=float(os.getenv("TIKTOK_SLIDESHOW_IMAGE_DURATION","4.0"))
TIKTOK_SLIDESHOW_LOOP_IMAGES=os.getenv("TIKTOK_SLIDESHOW_LOOP_IMAGES","1").lower() in ("1","true","on","yes")
TIKTOK_SLIDESHOW_WIDTH=int(os.getenv("TIKTOK_SLIDESHOW_WIDTH","720"))
TIKTOK_SLIDESHOW_HEIGHT=int(os.getenv("TIKTOK_SLIDESHOW_HEIGHT","1280"))
TIKTOK_SLIDESHOW_FPS=int(os.getenv("TIKTOK_SLIDESHOW_FPS","30"))
TIKTOK_SLIDESHOW_TRANSITION=os.getenv("TIKTOK_SLIDESHOW_TRANSITION","slideleft").strip() or "slideleft"
TIKTOK_SLIDESHOW_TRANSITION_DURATION=float(os.getenv("TIKTOK_SLIDESHOW_TRANSITION_DURATION","0.6"))
TIKTOK_SLIDESHOW_MIN_IMAGE_DURATION=float(os.getenv("TIKTOK_SLIDESHOW_MIN_IMAGE_DURATION","0.6"))
TIKTOK_SLIDESHOW_SYNC_AUDIO=os.getenv("TIKTOK_SLIDESHOW_SYNC_AUDIO","1").lower() in ("1","true","on","yes")

def _decode_tt_base64(val: str) -> bytes:
    padding = (4 - len(val) % 4) % 4
    return base64.b64decode(val + ("=" * padding))

def _solve_tt_challenge(html_text: str) -> str:
    wci_match = re.search(r'(?is)<[^>]+\bid="wci"[^>]*\bclass="([^"]*)"', html_text)
    cs_match = re.search(r'(?is)<[^>]+\bid="cs"[^>]*\bclass="([^"]*)"', html_text)
    rci_match = re.search(r'(?is)<[^>]+\bid="rci"[^>]*\bclass="([^"]*)"', html_text)
    rs_match = re.search(r'(?is)<[^>]+\bid="rs"[^>]*\bclass="([^"]*)"', html_text)
    
    if not wci_match or not cs_match:
        return ""
        
    chal_name = wci_match.group(1).strip()
    chal_enc = cs_match.group(1).strip()
    
    try:
        chal_bytes = _decode_tt_base64(chal_enc)
        chal_data = json.loads(chal_bytes)
        
        v = chal_data.get("v", {})
        base_val = _decode_tt_base64(v.get("a", ""))
        expected_digest = _decode_tt_base64(v.get("c", ""))
        
        solution = ""
        for i in range(1_000_000 + 1):
            candidate = base_val + str(i).encode('utf-8')
            if hashlib.sha256(candidate).digest() == expected_digest:
                solution = str(i)
                break
                
        if not solution:
            return ""
            
        chal_data["d"] = base64.b64encode(solution.encode('utf-8')).decode('utf-8')
        chal_cookie_val = base64.b64encode(json.dumps(chal_data, separators=(',', ':')).encode('utf-8')).decode('utf-8')
        
        cookies = [f"{chal_name}={chal_cookie_val}"]
        
        if rci_match and rs_match:
            rci = rci_match.group(1).strip()
            rs = rs_match.group(1).strip()
            if rci:
                cookies.append(f"{rci}={rs}")
                
        return "; ".join(cookies)
    except Exception as e:
        log.warning("Failed to solve TikTok challenge | err=%r", e)
        return ""

def _ttdbg(msg:str,*args):
    if DEBUG_TIKTOK_LOG:
        log.warning("TTDBG | "+msg,*args)

def _safe_remove_file(path:str|None,label:str):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("TikTok temp deleted | label=%s file=%s",label,os.path.basename(path))
    except Exception as e:
        log.warning("Failed to delete TikTok temp | label=%s path=%s err=%r",label,path,e)

async def _kill_process(proc,label:str):
    if not proc or proc.returncode is not None:
        return
    try:
        proc.kill()
        await proc.wait()
        log.warning("%s process killed",label)
    except ProcessLookupError:
        return
    except Exception as e:
        log.warning("Failed to kill %s process | err=%r",label,e)

def _write_debug_file(prefix:str,content:str|bytes,ext:str="txt")->str:
    try:
        os.makedirs(TMP_DIR,exist_ok=True)
        path=os.path.join(TMP_DIR,f"{prefix}_{uuid.uuid4().hex}.{ext}")
        if isinstance(content,(bytes,bytearray)):
            with open(path,"wb") as f:
                f.write(content)
        else:
            with open(path,"w",encoding="utf-8",errors="ignore") as f:
                f.write(content)
        _ttdbg("debug file written | path=%s",path)
        return path
    except Exception as e:
        _ttdbg("debug file write failed | prefix=%s err=%r",prefix,e)
        return ""

def _truncate_text(text:str,limit:int)->str:
    text=(text or "").strip()
    if limit<=0:
        return ""
    if len(text)<=limit:
        return text
    if limit<=3:
        return "."*limit
    return text[:limit-3].rstrip()+"..."

def _load_tiktok_cookie_header(path:str)->str:
    global _TIKTOK_COOKIE_HEADER_CACHE
    if _TIKTOK_COOKIE_HEADER_CACHE is not None:
        return _TIKTOK_COOKIE_HEADER_CACHE
    if not path or not os.path.exists(path):
        _ttdbg("tiktok cookie file not found | path=%s",path)
        _TIKTOK_COOKIE_HEADER_CACHE=""
        return _TIKTOK_COOKIE_HEADER_CACHE
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
                    if name and any(d in domain for d in TIKTOK_COOKIE_DOMAINS):
                        pairs.append(f"{name}={value}")
                    continue
                if "=" in line and "\t" not in line and not line.lower().startswith(("http://","https://")):
                    name,value=line.split("=",1)
                    name=name.strip()
                    value=value.strip()
                    if name:
                        pairs.append(f"{name}={value}")
        _TIKTOK_COOKIE_HEADER_CACHE="; ".join(pairs)
        _ttdbg("tiktok cookie loaded | path=%s pairs=%s",path,len(pairs))
        return _TIKTOK_COOKIE_HEADER_CACHE
    except Exception as e:
        _ttdbg("tiktok cookie load failed | path=%s err=%r",path,e)
        _TIKTOK_COOKIE_HEADER_CACHE=""
        return _TIKTOK_COOKIE_HEADER_CACHE

def _build_tiktok_headers(referer:str|None=None,extra_cookie:str|None=None)->dict:
    headers=dict(WEB_HEADERS)
    if referer:
        headers["Referer"]=referer
    cookie_parts=[]
    file_cookie=_load_tiktok_cookie_header(TIKTOK_COOKIES_PATH)
    if file_cookie:
        cookie_parts.append(file_cookie)
    if extra_cookie:
        cookie_parts.append(extra_cookie)
    if cookie_parts:
        headers["Cookie"]="; ".join(x for x in cookie_parts if x)
    return headers

def _merge_cookie_headers(*cookie_values:str)->str:
    jar={}
    for cookie_value in cookie_values:
        text=str(cookie_value or "").strip()
        if not text:
            continue
        for part in text.split(";"):
            kv=part.strip()
            if not kv or "=" not in kv:
                continue
            name,value=kv.split("=",1)
            name=name.strip()
            value=value.strip()
            if name:
                jar[name]=value
    return "; ".join(f"{k}={v}" for k,v in jar.items())

def _cookie_header(cookies:list[dict]|None)->str:
    if not cookies:
        return ""
    parts=[]
    for c in cookies:
        name=str((c or {}).get("name") or "").strip()
        value=str((c or {}).get("value") or "").strip()
        if name:
            parts.append(f"{name}={value}")
    return "; ".join(parts)

def _cookies_from_header(cookie_header:str)->list[dict]:
    out=[]
    for part in str(cookie_header or "").split(";"):
        kv=part.strip()
        if not kv or "=" not in kv:
            continue
        name,value=kv.split("=",1)
        name=name.strip()
        value=value.strip()
        if name:
            out.append({"name":name,"value":value})
    return out

def _int_meta(*values)->int:
    for value in values:
        try:
            if value is None:
                continue
            return int(float(value))
        except (TypeError,ValueError):
            continue
    return 0

def _duration_meta(*values)->int:
    duration=_int_meta(*values)
    if duration>3600:
        return max(int(round(duration/1000)),0)
    return max(duration,0)

def _extract_debug_markers(html_text:str)->dict:
    text=html_text or ""
    low=text.lower()
    return {
        "has_universal":"__UNIVERSAL_DATA_FOR_REHYDRATION__" in text,
        "has_sigi":"SIGI_STATE" in text,
        "has_next":"__NEXT_DATA__" in text,
        "has_item_module":"ItemModule" in text,
        "has_default_scope":"__DEFAULT_SCOPE__" in text,
        "has_video_path":"/video/" in text,
        "has_login":"login" in low,
        "has_verify":"verify" in low,
        "has_captcha":"captcha" in low,
        "has_robot":"robot" in low,
        "has_unusual":"unusual" in low,
        "has_modern_ssr":"__MODERN_SSR_DATA__" in text,
        "has_4d":"tiktok_4d_playback" in low,
    }

def _detect_weird_tiktok_page(html_text:str,final_url:str="")->str:
    text=html_text or ""
    low=text.lower()
    final=(final_url or "").lower()
    has_data="__UNIVERSAL_DATA_FOR_REHYDRATION__" in text or "SIGI_STATE" in text or "__NEXT_DATA__" in text
    if "/player/v1/" in final:
        return "player_v1_url"
    if "/login" in final:
        return "login_url"
    if not has_data and ("captcha" in low or "verify" in low or "robot" in low or "unusual" in low):
        return "captcha_or_verify"
    if not has_data and ("tiktok_4d_playback" in low or "__MODERN_SSR_DATA__" in text):
        try:
            m=MODERN_SSR_RE.search(text)
            if m:
                ssr=json.loads(m.group(1))
                if isinstance(ssr,dict) and not (ssr.get("data") or {}):
                    return "modern_ssr_empty"
        except Exception:
            return "modern_ssr_shell"
        return "modern_shell"
    if not has_data and "<title data-react-helmet=\"true\"></title>" in text:
        return "empty_shell"
    return ""

def _dump_script_tags(html_text:str)->str:
    scripts=re.findall(r"<script\b[^>]*>(.*?)</script>",html_text or "",re.S|re.I)
    chunks=[]
    for i,s in enumerate(scripts[:80],1):
        s=(s or "").strip()
        if s:
            chunks.append(f"===== SCRIPT {i} =====\n{s[:6000]}\n")
    return "\n\n".join(chunks)

async def _send_debug_file(bot,path:str,caption:str):
    try:
        chat_id=int(LOG_CHAT_ID)
    except Exception:
        _ttdbg("invalid LOG_CHAT_ID | value=%r",LOG_CHAT_ID)
        return
    if not path or not os.path.exists(path):
        return
    try:
        with open(path,"rb") as f:
            await bot.send_document(chat_id=chat_id,document=f,caption=_truncate_text(caption,1024),disable_notification=True)
        _ttdbg("debug file sent | chat_id=%s path=%s",chat_id,path)
    except Exception as e:
        _ttdbg("failed sending debug file | chat_id=%s path=%s err=%r",chat_id,path,e)

async def _dump_tiktok_debug(bot,label:str,request_url:str,final_url:str,status:int,headers:dict,html_text:str,extra:dict|None=None):
    if not DEBUG_TIKTOK_DUMP:
        return
    markers=_extract_debug_markers(html_text)
    meta={
        "label":label,
        "request_url":request_url,
        "final_url":final_url,
        "status":status,
        "headers":dict(headers or {}),
        "markers":markers,
        "extra":extra or {},
        "body_preview":(html_text or "")[:5000],
    }
    meta_path=_write_debug_file(f"tiktok_{label}_meta",json.dumps(meta,ensure_ascii=False,indent=2),"json")
    html_path=_write_debug_file(f"tiktok_{label}_body",html_text or "","html")
    scripts_path=_write_debug_file(f"tiktok_{label}_scripts",_dump_script_tags(html_text or "") or "no script tags","txt")
    await _send_debug_file(bot,meta_path,f"[TTDBG] {label} meta")
    await _send_debug_file(bot,html_path,f"[TTDBG] {label} html")
    await _send_debug_file(bot,scripts_path,f"[TTDBG] {label} scripts")
    _ttdbg("dump saved | label=%s status=%s final=%s markers=%s",label,status,final_url,markers)

def is_tiktok(url:str)->bool:
    return any(x in (url or "") for x in ("tiktok.com","vt.tiktok.com","vm.tiktok.com"))

def _is_short_tiktok_url(url:str)->bool:
    return bool(SHORT_TIKTOK_RE.search(url or ""))

def _format_size(num_bytes:int)->str:
    if num_bytes<=0:
        return "0 B"
    value=float(num_bytes)
    for unit in ("B","KB","MB","GB","TB"):
        if value<1024 or unit=="TB":
            return f"{int(value)} {unit}" if unit=="B" else f"{value:.1f} {unit}"
        value/=1024
    return f"{value:.1f} TB"

def _format_speed(bytes_per_sec:float)->str:
    if bytes_per_sec<=0:
        return "0 B/s"
    value=float(bytes_per_sec)
    for unit in ("B/s","KB/s","MB/s","GB/s"):
        if value<1024 or unit=="GB/s":
            return f"{int(value)} {unit}" if unit=="B/s" else f"{value:.1f} {unit}"
        value/=1024
    return f"{value:.1f} GB/s"

def _format_eta(seconds:float)->str:
    if seconds<=0:
        return "0s"
    seconds=int(seconds)
    h,m,s=seconds//3600,(seconds%3600)//60,seconds%60
    if h>0:
        return f"{h}h {m}m {s}s"
    if m>0:
        return f"{m}m {s}s"
    return f"{s}s"

async def _safe_edit_progress(bot,chat_id,status_msg_id,title:str,downloaded:int,total:int=0,speed_bps:float=0.0,eta_seconds:float|None=None):
    if not TIKTOK_PROGRESS:
        return
    if not status_msg_id:
        return    
    lines=[f"<b>{html.escape(title)}</b>",""]
    if total>0:
        pct=min(downloaded*100/total,100.0)
        lines.append(f"<code>{html.escape(progress_bar(pct))}</code>")
        lines.append(f"<code>{html.escape(_format_size(downloaded))}/{html.escape(_format_size(total))}</code>")
    else:
        lines.append(f"<code>{html.escape(_format_size(downloaded))} downloaded</code>")
    if speed_bps>0:
        lines.append(f"<code>Speed: {html.escape(_format_speed(speed_bps))}</code>")
    if eta_seconds is not None and eta_seconds>=0 and total>0 and speed_bps>0:
        lines.append(f"<code>ETA: {html.escape(_format_eta(eta_seconds))}</code>")
    try:
        await bot.edit_message_text(chat_id=chat_id,message_id=status_msg_id,text="\n".join(lines),parse_mode="HTML")
    except RetryAfter as e:
        wait=max(int(getattr(e,"retry_after",1)),1)
        log.warning("TikTok progress RetryAfter | chat_id=%s wait=%s",chat_id,wait)
        await asyncio.sleep(wait+1)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            log.debug("TikTok progress edit failed | chat_id=%s message_id=%s err=%r",chat_id,status_msg_id,e)

async def _safe_edit_status(bot,chat_id,status_msg_id,text:str,min_interval:float=1.2):
    cache=getattr(bot,"_status_edit_cache",{})
    key=(chat_id,status_msg_id)
    now=time.monotonic()
    prev=cache.get(key) or {}
    if prev.get("text")==text:
        return
    if now-prev.get("ts",0)<min_interval:
        return
    for _ in range(2):
        try:
            await bot.edit_message_text(chat_id=chat_id,message_id=status_msg_id,text=text,parse_mode="HTML",disable_web_page_preview=True)
            cache[key]={"text":text,"ts":time.monotonic()}
            setattr(bot,"_status_edit_cache",cache)
            return
        except RetryAfter as e:
            wait_time=max(int(getattr(e,"retry_after",1)),1)
            await asyncio.sleep(wait_time)
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return
            log.warning("Failed to edit status | chat_id=%s message_id=%s err=%s",chat_id,status_msg_id,e)
            return

async def _probe_total_bytes(session,url:str,headers:dict|None=None)->int:
    try:
        async with session.head(url,headers=headers,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
            total=int(resp.headers.get("Content-Length",0) or 0)
            if total>0:
                return total
    except Exception as e:
        log.debug("TikTok HEAD size probe failed | err=%r",e)
    try:
        h=dict(headers or {})
        h["Range"]="bytes=0-0"
        async with session.get(url,headers=h,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
            content_range=resp.headers.get("Content-Range","")
            m=re.search(r"/(\d+)$",content_range)
            if m:
                return int(m.group(1))
            if resp.headers.get("Content-Length"):
                return int(resp.headers.get("Content-Length",0) or 0)
    except Exception as e:
        log.debug("TikTok Range size probe failed | err=%r",e)
    return 0

async def aria2c_download(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    aria2=shutil.which("aria2c")
    if not aria2:
        raise RuntimeError("aria2c not found in PATH")
    total=await _probe_total_bytes(session,media_url,headers=headers) if TIKTOK_PROGRESS else 0
    out_dir=os.path.dirname(out_path) or "."
    out_name=os.path.basename(out_path)
    
    cmd=[
        aria2,"--dir",out_dir,"--out",out_name,"--file-allocation=none","--allow-overwrite=true",
        "--auto-file-renaming=false","--continue=true",
        "--summary-interval=0","--download-result=hide","--console-log-level=warn",
    ]
    for k,v in (headers or {}).items():
        if v:
            cmd.extend(["--header",f"{k}: {v}"])
    cmd.append(media_url)
    proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
    if not TIKTOK_PROGRESS:
        try:
            _,stderr=await asyncio.wait_for(proc.communicate(),timeout=ARIA2C_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_process(proc,"aria2c")
            raise RuntimeError(f"aria2c timeout after {ARIA2C_TIMEOUT}s")
        if proc.returncode!=0:
            err=stderr.decode(errors="ignore").strip() if stderr else ""
            raise RuntimeError(err or f"aria2c exited with code {proc.returncode}")
        return
    started=time.monotonic()
    last_edit,last_sample_size,last_sample_ts=-10.0,0,time.time()
    while proc.returncode is None:
        if time.monotonic()-started>ARIA2C_TIMEOUT:
            await _kill_process(proc,"aria2c")
            raise RuntimeError(f"aria2c timeout after {ARIA2C_TIMEOUT}s")
        await asyncio.sleep(0.7)
        if not os.path.exists(out_path):
            continue
        try:
            downloaded=os.path.getsize(out_path)
        except Exception as e:
            log.debug("TikTok aria2c size read failed | file=%s err=%r",out_path,e)
            continue
        if downloaded<=0:
            continue
        now=time.time()
        elapsed=max(now-last_sample_ts,0.001)
        speed_bps=max(downloaded-last_sample_size,0)/elapsed
        eta_seconds=((total-downloaded)/speed_bps) if total>0 and speed_bps>0 and downloaded<=total else None
        if now-last_edit<TIKTOK_PROGRESS_INTERVAL and last_edit>=0:
            continue
        await _safe_edit_progress(bot,chat_id,status_msg_id,title_text,downloaded,total,speed_bps,eta_seconds)
        last_edit,last_sample_size,last_sample_ts=now,downloaded,now
    _,stderr=await proc.communicate()
    if proc.returncode!=0:
        err=stderr.decode(errors="ignore").strip() if stderr else ""
        raise RuntimeError(err or f"aria2c exited with code {proc.returncode}")


async def aiohttp_download(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    async with session.get(media_url,headers=headers,timeout=aiohttp.ClientTimeout(total=AIOHTTP_DOWNLOAD_TIMEOUT),allow_redirects=True) as r:
        if r.status>=400:
            raise RuntimeError(f"Download failed: HTTP {r.status}")
        total=int(r.headers.get("Content-Length",0) or 0)
        downloaded=0
        last_edit,last_sample_size,last_sample_ts=-10.0,0,time.time()
        chunk_size=max(64*1024,int(TIKTOK_AIOHTTP_CHUNK_SIZE or 256*1024))
        async with aiofiles.open(out_path,"wb") as f:
            async for chunk in r.content.iter_chunked(chunk_size):
                if not chunk:
                    continue
                await f.write(chunk)
                downloaded+=len(chunk)
                if not TIKTOK_PROGRESS:
                    continue
                now=time.time()
                elapsed=max(now-last_sample_ts,0.001)
                speed_bps=max(downloaded-last_sample_size,0)/elapsed
                eta_seconds=((total-downloaded)/speed_bps) if total>0 and speed_bps>0 and downloaded<=total else None
                if now-last_edit<TIKTOK_PROGRESS_INTERVAL and last_edit>=0:
                    continue
                await _safe_edit_progress(bot,chat_id,status_msg_id,title_text,downloaded,total,speed_bps,eta_seconds)
                last_edit,last_sample_size,last_sample_ts=now,downloaded,now

async def _download_with_aria2_first(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    aria2_path=shutil.which("aria2c")
    if aria2_path:
        try:
            log.info("TikTok download engine | engine=aria2c path=%s progress=%s",aria2_path,TIKTOK_PROGRESS)
            await aria2c_download(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
            return
        except Exception as e:
            log.warning("TikTok aria2c failed, fallback aiohttp | err=%r",e)
            _safe_remove_file(out_path,"aria2c partial")
    else:
        log.warning("TikTok aria2c not found in PATH, using aiohttp")
    log.info("TikTok download engine | engine=aiohttp progress=%s",TIKTOK_PROGRESS)
    await aiohttp_download(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)

async def _download_with_aiohttp_first(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    try:
        log.info("TikTok download engine | engine=aiohttp progress=%s chunk=%s",TIKTOK_PROGRESS,TIKTOK_AIOHTTP_CHUNK_SIZE)
        await aiohttp_download(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
        return
    except Exception as e:
        log.warning("TikTok aiohttp failed, fallback aria2c | err=%r",e)
        _safe_remove_file(out_path,"aiohttp partial")
    aria2_path=shutil.which("aria2c")
    if not aria2_path:
        raise RuntimeError("aiohttp failed and aria2c not found")
    log.info("TikTok download engine | engine=aria2c path=%s progress=%s",aria2_path,TIKTOK_PROGRESS)
    await aria2c_download(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)

async def _download_with_best_engine(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    if TIKTOK_DOWNLOAD_ENGINE in ("aria2","aria2-first","aria2c","aria2c-first"):
        return await _download_with_aria2_first(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
    return await _download_with_aiohttp_first(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)

def _extract_aweme_id(url:str)->str:
    m=re.search(r"/(?:video|photo|player/v1)/(\d+)",url or "",flags=re.I)
    return (m.group(1) if m else "").strip()

async def _resolve_tiktok_url(url:str)->tuple[str,str]:
    session=await get_http_session()
    headers=_build_tiktok_headers("https://www.tiktok.com/")
    async with session.get(url,headers=headers,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
        final_url=str(resp.url)
        resp_cookie=_cookie_header([{"name":c.key,"value":c.value} for c in resp.cookies.values()])
        merged_cookie=_merge_cookie_headers(headers.get("Cookie",""),resp_cookie)
        _ttdbg("resolve | input=%s status=%s final=%s cookie=%s",url,resp.status,final_url,bool(merged_cookie))
        return final_url,merged_cookie

def _json_walk(obj,key:str):
    if isinstance(obj,dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found=_json_walk(v,key)
            if found is not None:
                return found
    elif isinstance(obj,list):
        for item in obj:
            found=_json_walk(item,key)
            if found is not None:
                return found
    return None

def _pick_first_url(value)->str:
    if isinstance(value,str) and value.strip():
        return value.strip()
    if isinstance(value,list):
        for x in value:
            if isinstance(x,str) and x.strip():
                return x.strip()
    return ""

def _collect_url_list(value)->list[str]:
    out=[]
    if isinstance(value,str) and value.strip():
        out.append(value.strip())
    elif isinstance(value,list):
        for x in value:
            if isinstance(x,str) and x.strip():
                out.append(x.strip())
    return out

def _add_unique_urls(dst:list[str],value):
    for u in _collect_url_list(value):
        if u and u not in dst:
            dst.append(u)

def _extract_music_urls(item:dict)->list[str]:
    music=item.get("music") or item.get("musicInfo") or {}
    urls=[]
    if isinstance(music,dict):
        _add_unique_urls(urls,music.get("playUrl"))
        _add_unique_urls(urls,music.get("play_url"))
        _add_unique_urls(urls,music.get("playAddr"))
        _add_unique_urls(urls,music.get("playAddrUrl"))
        play_addr=music.get("playAddr") or music.get("play_addr") or {}
        if isinstance(play_addr,dict):
            _add_unique_urls(urls,play_addr.get("urlList") or play_addr.get("UrlList"))
            _add_unique_urls(urls,play_addr.get("url") or play_addr.get("Uri"))
    return urls
    
def _parse_direct_media(item:dict)->dict:
    desc=str(item.get("desc") or item.get("description") or "").strip()
    title=desc or "TikTok Video"
    image_post=item.get("imagePost") or item.get("image_post") or {}
    if isinstance(image_post,dict) and isinstance(image_post.get("images"),list) and image_post.get("images"):
        images=[]
        for img in image_post.get("images") or []:
            image_url=_pick_first_url(
                (((img or {}).get("imageURL") or {}).get("urlList"))
                or (((img or {}).get("displayImage") or {}).get("urlList"))
                or (((img or {}).get("ownerWatermarkImage") or {}).get("urlList"))
            )
            if image_url:
                images.append(image_url)
        if images:
            music_urls=_extract_music_urls(item)
            return {
                "kind":"album",
                "title":title,
                "desc":desc,
                "images":images,
                "music_url":music_urls[0] if music_urls else "",
                "music_urls":music_urls,
            }
    video=item.get("video") or {}
    duration=_duration_meta(video.get("duration"),video.get("Duration"))
    width=_int_meta(video.get("width"),video.get("Width"))
    height=_int_meta(video.get("height"),video.get("Height"))
    bitrate_info=video.get("bitrateInfo") if isinstance(video,dict) else []
    video_urls=[]
    candidates=[video.get("playAddr"),video.get("playAddrStruct"),video.get("downloadAddr"),video.get("downloadAddrStruct")]
    if isinstance(bitrate_info,list):
        for br in bitrate_info:
            if isinstance(br,dict):
                width=width or _int_meta(br.get("Width"),br.get("width"))
                height=height or _int_meta(br.get("Height"),br.get("height"))
                candidates.append(br.get("PlayAddr"))
                candidates.append(br.get("playAddr"))
    for candidate in candidates:
        if isinstance(candidate,dict):
            width=width or _int_meta(candidate.get("Width"),candidate.get("width"))
            height=height or _int_meta(candidate.get("Height"),candidate.get("height"))
            _add_unique_urls(video_urls,candidate.get("urlList") or candidate.get("UrlList"))
            _add_unique_urls(video_urls,candidate.get("url") or candidate.get("Uri"))
        elif isinstance(candidate,str):
            _add_unique_urls(video_urls,candidate)
    if video_urls:
        return {
            "kind":"video",
            "title":title,
            "desc":desc,
            "video_url":video_urls[0],
            "video_urls":video_urls,
            "duration":duration,
            "width":width,
            "height":height,
        }
    raise RuntimeError("TikTok direct media URL not found")

def _parse_universal_data(html_text:str)->dict:
    m=UNIVERSAL_RE.search(html_text or "")
    if not m:
        raise RuntimeError("TikTok universal data not found")
    try:
        data=json.loads(m.group(1))
    except Exception as e:
        raise RuntimeError(f"Failed to parse TikTok universal data: {e}") from e
    default_scope=data.get("__DEFAULT_SCOPE__")
    if not isinstance(default_scope,dict):
        raise RuntimeError("TikTok default scope not found")
    item_struct=default_scope.get("itemStruct")
    if not isinstance(item_struct,dict):
        item_module=default_scope.get("webapp.video-detail")
        if isinstance(item_module,dict):
            item_info=item_module.get("itemInfo") or {}
            item_struct=item_info.get("itemStruct") if isinstance(item_info,dict) else None
    if not isinstance(item_struct,dict):
        item_struct=_json_walk(default_scope,"itemStruct")
    if not isinstance(item_struct,dict):
        raise RuntimeError("TikTok itemStruct not found")
    return item_struct

def _parse_sigi_state(html_text:str)->dict:
    m=SIGI_RE.search(html_text or "")
    if not m:
        raise RuntimeError("TikTok SIGI_STATE not found")
    try:
        data=json.loads(m.group(1))
    except Exception as e:
        raise RuntimeError(f"Failed to parse TikTok SIGI_STATE: {e}") from e
    item_module=data.get("ItemModule")
    if isinstance(item_module,dict) and item_module:
        first=next(iter(item_module.values()),None)
        if isinstance(first,dict):
            return first
    detail=data.get("VideoPage") or data.get("ItemPage") or {}
    item_struct=detail.get("itemInfo",{}).get("itemStruct") if isinstance(detail,dict) else None
    if isinstance(item_struct,dict):
        return item_struct
    item_struct=_json_walk(data,"itemStruct")
    if isinstance(item_struct,dict):
        return item_struct
    raise RuntimeError("TikTok itemStruct not found in SIGI_STATE")

def _parse_next_data(html_text:str)->dict:
    m=NEXT_RE.search(html_text or "")
    if not m:
        raise RuntimeError("TikTok __NEXT_DATA__ not found")
    try:
        data=json.loads(m.group(1))
    except Exception as e:
        raise RuntimeError(f"Failed to parse TikTok __NEXT_DATA__: {e}") from e
    item_struct=_json_walk(data,"itemStruct")
    if isinstance(item_struct,dict):
        return item_struct
    raise RuntimeError("TikTok itemStruct not found in __NEXT_DATA__")

def _extract_item_struct(html_text:str,final_url:str="")->dict:
    errors=[]
    for parser in (_parse_universal_data,_parse_sigi_state,_parse_next_data):
        try:
            item=parser(html_text)
            if isinstance(item,dict) and item:
                _ttdbg("parser success | parser=%s",parser.__name__)
                return item
        except Exception as e:
            errors.append(f"{parser.__name__}: {e}")
    weird=_detect_weird_tiktok_page(html_text,final_url)
    if weird:
        raise RuntimeError(f"TikTok weird page detected: {weird} | {' ; '.join(errors)}")
    raise RuntimeError(" ; ".join(errors) if errors else "TikTok itemStruct not found")

def _scrapling_text(page)->str:
    for attr in ("html_content","text","html","content","body"):
        try:
            val=getattr(page,attr,None)
            if callable(val):
                val=val()
            if isinstance(val,bytes):
                return val.decode("utf-8",errors="ignore")
            if isinstance(val,str) and val.strip():
                return val
        except Exception as e:
            log.debug("Scrapling text attr failed | attr=%s err=%r",attr,e)
    return str(page or "")

def _scrapling_cookie_header(page)->str:
    try:
        cookies=getattr(page,"cookies",None)
        if callable(cookies):
            cookies=cookies()
        if isinstance(cookies,dict):
            return "; ".join(f"{k}={v}" for k,v in cookies.items() if k)
        if isinstance(cookies,list):
            parts=[]
            for c in cookies:
                if not isinstance(c,dict):
                    continue
                name=str(c.get("name") or "").strip()
                value=str(c.get("value") or "").strip()
                if name:
                    parts.append(f"{name}={value}")
            return "; ".join(parts)
    except Exception as e:
        log.debug("Scrapling cookie extraction failed | err=%r",e)
    return ""

async def _fetch_html_with_scrapling(target:str,cookie_header:str="")->tuple[str,str,int,str,dict]:
    if not USE_SCRAPLING:
        raise RuntimeError("Scrapling disabled")
    try:
        from scrapling.fetchers import AsyncFetcher
    except Exception as e:
        raise RuntimeError(f"Scrapling unavailable: {e}") from e
    headers={"Accept":WEB_HEADERS["Accept"],"Accept-Language":WEB_HEADERS["Accept-Language"],"Referer":"https://www.tiktok.com/"}
    if cookie_header:
        headers["Cookie"]=cookie_header
    kwargs={"headers":headers,"stealthy_headers":True,"impersonate":"chrome","timeout":20}
    try:
        page=await AsyncFetcher.get(target,**kwargs)
    except TypeError:
        kwargs.pop("impersonate",None)
        page=await AsyncFetcher.get(target,**kwargs)
    html_text=_scrapling_text(page)
    final_url=str(getattr(page,"url",target) or target)
    status=int(getattr(page,"status",getattr(page,"status_code",0)) or 0)
    resp_cookie=_scrapling_cookie_header(page)
    headers_dump=dict(getattr(page,"headers",{}) or {})
    _ttdbg("scrapling fetch | target=%s status=%s final=%s len=%s cookie=%s",target,status,final_url,len(html_text),bool(resp_cookie))
    return html_text,final_url,status,resp_cookie,headers_dump

async def _fetch_tiktok_govd_fast(url:str)->dict:
    aweme_id=_extract_aweme_id(url)
    resolved=url
    resolved_cookie=""
    if not aweme_id or _is_short_tiktok_url(url):
        resolved,resolved_cookie=await _resolve_tiktok_url(url)
        aweme_id=_extract_aweme_id(resolved)
    if not aweme_id:
        raise RuntimeError("TikTok aweme id not found")
    if "/player/v1/" in (resolved or "").lower():
        raise RuntimeError(f"TikTok weird page detected: player_v1_url | resolved={resolved}")
    session=await get_http_session()
    target=f"https://www.tiktok.com/@_/video/{aweme_id}"
    headers=_build_tiktok_headers("https://www.tiktok.com/",resolved_cookie)
    
    async with session.get(target,headers=headers,timeout=aiohttp.ClientTimeout(total=12),allow_redirects=True) as resp:
        final_url=str(resp.url)
        status=resp.status
        html_text=await resp.text()
        resp_cookie=_cookie_header([{"name":c.key,"value":c.value} for c in resp.cookies.values()])
        merged_cookie=_merge_cookie_headers(headers.get("Cookie",""),resp_cookie)

    if "Please wait..." in html_text and 'id="cs"' in html_text and 'id="wci"' in html_text:
        log.info("TikTok challenge detected, solving PoW...")
        chal_cookie = await asyncio.to_thread(_solve_tt_challenge, html_text)
        if chal_cookie:
            headers["Cookie"] = _merge_cookie_headers(merged_cookie, chal_cookie)
            async with session.get(target,headers=headers,timeout=aiohttp.ClientTimeout(total=12),allow_redirects=True) as resp2:
                final_url=str(resp2.url)
                status=resp2.status
                html_text=await resp2.text()
                resp_cookie2=_cookie_header([{"name":c.key,"value":c.value} for c in resp2.cookies.values()])
                merged_cookie=_merge_cookie_headers(headers.get("Cookie",""),resp_cookie2)

    _ttdbg("fast fetch | target=%s status=%s final=%s len=%s cookie=%s",target,status,final_url,len(html_text),bool(merged_cookie))
    
    item_struct=_extract_item_struct(html_text,final_url)
    media=_parse_direct_media(item_struct)
    media["cookies"]=_cookies_from_header(merged_cookie)
    media["resolved_url"]=resolved
    media["aweme_id"]=aweme_id
    media["target_url"]=target
    media["final_url"]=final_url
    media["source"]="fast"
    return media

async def _fetch_tiktok_direct(url:str,bot=None)->dict:
    resolved,resolved_cookie=await _resolve_tiktok_url(url)
    aweme_id=_extract_aweme_id(resolved)
    if not aweme_id:
        raise RuntimeError("TikTok aweme id not found")
    if "/player/v1/" in (resolved or "").lower():
        raise RuntimeError(f"TikTok weird page detected: player_v1_url | resolved={resolved}")
    session=await get_http_session()
    target=f"https://www.tiktok.com/@_/video/{aweme_id}"
    headers=_build_tiktok_headers("https://www.tiktok.com/",resolved_cookie)
    last_aio_err=None
    final_url=target
    status=0
    html_text=""
    merged_cookie=resolved_cookie
    headers_dump={}
    
    for attempt in range(5):
        try:
            async with session.get(target,headers=headers,timeout=aiohttp.ClientTimeout(total=25),allow_redirects=True) as resp:
                final_url=str(resp.url)
                status=resp.status
                html_text=await resp.text()
                resp_cookie=_cookie_header([{"name":c.key,"value":c.value} for c in resp.cookies.values()])
                merged_cookie=_merge_cookie_headers(headers.get("Cookie",""),resp_cookie)
                headers_dump=dict(resp.headers)
            
            if "Please wait..." in html_text and 'id="cs"' in html_text and 'id="wci"' in html_text:
                log.info("TikTok challenge detected in full-scraping, solving PoW...")
                chal_cookie = await asyncio.to_thread(_solve_tt_challenge, html_text)
                if chal_cookie:
                    headers["Cookie"] = _merge_cookie_headers(merged_cookie, chal_cookie)
                    async with session.get(target,headers=headers,timeout=aiohttp.ClientTimeout(total=25),allow_redirects=True) as resp2:
                        final_url=str(resp2.url)
                        status=resp2.status
                        html_text=await resp2.text()
                        resp_cookie2=_cookie_header([{"name":c.key,"value":c.value} for c in resp2.cookies.values()])
                        merged_cookie=_merge_cookie_headers(headers.get("Cookie",""),resp_cookie2)
                        headers_dump=dict(resp2.headers)

            _ttdbg("aiohttp fetch | attempt=%s target=%s status=%s final=%s len=%s cookie=%s resolved=%s",attempt+1,target,status,final_url,len(html_text),bool(merged_cookie),resolved)
            item_struct=_extract_item_struct(html_text,final_url)
            break
        except Exception as e:
            last_aio_err=e
            _ttdbg("aiohttp parse/fetch failed | attempt=%s target=%s err=%r",attempt+1,target,e)
            if attempt<4:
                await asyncio.sleep(0.35*(attempt+1))
                continue
            _ttdbg("aiohttp failed after retries, try scrapling once | target=%s err=%r",target,e)
            try:
                scrap_html,scrap_final,scrap_status,scrap_cookie,scrap_headers=await _fetch_html_with_scrapling(target,merged_cookie)
                scrap_merged_cookie=_merge_cookie_headers(merged_cookie,scrap_cookie)
                item_struct=_extract_item_struct(scrap_html,scrap_final)
                html_text=scrap_html
                final_url=scrap_final
                status=scrap_status
                merged_cookie=scrap_merged_cookie
                headers_dump=scrap_headers
                break
            except Exception as scrap_err:
                if bot:
                    await _dump_tiktok_debug(bot,"scrape_failed",target,final_url,status,headers_dump,html_text,{
                        "input_url":url,
                        "resolved":resolved,
                        "canonical_target":target,
                        "aweme_id":aweme_id,
                        "aiohttp_error":str(last_aio_err),
                        "scrapling_error":str(scrap_err),
                        "has_cookie":bool(merged_cookie),
                    })
                raise RuntimeError(f"TikTok scraping failed: aiohttp={last_aio_err} ; scrapling={scrap_err}") from scrap_err
                
    media=_parse_direct_media(item_struct)
    media["cookies"]=_cookies_from_header(merged_cookie)
    media["resolved_url"]=resolved
    media["aweme_id"]=aweme_id
    media["target_url"]=target
    media["final_url"]=final_url
    media["source"]="full-scraping"
    return media


async def _fetch_tiktok_metadata(url:str,bot=None,chat_id=None,status_msg_id=None,metadata_ready:bool=False)->dict:
    fast_err=None
    if USE_GOVD_FAST:
        try:
            started=time.monotonic()
            media=await _fetch_tiktok_govd_fast(url)
            log.info("TikTok metadata success | source=fast url=%s kind=%s elapsed=%.2fs target=%s",url,media.get("kind"),time.monotonic()-started,media.get("target_url"))
            return media
        except Exception as e:
            fast_err=e
            log.warning("TikTok fast metadata failed, fallback full scraper | url=%s err=%r",url,e)
    if not metadata_ready and bot is not None and chat_id is not None and status_msg_id is not None:
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Scraping TikTok metadata...</b>")
    started=time.monotonic()
    media=await _fetch_tiktok_direct(url,bot=bot)
    log.info("TikTok metadata success | source=full-scraping url=%s kind=%s elapsed=%.2fs target=%s fast_err=%r",url,media.get("kind"),time.monotonic()-started,media.get("target_url"),fast_err)
    return media

async def _download_direct_video(media:dict,bot,chat_id,status_msg_id)->dict:
    session=await get_http_session()
    title=(media.get("title") or "TikTok Video").strip()
    cookie_header=_cookie_header(media.get("cookies"))
    video_urls=media.get("video_urls") or []
    if media.get("video_url") and media.get("video_url") not in video_urls:
        video_urls.insert(0,media.get("video_url"))
    if not video_urls:
        raise RuntimeError("TikTok direct video URLs empty")
    base_headers={
        "User-Agent":USER_AGENT,
        "Referer":media.get("final_url") or media.get("resolved_url") or "https://www.tiktok.com/",
        "Origin":"https://www.tiktok.com",
        "Accept":"video/webm,video/mp4,video/*,*/*;q=0.8",
        "Accept-Language":"en-US,en;q=0.9",
        "Connection":"keep-alive",
    }
    if cookie_header:
        base_headers["Cookie"]=cookie_header
    last_err=None
    for idx,video_url in enumerate(video_urls,start=1):
        out_path=f"{TMP_DIR}/{uuid.uuid4().hex}_{sanitize_filename(title)}.mp4"
        try:
            _ttdbg("direct video download try | index=%s total=%s url=%s",idx,len(video_urls),video_url[:180])
            await _download_with_best_engine(session,video_url,out_path,bot,chat_id,status_msg_id,"Downloading TikTok video...",headers=base_headers)
            if await asyncio.to_thread(is_invalid_video,out_path):
                _safe_remove_file(out_path,"invalid TikTok video")
                raise RuntimeError("Invalid video file from TikTok scraping")
            log.info("TikTok direct scraping success | type=video source=%s file=%s url_index=%s",media.get("source"),out_path,idx)
            return {
                "path":out_path,
                "title":title,
                "desc":media.get("desc") or "",
                "source":media.get("source") or "scraping",
                "kind":"video",
                "duration":media.get("duration") or 0,
                "width":media.get("width") or 0,
                "height":media.get("height") or 0,
            }
        except Exception as e:
            last_err=e
            log.warning("TikTok direct URL failed | index=%s total=%s err=%r",idx,len(video_urls),e)
            _safe_remove_file(out_path,"failed direct video")
            continue
    raise RuntimeError(f"All TikTok direct video URLs failed: {last_err}")

async def _download_album_images(session,image_urls:list[str],title:str,bot,chat_id,status_msg_id,headers:dict|None=None)->list[dict]:
    if not image_urls:
        return []
    total=len(image_urls)
    sem=asyncio.Semaphore(8)
    results=[None]*total
    async def one(idx:int,image_url:str):
        async with sem:
            safe_title=sanitize_filename(title or "TikTok Slideshow")
            out_path=f"{TMP_DIR}/{uuid.uuid4().hex}_{safe_title}_{idx+1}.jpg"
            try:
                async with session.get(image_url,headers=headers,timeout=aiohttp.ClientTimeout(total=120),allow_redirects=True) as r:
                    if r.status>=400:
                        raise RuntimeError(f"Image HTTP {r.status}")
                    async with aiofiles.open(out_path,"wb") as f:
                        async for chunk in r.content.iter_chunked(max(64*1024,int(TIKTOK_ALBUM_CHUNK_SIZE or 256*1024))):
                            if chunk:
                                await f.write(chunk)
                results[idx]={"type":"photo","path":out_path}
                log.info("TikTok slideshow image saved | index=%s/%s file=%s",idx+1,total,out_path)
            except Exception as e:
                log.exception("Failed to download slideshow image | index=%s url=%s err=%r",idx,image_url,e)
                _safe_remove_file(out_path,"failed slideshow image")
                raise
    await asyncio.gather(*(one(i,url) for i,url in enumerate(image_urls)))
    return [x for x in results if x]

async def _download_direct_album(media:dict,bot,chat_id,status_msg_id)->dict:
    session=await get_http_session()
    title=(media.get("title") or "TikTok Slideshow").strip()
    image_urls=[u for u in (media.get("images") or []) if u]
    if not image_urls:
        raise RuntimeError("TikTok slideshow images not found")
    cookie_header=_cookie_header(media.get("cookies"))
    headers={"User-Agent":USER_AGENT,"Referer":"https://www.tiktok.com/"}
    if cookie_header:
        headers["Cookie"]=cookie_header
    items=await _download_album_images(session,image_urls,title,bot,chat_id,status_msg_id,headers=headers)
    if not items:
        raise RuntimeError("TikTok slideshow download failed")
    log.info("TikTok direct scraping success | type=album source=%s items=%s",media.get("source"),len(items))
    return {"items":items,"title":title,"desc":media.get("desc") or "","source":media.get("source") or "scraping","kind":"album"}

async def _download_slideshow_audio(media:dict,bot,chat_id,status_msg_id)->dict:
    session=await get_http_session()
    title=(media.get("title") or "TikTok Slideshow Audio").strip()
    urls=[u for u in (media.get("music_urls") or []) if u]
    if media.get("music_url") and media.get("music_url") not in urls:
        urls.insert(0,media.get("music_url"))
    if not urls:
        raise RuntimeError("TikTok slideshow audio not found")
    cookie_header=_cookie_header(media.get("cookies"))
    headers={
        "User-Agent":USER_AGENT,
        "Referer":media.get("final_url") or media.get("resolved_url") or "https://www.tiktok.com/",
        "Accept":"audio/*,*/*;q=0.8",
        "Accept-Language":"en-US,en;q=0.9",
    }
    if cookie_header:
        headers["Cookie"]=cookie_header
    last_err=None
    for idx,url in enumerate(urls,start=1):
        out_path=f"{TMP_DIR}/{uuid.uuid4().hex}_{sanitize_filename(title)}.m4a"
        try:
            async with session.get(url,headers=headers,timeout=aiohttp.ClientTimeout(total=120),allow_redirects=True) as r:
                if r.status>=400:
                    raise RuntimeError(f"Audio HTTP {r.status}")
                async with aiofiles.open(out_path,"wb") as f:
                    async for chunk in r.content.iter_chunked(max(64*1024,int(TIKTOK_AIOHTTP_CHUNK_SIZE or 256*1024))):
                        if chunk:
                            await f.write(chunk)
            if not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
                raise RuntimeError("Downloaded audio is empty")
            log.info("TikTok slideshow audio saved | source=%s index=%s file=%s",media.get("source"),idx,out_path)
            return {
                "path":out_path,
                "title":title,
                "desc":media.get("desc") or "",
                "source":media.get("source") or "scraping",
                "kind":"audio",
            }
        except Exception as e:
            last_err=e
            log.warning("TikTok slideshow audio URL failed | index=%s total=%s err=%r",idx,len(urls),e)
            _safe_remove_file(out_path,"failed slideshow audio")
    raise RuntimeError(f"All TikTok slideshow audio URLs failed: {last_err}")

def _ffprobe_duration(path:str)->float:
    try:
        result=subprocess.run(
            ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        if result.returncode==0:
            return max(float((result.stdout or "0").strip() or 0),0.0)
    except Exception as e:
        log.warning("Failed to probe slideshow audio duration | file=%s err=%r",path,e)
    return 0.0

def _render_slideshow_video(image_paths:list[str], audio_path:str|None, out_path:str):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found")
    if not image_paths:
        raise RuntimeError("No slideshow images to render")

    audio_duration = _ffprobe_duration(audio_path) if audio_path else 0.0

    if len(image_paths) == 1:
        cmd = [
            "ffmpeg", "-y", 
            "-loop", "1", "-framerate", str(TIKTOK_SLIDESHOW_FPS), 
            "-i", image_paths[0]
        ]
        if audio_path:
            cmd.extend(["-i", audio_path])

        filter_str = (
            f"scale={TIKTOK_SLIDESHOW_WIDTH}:{TIKTOK_SLIDESHOW_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={TIKTOK_SLIDESHOW_WIDTH}:{TIKTOK_SLIDESHOW_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            f"format=yuv420p,setsar=1"
        )

        cmd.extend([
            "-vf", filter_str,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart"
        ])

        if audio_path:
            cmd.extend(["-c:a", "aac", "-b:a", "128k", "-shortest"])
        else:
            cmd.extend(["-t", "5"])

        cmd.append(out_path)

        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg single image render failed: {result.stderr}")
        return

    base_duration=max(float(TIKTOK_SLIDESHOW_IMAGE_DURATION),0.3)
    transition_name=(TIKTOK_SLIDESHOW_TRANSITION or "slideleft").strip()

    render_paths=list(image_paths)

    if TIKTOK_SLIDESHOW_LOOP_IMAGES and audio_duration>0:
        raw_transition=float(TIKTOK_SLIDESHOW_TRANSITION_DURATION)
        transition_probe=max(min(raw_transition,base_duration-0.1),0.1)
        step=max(base_duration-transition_probe,0.1)
        loop_count=max(len(image_paths),int(audio_duration/step)+2)
        render_paths=[image_paths[i % len(image_paths)] for i in range(loop_count)]
        per_image=base_duration
    elif TIKTOK_SLIDESHOW_SYNC_AUDIO and audio_duration>0 and len(image_paths)>0:
        per_image=max(audio_duration/len(image_paths),float(TIKTOK_SLIDESHOW_MIN_IMAGE_DURATION))
    else:
        per_image=base_duration

    transition=max(min(float(TIKTOK_SLIDESHOW_TRANSITION_DURATION),per_image-0.1),0.1)

    log.info(
        "TikTok slideshow timing | images=%s render_images=%s audio_duration=%.2fs per_image=%.2fs transition=%.2fs loop=%s sync_audio=%s",
        len(image_paths),len(render_paths),audio_duration,per_image,transition,TIKTOK_SLIDESHOW_LOOP_IMAGES,TIKTOK_SLIDESHOW_SYNC_AUDIO,
    )

    inputs=[]
    filters=[]
    video_labels=[]

    for i,path in enumerate(render_paths):
        inputs.extend(["-loop","1","-t",f"{per_image:.3f}","-i",path])
        filters.append(
            f"[{i}:v]"
            f"scale={TIKTOK_SLIDESHOW_WIDTH}:{TIKTOK_SLIDESHOW_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={TIKTOK_SLIDESHOW_WIDTH}:{TIKTOK_SLIDESHOW_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={TIKTOK_SLIDESHOW_FPS},format=yuv420p,setsar=1"
            f"[v{i}]"
        )
        video_labels.append(f"[v{i}]")

    audio_index=len(render_paths)
    if audio_path:
        inputs.extend(["-i",audio_path])

    if len(video_labels)==1:
        filters.append(f"{video_labels[0]}copy[vout]")
    else:
        prev=video_labels[0]
        for i in range(1,len(video_labels)):
            out="[vout]" if i==len(video_labels)-1 else f"[vx{i}]"
            offset=i*(per_image-transition)
            filters.append(
                f"{prev}{video_labels[i]}"
                f"xfade=transition={transition_name}:duration={transition:.3f}:offset={offset:.3f}"
                f"{out}"
            )
            prev=out

    cmd=[
        "ffmpeg","-y",
        *inputs,
        "-filter_complex",";".join(filters),
        "-map","[vout]",
    ]

    if audio_path:
        cmd.extend(["-map",f"{audio_index}:a:0"])

    cmd.extend([
        "-c:v","libx264",
        "-preset","veryfast",
        "-crf","23",
        "-pix_fmt","yuv420p",
        "-movflags","+faststart",
    ])

    if audio_path:
        cmd.extend(["-c:a","aac","-b:a","128k","-shortest"])

    cmd.append(out_path)

    result=subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )

    if result.returncode!=0:
        raise RuntimeError((result.stderr or "ffmpeg slideshow render failed")[-1500:])

    if not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
        raise RuntimeError("Slideshow render output is empty")


async def _download_direct_slideshow_video(media:dict,bot,chat_id,status_msg_id)->dict:
    session=await get_http_session()
    title=(media.get("title") or "TikTok Slideshow").strip()
    image_urls=[u for u in (media.get("images") or []) if u]
    if not image_urls:
        raise RuntimeError("TikTok slideshow images not found")
    cookie_header=_cookie_header(media.get("cookies"))
    headers={"User-Agent":USER_AGENT,"Referer":media.get("final_url") or media.get("resolved_url") or "https://www.tiktok.com/"}
    if cookie_header:
        headers["Cookie"]=cookie_header
    await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Downloading TikTok slideshow images...</b>",min_interval=0)
    items=await _download_album_images(session,image_urls,title,bot,chat_id,status_msg_id,headers=headers)
    image_paths=[item.get("path") for item in items if item.get("path")]
    audio_meta=None
    audio_path=None
    out_path=f"{TMP_DIR}/{uuid.uuid4().hex}_{sanitize_filename(title)}_slideshow.mp4"
    try:
        if media.get("music_url") or media.get("music_urls"):
            await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Downloading TikTok slideshow audio...</b>",min_interval=0)
            audio_meta=await _download_slideshow_audio(media,bot,chat_id,status_msg_id)
            audio_path=audio_meta.get("path")
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Converting slideshow to MP4...</b>",min_interval=0)
        await asyncio.to_thread(_render_slideshow_video,image_paths,audio_path,out_path)
        duration=int(round(_ffprobe_duration(out_path)))
        log.info("TikTok slideshow rendered | source=%s images=%s audio=%s file=%s",media.get("source"),len(image_paths),bool(audio_path),out_path)
        return {
            "path":out_path,
            "title":title,
            "desc":media.get("desc") or "",
            "source":media.get("source") or "scraping",
            "kind":"video",
            "duration":duration,
            "width":TIKTOK_SLIDESHOW_WIDTH,
            "height":TIKTOK_SLIDESHOW_HEIGHT,
        }
    except Exception:
        _safe_remove_file(out_path,"failed slideshow video")
        raise
    finally:
        for path in image_paths:
            _safe_remove_file(path,"slideshow image")
        _safe_remove_file(audio_path,"slideshow audio")
        
async def _download_tiktok_media(media:dict, bot, chat_id, status_msg_id, fmt_key="mp4"):
    kind = media.get("kind")
    images = media.get("images") or []

    if fmt_key == "mp3":
        if kind == "album":
            return await _download_slideshow_audio(media, bot, chat_id, status_msg_id)
        if kind != "video":
            raise RuntimeError("TikTok media does not contain audio")
        return await _download_direct_video(media, bot, chat_id, status_msg_id)

    if kind == "video":
        return await _download_direct_video(media, bot, chat_id, status_msg_id)

    if kind == "album":
        if len(images) <= 1 and fmt_key in ("video", "mp4", "slideshow_video"):
            return await _download_direct_slideshow_video(media, bot, chat_id, status_msg_id)
  
        if fmt_key in ("video", "mp4"):
            return {"choice_required": "tiktok_slideshow", "media": media}

        if fmt_key == "slideshow_video":
            return await _download_direct_slideshow_video(media, bot, chat_id, status_msg_id)

        await _safe_edit_status(bot, chat_id, status_msg_id, "<b>Downloading TikTok slideshow...</b>", min_interval=0)
        return await _download_direct_album(media, bot, chat_id, status_msg_id)

    raise RuntimeError("Unsupported TikTok media type")

async def tiktok_scrape_download(url,bot,chat_id,status_msg_id,fmt_key="mp4",metadata_ready:bool=False):
    media=await _fetch_tiktok_metadata(url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,metadata_ready=metadata_ready)
    log.info("TikTok scraping metadata ready | url=%s kind=%s title=%r target=%s source=%s",url,media.get("kind"),media.get("title"),media.get("target_url"),media.get("source"))
    return await _download_tiktok_media(media,bot,chat_id,status_msg_id,fmt_key=fmt_key)

async def douyin_download(url,bot,chat_id,status_msg_id):
    result=await _tikwm_result(url=url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,fmt_key="mp4")
    if result.get("items"):
        raise RuntimeError("SLIDESHOW")
    return result

async def tiktok_download(url,bot,chat_id,status_msg_id,fmt_key="mp4",metadata_ready:bool=False):
    try:
        log.info("TikTok primary start | source=scraping url=%s fmt=%s metadata_ready=%s fast=%s engine=%s progress=%s",url,fmt_key,metadata_ready,USE_GOVD_FAST,TIKTOK_DOWNLOAD_ENGINE,TIKTOK_PROGRESS)
        result=await tiktok_scrape_download(url=url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,fmt_key=fmt_key,metadata_ready=metadata_ready)
        if isinstance(result,dict):
            if result.get("path"):
                log.info("TikTok primary success | source=%s file=%s",result.get("source"),result.get("path"))
            elif result.get("items"):
                log.info("TikTok primary success | source=%s items=%s",result.get("source"),len(result.get("items") or []))
        return result
    except Exception as e:
        log.warning("TikTok scraping failed, fallback to tikwm | url=%s fmt=%s err=%r",url,fmt_key,e)
        try:
            err_path=_write_debug_file("tiktok_scrape_exception",repr(e),"txt")
            await _send_debug_file(bot,err_path,f"[TTDBG] scrape exception | {url}")
        except Exception as dbg_err:
            log.debug("Failed to send TikTok scrape exception debug | err=%r",dbg_err)
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>TikTok scraping failed</b>\n\n<i>Fallback to tikwm...</i>")
        result=await _tikwm_result(url=url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,fmt_key=fmt_key)
        if isinstance(result,dict):
            if result.get("path"):
                log.info("TikTok fallback success | source=tikwm file=%s",result.get("path"))
            elif result.get("items"):
                log.info("TikTok fallback success | source=tikwm items=%s",len(result.get("items") or []))
        return result