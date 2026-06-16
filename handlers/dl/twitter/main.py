import os,re,time,uuid,json,shutil,asyncio,aiohttp,aiofiles,logging,html
from urllib.parse import urlparse,urlencode
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR
try:
    from handlers.dl.constants import BASE_DIR
except Exception:
    BASE_DIR=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from handlers.dl.utils import sanitize_filename,progress_bar
from handlers.dl.ytdlp import ytdlp_download

log=logging.getLogger(__name__)

COOKIES_PATH=os.path.abspath(os.path.join(BASE_DIR,"..","..","data","cookies.txt"))
DEBUG_TWITTER=True
_COOKIE_MAP_CACHE=None
_COOKIE_HEADER_CACHE=None
AUTH_TOKEN="AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
API_HOSTNAME="x.com"
API_BASE=f"https://{API_HOSTNAME}/i/api/graphql/"
API_ENDPOINT=API_BASE+"2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId"
STATUS_RE=re.compile(r"https?://(?:fx|vx|fixup)?(?:twitter|x)\.com/[^/]+/status/(\d+)",re.I)
SHORT_RE=re.compile(r"https?://t\.co/\w+",re.I)
RES_RE=re.compile(r"(\d+)x(\d+)")

def _dbg(msg:str,*args):
    if DEBUG_TWITTER: log.warning("XDBG | "+msg,*args)

def _clip(text:str,limit:int=300)->str:
    text=str(text or "").replace("\n","\\n").replace("\r","\\r")
    return text if len(text)<=limit else text[:limit]+"...<cut>"

def is_x_url(url:str)->bool:
    text=(url or "").strip()
    if not text: return False
    host=(urlparse(text).hostname or "").lower()
    return host in ("x.com","www.x.com","twitter.com","www.twitter.com","mobile.twitter.com","t.co","www.t.co")

async def _safe_edit_status(bot,chat_id,status_msg_id,text:str):
    if not status_msg_id:
        return
    try:
        await bot.edit_message_text(chat_id=chat_id,message_id=status_msg_id,text=text,parse_mode="HTML",disable_web_page_preview=True)
    except Exception:
        pass

X_COOKIE_DOMAINS = ("x.com", ".x.com", "twitter.com", ".twitter.com")
X_COOKIE_NAMES = {
    "auth_token",
    "ct0",
    "twid",
    "guest_id",
    "personalization_id",
    "lang",
    "kdt",
}

def _cookie_domain_ok(domain: str) -> bool:
    d = (domain or "").strip().lower()
    return any(d == x or d.endswith(x) for x in X_COOKIE_DOMAINS)

def _load_cookies(path: str) -> tuple[dict, str]:
    global _COOKIE_MAP_CACHE, _COOKIE_HEADER_CACHE
    if _COOKIE_MAP_CACHE is not None and _COOKIE_HEADER_CACHE is not None:
        return _COOKIE_MAP_CACHE, _COOKIE_HEADER_CACHE

    cookies = {}
    if not path or not os.path.exists(path):
        _COOKIE_MAP_CACHE, _COOKIE_HEADER_CACHE = {}, ""
        return _COOKIE_MAP_CACHE, _COOKIE_HEADER_CACHE

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split("\t")
                if len(parts) >= 7:
                    domain = (parts[0] or "").strip().lower()
                    name = (parts[5] or "").strip()
                    value = (parts[6] or "").strip()

                    if not name or not value:
                        continue
                    if not _cookie_domain_ok(domain):
                        continue
                    if name not in X_COOKIE_NAMES:
                        continue

                    cookies[name] = value
                    continue

                if "=" in line and "\t" not in line and not line.lower().startswith(("http://", "https://")):
                    name, value = line.split("=", 1)
                    name = name.strip()
                    value = value.strip()
                    if name in X_COOKIE_NAMES and value:
                        cookies[name] = value

        ordered = ["auth_token", "ct0", "twid", "guest_id", "personalization_id", "lang", "kdt"]
        header = "; ".join(f"{k}={cookies[k]}" for k in ordered if cookies.get(k))

        _dbg(
            "cookie loaded | path=%s count=%s header_len=%s ct0=%s auth=%s",
            path,
            len(cookies),
            len(header),
            bool(cookies.get("ct0")),
            bool(cookies.get("auth_token")),
        )

        _COOKIE_MAP_CACHE, _COOKIE_HEADER_CACHE = cookies, header
        return _COOKIE_MAP_CACHE, _COOKIE_HEADER_CACHE

    except Exception as e:
        _dbg("cookie load failed | path=%s err=%r", path, e)
        _COOKIE_MAP_CACHE, _COOKIE_HEADER_CACHE = {}, ""
        return _COOKIE_MAP_CACHE, _COOKIE_HEADER_CACHE

async def _resolve_short_url(url:str)->str:
    session=await get_http_session()
    async with session.get(url,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
        final=str(resp.url)
        _dbg("short resolve | from=%s to=%s status=%s",url,final,resp.status)
        return final

def _extract_status_id(url:str)->str:
    text=(url or "").strip()
    m=STATUS_RE.search(text)
    if m: return (m.group(1) or "").strip()
    return ""

def _build_api_headers() -> dict:
    cookie_map, cookie_header = _load_cookies(COOKIES_PATH)
    csrf = cookie_map.get("ct0", "")
    auth = cookie_map.get("auth_token", "")

    if not csrf or not auth or not cookie_header:
        return {}

    _dbg("api headers build | cookie_len=%s ct0_len=%s auth_len=%s", len(cookie_header), len(csrf), len(auth))

    return {
        "authorization": "Bearer " + AUTH_TOKEN,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "x-twitter-active-user": "yes",
        "x-csrf-token": csrf,
        "cookie": cookie_header,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "accept": "*/*",
        "referer": "https://x.com/",
        "origin": "https://x.com",
    }

def _build_api_query(tweet_id:str)->str:
    variables={"tweetId":tweet_id,"withCommunity":False,"includePromotedContent":False,"withVoice":False}
    features={
        "creator_subscriptions_tweet_preview_api_enabled":True,
        "tweetypie_unmention_optimization_enabled":True,
        "responsive_web_edit_tweet_api_enabled":True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled":True,
        "view_counts_everywhere_api_enabled":True,
        "longform_notetweets_consumption_enabled":True,
        "responsive_web_twitter_article_tweet_consumption_enabled":False,
        "tweet_awards_web_tipping_enabled":False,
        "freedom_of_speech_not_reach_fetch_enabled":True,
        "standardized_nudges_misinfo":True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":True,
        "longform_notetweets_rich_text_read_enabled":True,
        "longform_notetweets_inline_media_enabled":True,
        "responsive_web_graphql_exclude_directive_enabled":True,
        "verified_phone_label_enabled":False,
        "responsive_web_media_download_video_enabled":False,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled":False,
        "responsive_web_graphql_timeline_navigation_enabled":True,
        "responsive_web_enhance_cards_enabled":False,
    }
    field_toggles={"withArticleRichContentState":False}
    return urlencode({"variables":json.dumps(variables,separators=(",",":")),"features":json.dumps(features,separators=(",",":")),"fieldToggles":json.dumps(field_toggles,separators=(",",":"))})

async def _get_tweet_api(tweet_id:str)->dict:
    headers=_build_api_headers()
    if not headers: raise RuntimeError("invalid X auth cookies")
    req_url=API_ENDPOINT+"?"+_build_api_query(tweet_id)
    session=await get_http_session()
    _dbg("api fetch start | tweet_id=%s",tweet_id)
    async with session.get(req_url,headers=headers,timeout=aiohttp.ClientTimeout(total=30),allow_redirects=True) as resp:
        text=await resp.text()
        _dbg("api fetch done | status=%s body_len=%s",resp.status,len(text))
        if resp.status!=200:
            raise RuntimeError(f"invalid response code: HTTP {resp.status}")
    data=json.loads(text)
    result=((data.get("data") or {}).get("tweetResult") or {}).get("result")
    if not result: raise RuntimeError("tweet unavailable")
    if result.get("__typename")=="TweetUnavailable": raise RuntimeError("tweet unavailable")
    if result.get("tweet") and isinstance(result["tweet"],dict):
        legacy=((result["tweet"].get("legacy")) or {})
    else:
        legacy=(result.get("legacy") or {})
    if not legacy: raise RuntimeError("tweet data not found")
    return legacy

def _pick_media_entities(tweet:dict)->list:
    entities=tweet.get("entities") or {}
    extended=tweet.get("extended_entities") or {}
    if entities.get("media"): return entities["media"]
    if extended.get("media"): return extended["media"]
    return []

def _clean_tweet_caption(text:str)->str:
    text=html.unescape((text or "").strip())
    if not text:
        return ""
    text=re.sub(r"https?://t\.co/\w+","",text,flags=re.I)
    text=re.sub(r"\s+"," ",text).strip()
    return text

def _fallback_title(items:list)->str:
    if len(items)==1 and str(items[0].get("type") or "").strip().lower()=="video":
        return "X Video"
    return "X Media"
    
def _resolution_from_url(url:str)->tuple[int,int]:
    m=RES_RE.search(url or "")
    if not m: return 0,0
    try: return int(m.group(1)),int(m.group(2))
    except Exception: return 0,0

def _best_video_variant(media:dict)->dict|None:
    info=media.get("video_info") or {}
    variants=info.get("variants") or []
    mp4=[v for v in variants if (v.get("content_type") or "")=="video/mp4" and v.get("url")]
    if not mp4: return None
    mp4.sort(key=lambda v:int(v.get("bitrate") or 0),reverse=True)
    return mp4[0]

def _parse_tweet_media(tweet:dict)->dict:
    entities=_pick_media_entities(tweet)
    if not entities: raise RuntimeError("tweet has no media")
    items=[]
    for media in entities:
        mtype=(media.get("type") or "").strip().lower()
        if mtype=="photo":
            url=(media.get("media_url_https") or "").strip()
            if url: items.append({"type":"photo","url":url})
            continue
        if mtype in ("video","animated_gif"):
            best=_best_video_variant(media)
            if best and best.get("url"):
                w,h=_resolution_from_url(best["url"])
                items.append({"type":"video","url":best["url"],"bitrate":int(best.get("bitrate") or 0),"width":w,"height":h})
    if not items: raise RuntimeError("tweet has no downloadable media")
    caption=_clean_tweet_caption(tweet.get("full_text") or "")
    fallback=_fallback_title(items)
    title=caption or fallback
    _dbg("tweet media parsed | count=%s types=%s caption=%s title=%s",len(items),[x.get("type") for x in items],bool(caption),title)
    return {"items":items,"caption":caption,"title":title}

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
    log.info("X aria2c start | url=%s out=%s",media_url,out_path)
    log.debug("X aria2c cmd | %s"," ".join(cmd))
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
    if stderr_text: log.debug("X aria2c stderr | %s",stderr_text)
    if proc.returncode!=0: raise RuntimeError(stderr_text or f"aria2c exited with code {proc.returncode}")
    log.info("X aria2c success | out=%s",out_path)

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

async def _download_one_media(session,item:dict,bot,chat_id,status_msg_id,idx:int,total:int)->dict:
    media_type=str(item.get("type") or "").strip().lower()
    media_url=str(item.get("url") or "").strip()
    if not media_url: raise RuntimeError("media url kosong")
    ext=".mp4" if media_type=="video" else ".jpg"
    title="X Video" if media_type=="video" else "X Media"
    out_path=os.path.join(TMP_DIR,f"{uuid.uuid4().hex}_{sanitize_filename(title)}{ext}")
    headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","Referer":"https://x.com/"}
    title_text=f"Downloading {'X Video' if media_type=='video' else 'X Media'}... ({idx}/{total})"
    try:
        await _aria2c_download_with_progress(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
    except Exception as e:
        log.warning("X aria2c failed, fallback aiohttp | idx=%s url=%s err=%r",idx,media_url,e)
        if os.path.exists(out_path):
            try: os.remove(out_path)
            except Exception: pass
        await _aiohttp_download_with_progress(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
    return {"type":media_type if media_type in {"video","photo"} else "photo","path":out_path,"url":media_url}

async def _download_x_items(parsed:dict,bot,chat_id,status_msg_id)->dict:
    items=parsed.get("items") or []
    title=(parsed.get("title") or "").strip() or _fallback_title(items)
    session=await get_http_session()
    downloaded_items=[]; total=len(items)
    for idx,item in enumerate(items,start=1):
        downloaded=await _download_one_media(session,item,bot,chat_id,status_msg_id,idx,total)
        downloaded_items.append(downloaded)
    if len(downloaded_items)==1:
        return {"path":downloaded_items[0]["path"],"title":title}
    return {"items":downloaded_items,"title":title}

async def twitter_scrape_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    del fmt_key,format_id,has_audio
    if not metadata_ready:
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>Scraping X media...</b>")
    text=(raw_url or "").strip()
    if SHORT_RE.match(text):
        text=await _resolve_short_url(text)
    tweet_id=_extract_status_id(text)
    _dbg("scrape start | url=%s tweet_id=%s",text,tweet_id)
    if not tweet_id:
        raise RuntimeError("failed to extract x status id")
    tweet=await _get_tweet_api(tweet_id)
    parsed=_parse_tweet_media(tweet)
    result=await _download_x_items(parsed,bot,chat_id,status_msg_id)
    _dbg("scrape success | result_type=%s","album" if isinstance(result,dict) and result.get("items") else "single")
    return result

async def twitter_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False,metadata_ready:bool=False):
    try:
        return await twitter_scrape_download(
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
        log.exception("X scraping failed, fallback to yt-dlp | url=%s err=%r",raw_url,e)
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>X scraping failed</b>\n\n<i>Fallback to yt-dlp...</i>")
        return await ytdlp_download(raw_url,fmt_key,bot,chat_id,status_msg_id,format_id=format_id,has_audio=has_audio)
        