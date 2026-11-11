import os
import re
import json
import html
import uuid
import time
import logging
import asyncio
import aiohttp
import aiofiles
from urllib.parse import urlparse
from collections import OrderedDict
from telegram.error import RetryAfter
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from curl_cffi.requests import AsyncSession
from handlers.dl.utils import check_media_size_limit, FileSizeLimitExceeded
from handlers.dl.progress import render_progress_text, edit_status, TransferStats, prune_edit_cache

log=logging.getLogger(__name__)
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
_LAST_IG_STATUS_TEXT={}

IG_PROGRESS=os.getenv("INSTAGRAM_PROGRESS","1").lower() in ("1","true","on","yes")
IG_PROGRESS_INTERVAL=float(os.getenv("INSTAGRAM_PROGRESS_INTERVAL","1.5"))
IG_AIOHTTP_CHUNK_SIZE=int(os.getenv("INSTAGRAM_AIOHTTP_CHUNK_SIZE",str(512*1024)))
IG_CAROUSEL_CONCURRENCY=int(os.getenv("INSTAGRAM_CAROUSEL_CONCURRENCY","6"))
IG_PROGRESS_MIN_BYTES=int(os.getenv("INSTAGRAM_PROGRESS_MIN_BYTES",str(8*1024*1024)))
IG_META_CACHE_TTL=float(os.getenv("INSTAGRAM_META_CACHE_TTL","1800"))
IG_META_CACHE_MAX=int(os.getenv("INSTAGRAM_META_CACHE_MAX","500"))
IG_THUMB_WIDTH=int(os.getenv("INSTAGRAM_THUMB_WIDTH","320"))
IG_META_CACHE: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()

IG_HOME_URL="https://www.instagram.com/"
GRAPHQL_ENDPOINT="https://www.instagram.com/api/graphql"
POLARIS_ACTION="PolarisLoggedOutDesktopWWWPostRootContentQuery"
GRAPHQL_DOC_ID="27130156389949648"
IG_APP_ID="936619743392459"
IG_ASBD_ID="359341"
IG_B64_ALPHABET="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
IG_TOKEN_TTL=1800.0
_IG_HTTP=None
_IG_HTTP_LOCK=asyncio.Lock()
_IG_TOKEN_CACHE={"lsd":"","csrf":"","ts":0.0}
IG_WEB_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
WEB_HEADERS={
    "User-Agent":IG_WEB_USER_AGENT,
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
}

class RetryableDownloadError(RuntimeError):
    pass

def is_instagram_url(url:str)->bool:
    try:
        host=(urlparse((url or "").strip()).hostname or "").lower()
        return host=="instagram.com" or host.endswith(".instagram.com") or host=="instagr.am"
    except Exception as e:
        text=(url or "").lower()
        log.warning("Failed to parse Instagram URL host | url=%s err=%s",url,e)
        return "instagram.com" in text or "instagr.am" in text

def _ensure_tmp_dir():
    os.makedirs(TMP_DIR,exist_ok=True)

def _truncate_text(text:str,limit:int)->str:
    text=(text or "").strip()
    if limit<=0:
        return ""
    if len(text)<=limit:
        return text
    if limit<=3:
        return "."*limit
    return text[:limit-3].rstrip()+"..."

def _normalize_instagram_url(raw_url: str) -> str:
    text = (raw_url or "").strip()
    if not text:
        return text
    if not re.match(r"^https?://", text, flags=re.I):
        text = "https://" + text
    p = urlparse(text)
    scheme = p.scheme or "https"
    host = (p.netloc or "").lower()
    path = p.path or "/"
    if path != "/" and not path.endswith("/"):
        path += "/"
    return f"{scheme}://{host}{path}"

def _extract_shortcode(raw_url:str)->str:
    text=_normalize_instagram_url(raw_url)
    m=re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",text,flags=re.I)
    return (m.group(1) if m else "").strip()
    
def _extract_meta(html_text: str, key: str) -> str:
    for pat in (
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(key)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(key)}["\']',
    ):
        m = re.search(pat, html_text or "", flags=re.I)
        if m:
            return html.unescape((m.group(1) or "").strip())
    return ""

def _extract_json_ld_metadata(html_text: str) -> dict:
    matches = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_text or "", flags=re.I | re.S)
    for raw in matches:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(html.unescape(raw))
        except Exception as e:
            log.debug("Instagram JSON-LD parse failed | err=%r", e)
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            caption = str(obj.get("caption") or obj.get("description") or obj.get("name") or "").strip()
            username = ""
            nickname = ""
            author = obj.get("author")
            if isinstance(author, dict):
                nickname = str(author.get("name") or "").strip()
                alt = str(author.get("alternateName") or "").strip()
                if alt:
                    username = alt.lstrip("@")
                elif nickname.startswith("@"):
                    username = nickname.lstrip("@")
                    nickname = ""
            if caption or username or nickname:
                return {"caption": caption, "username": username, "nickname": nickname}
    return {"caption": "", "username": "", "nickname": ""}

def _fallback_caption_meta(primary_html:str,secondary_html:str="")->dict:
    for source in (primary_html or "",secondary_html or ""):
        meta=_extract_json_ld_metadata(source)
        if meta.get("caption") or meta.get("username") or meta.get("nickname"):
            return meta
    caption=(
        _extract_meta(primary_html,"og:description")
        or _extract_meta(primary_html,"twitter:description")
        or _extract_meta(primary_html,"description")
        or _extract_meta(primary_html,"og:title")
        or _extract_meta(primary_html,"twitter:title")
        or _extract_meta(secondary_html,"og:description")
        or _extract_meta(secondary_html,"twitter:description")
        or _extract_meta(secondary_html,"description")
        or _extract_meta(secondary_html,"og:title")
        or _extract_meta(secondary_html,"twitter:title")
    ).strip()
    username=""
    nickname=""
    m=re.search(r"@([A-Za-z0-9._]+)",caption)
    if m:
        username=m.group(1).strip()
    title_tag=(
        _extract_meta(primary_html,"og:title")
        or _extract_meta(primary_html,"twitter:title")
        or _extract_meta(secondary_html,"og:title")
        or _extract_meta(secondary_html,"twitter:title")
    ).strip()
    if title_tag:
        nickname=title_tag.split(" on Instagram",1)[0].strip() if " on Instagram" in title_tag else title_tag
    return {"caption":caption,"username":username,"nickname":nickname}
    
async def _fetch_instagram_caption_meta(raw_url:str)->dict:
    session=await get_http_session()
    url=_normalize_instagram_url(raw_url)
    last_err=None
    for target in (url.rstrip("/")+"/embed/captioned/",url):
        try:
            log.info("Instagram caption metadata fetch try | url=%s target=%s",raw_url,target)
            async with session.get(target,headers=WEB_HEADERS,timeout=aiohttp.ClientTimeout(total=25),allow_redirects=True) as resp:
                html_text=await resp.text()
                log.info("Instagram caption metadata fetch result | target=%s status=%s final=%s len=%s",target,resp.status,resp.url,len(html_text or ""))
                if resp.status>=400:
                    raise RuntimeError(f"Instagram metadata HTTP {resp.status}")
            meta=_extract_json_ld_metadata(html_text)
            if meta.get("caption") or meta.get("username") or meta.get("nickname"):
                log.info("Instagram caption metadata JSON-LD success | target=%s caption_len=%s username=%r nickname=%r",target,len(meta.get("caption") or ""),meta.get("username"),meta.get("nickname"))
                return meta
            meta=_fallback_caption_meta(html_text)
            if meta.get("caption") or meta.get("username") or meta.get("nickname"):
                log.info("Instagram fallback metadata success | target=%s caption_len=%s username=%r nickname=%r",target,len(meta.get("caption") or ""),meta.get("username"),meta.get("nickname"))
                return meta
            log.warning("Instagram caption metadata empty | target=%s",target)
        except Exception as e:
            last_err=e
            log.warning("Instagram caption metadata target failed | target=%s err=%r",target,e)
    if last_err:
        log.warning("Instagram metadata scrape failed | url=%s err=%r",raw_url,last_err)
    return {"caption":"","username":"","nickname":""}

def _caption_from_media(media:dict)->str:
    if not isinstance(media,dict):
        return ""
    edges=((media.get("edge_media_to_caption") or {}).get("edges")) or []
    if isinstance(edges,list) and edges:
        text=((edges[0] or {}).get("node") or {}).get("text") or ""
        if str(text).strip():
            return str(text).strip()
    caption_obj=media.get("caption") or {}
    if isinstance(caption_obj,dict):
        text=(caption_obj.get("text") or "").strip()
        if text:
            return text
    for key in ("accessibility_caption","title"):
        text=str(media.get(key) or "").strip()
        if text:
            return text
    return ""

def _extract_lsd_token(html_text:str)->str:
    m=re.search(r'\["LSD",\[\],\{"token":"([^"]+)"',html_text or "")
    if m:
        return m.group(1)
    m=re.search(r'<script[^>]*\bid="__eqmc"[^>]*>(.*?)</script>',html_text or "",flags=re.S)
    if m:
        try:
            return str(json.loads(m.group(1)).get("l") or "")
        except Exception as e:
            log.debug("Instagram LSD eqmc parse failed | err=%r",e)
    return ""

def _cookie_value(session,name:str)->str:
    try:
        val=session.cookies.get(name)
        if val:
            return str(val)
    except Exception:
        pass
    return ""

async def _ig_http()->AsyncSession:
    global _IG_HTTP
    if _IG_HTTP is None:
        _IG_HTTP=AsyncSession(impersonate="chrome")
    return _IG_HTTP

async def _ig_reset_http():
    global _IG_HTTP
    old=_IG_HTTP
    _IG_HTTP=None
    _IG_TOKEN_CACHE.update(lsd="",csrf="",ts=0.0)
    IG_META_CACHE.clear()
    if old is not None:
        try:
            await old.close()
        except Exception as e:
            log.debug("Instagram session close failed | err=%r",e)
    try:
        from .mobile_session import reset_session
        reset_session()
    except Exception:
        pass


close_instagram_session=_ig_reset_http

async def _ig_tokens(force:bool=False)->tuple[str,str]:
    async with _IG_HTTP_LOCK:
        now=time.monotonic()
        if not force and _IG_TOKEN_CACHE["lsd"] and (now-_IG_TOKEN_CACHE["ts"])<IG_TOKEN_TTL:
            return _IG_TOKEN_CACHE["lsd"],_IG_TOKEN_CACHE["csrf"]
        session=await _ig_http()
        lsd=""
        csrf=""
        try:
            resp=await session.get(IG_HOME_URL,timeout=20)
            lsd=_extract_lsd_token(resp.text or "")
            csrf=_cookie_value(session,"csrftoken")
        except Exception as e:
            log.warning("Instagram session bootstrap failed | err=%r",e)
        if lsd:
            _IG_TOKEN_CACHE.update(lsd=lsd,csrf=csrf,ts=now)
        return _IG_TOKEN_CACHE["lsd"],_IG_TOKEN_CACHE["csrf"]


async def warmup_instagram_session()->bool:
    lsd,csrf=await _ig_tokens()
    if lsd:
        log.info("Instagram session warmed | lsd=%s csrf=%s",bool(lsd),bool(csrf))
        return True
    log.warning("Instagram session warmup failed | lsd empty")
    return False


def clear_instagram_meta_cache():
    IG_META_CACHE.clear()

def _shortcode_to_pk(shortcode:str)->str:
    sc=(shortcode or "").strip()
    if len(sc)>28:
        sc=sc[:-28]
    value=0
    for ch in sc:
        idx=IG_B64_ALPHABET.find(ch)
        if idx<0:
            raise RuntimeError(f"Instagram invalid shortcode: {shortcode}")
        value=value*64+idx
    return str(value)

def _pick_largest(candidates:list[dict]|None)->tuple[str,int,int]:
    best=None
    for c in candidates or []:
        if not isinstance(c,dict):
            continue
        url=str(c.get("url") or "").strip()
        if not url:
            continue
        w=int(c.get("width") or 0)
        h=int(c.get("height") or 0)
        area=w*h
        if best is None or area>best[0]:
            best=(area,url,w,h)
    return (best[1],best[2],best[3]) if best else ("",0,0)


def _pick_thumb(candidates:list[dict]|None,orig_w:int,orig_h:int,target:int)->str:
    if not candidates:
        return ""
    valid=[]
    for c in candidates:
        if not isinstance(c,dict):
            continue
        url=str(c.get("url") or "").strip()
        w=int(c.get("width") or 0)
        h=int(c.get("height") or 0)
        if url and w>0 and h>0:
            valid.append((w,h,url))
    if not valid:
        return ""
    want_ratio=(orig_w/orig_h) if (orig_w and orig_h) else 0.0
    matching=[v for v in valid if want_ratio and abs((v[0]/v[1])-want_ratio)<0.08]
    pool=matching or valid
    big_enough=[v for v in pool if v[0]>=target]
    if big_enough:
        return min(big_enough,key=lambda v:v[0])[2]
    return max(pool,key=lambda v:v[0])[2]


def _node_duration(node:dict)->int:
    for key in ("video_duration","duration"):
        raw=node.get(key)
        try:
            value=float(raw)
        except (TypeError,ValueError):
            continue
        if value>0:
            return max(int(round(value)),0)
    return 0


def _polaris_node_item(node:dict|None)->dict|None:
    if not isinstance(node,dict):
        return None
    mt=int(node.get("media_type") or 0)
    vv=node.get("video_versions") or []
    dims=node.get("dimensions") or {}
    w=int(node.get("original_width") or dims.get("width") or 0)
    h=int(node.get("original_height") or dims.get("height") or 0)
    candidates=(node.get("image_versions2") or {}).get("candidates")
    is_video=mt==2 or bool(vv) or bool(node.get("is_video")) or bool(node.get("video_url"))

    if is_video:
        url=""
        if isinstance(vv,list) and vv:
            url=str((vv[0] or {}).get("url") or "").strip()
        if not url:
            url=str(node.get("video_url") or "").strip()
        if not url:
            url=str(node.get("display_url") or "").strip()
        if not url:
            return None
        thumb=_pick_thumb(candidates,w,h,IG_THUMB_WIDTH)
        if not thumb:
            thumb=str(node.get("display_url") or node.get("display_uri") or "").strip()
        return {
            "type":"video",
            "url":url,
            "thumbnail":thumb,
            "width":w,
            "height":h,
            "duration":_node_duration(node),
        }

    url,cand_w,cand_h=_pick_largest(candidates)
    if not url:
        url=str(node.get("display_url") or node.get("display_uri") or "").strip()
    if not url:
        return None
    if not w:
        w=cand_w
    if not h:
        h=cand_h
    return {"type":"photo","url":url,"thumbnail":"","width":w,"height":h,"duration":0}

def _parse_gql_media(media:dict)->dict:
    if not isinstance(media,dict):
        return {"caption":"","username":"","nickname":"","items":[]}
    owner=media.get("owner") or media.get("user") or {}
    username=str(owner.get("username") or "").strip()
    nickname=str(owner.get("full_name") or owner.get("fullName") or "").strip()
    caption=_caption_from_media(media)
    items=[]

    carousel=media.get("carousel_media")
    sidecar_edges=((media.get("edge_sidecar_to_children") or {}).get("edges")) or []

    if isinstance(carousel,list) and carousel:
        for node in carousel:
            it=_polaris_node_item(node)
            if it:
                items.append(it)
    elif sidecar_edges:
        for edge in sidecar_edges:
            node=(edge or {}).get("node") or {}
            it=_polaris_node_item(node)
            if it:
                items.append(it)
    else:
        it=_polaris_node_item(media)
        if it:
            items.append(it)

    return {"caption":caption,"username":username,"nickname":nickname,"items":items}

async def _fetch_gql_metadata(raw_url:str)->dict:
    shortcode=_extract_shortcode(raw_url)
    if not shortcode:
        raise RuntimeError("Instagram shortcode not found")
    media_id=_shortcode_to_pk(shortcode)

    for attempt in range(2):
        lsd,csrf=await _ig_tokens(force=attempt>0)
        session=await _ig_http()
        headers={
            "X-IG-App-ID":IG_APP_ID,
            "X-ASBD-ID":IG_ASBD_ID,
            "X-IG-WWW-Claim":"0",
            "Origin":"https://www.instagram.com",
            "Accept":"*/*",
            "X-Requested-With":"XMLHttpRequest",
            "X-FB-Friendly-Name":POLARIS_ACTION,
            "Referer":f"https://www.instagram.com/p/{shortcode}/",
        }
        if lsd:
            headers["X-FB-LSD"]=lsd
        if csrf:
            headers["X-CSRFToken"]=csrf

        body={
            "lsd":lsd or "",
            "fb_api_caller_class":"RelayModern",
            "fb_api_req_friendly_name":POLARIS_ACTION,
            "server_timestamps":"true",
            "variables":json.dumps({"media_id":media_id},separators=(",",":")),
            "doc_id":GRAPHQL_DOC_ID,
        }

        try:
            resp=await session.post(GRAPHQL_ENDPOINT,data=body,headers=headers,timeout=25)
        except Exception as e:
            if attempt==0:
                await _ig_reset_http()
                continue
            raise RuntimeError(f"Instagram GraphQL request failed: {e}") from e

        text=resp.text or ""
        log.info("Instagram GraphQL metadata fetch | status=%s len=%s attempt=%s",resp.status_code,len(text),attempt+1)

        if resp.status_code in (401,403):
            if attempt==0:
                await _ig_reset_http()
                continue
            raise RuntimeError(f"Instagram GraphQL HTTP {resp.status_code}")

        if resp.status_code>=400:
            raise RuntimeError(f"Instagram GraphQL HTTP {resp.status_code}")

        try:
            data=json.loads(text)
        except Exception as e:
            raise RuntimeError(f"Instagram GraphQL invalid JSON: {e}") from e

        if not isinstance(data,dict):
            raise RuntimeError("Instagram GraphQL invalid response")

        media=(data.get("data") or {}).get("xig_polaris_media")
        if not isinstance(media,dict):
            media=_json_find_media(data)
        if not isinstance(media,dict):
            errs=data.get("errors")
            raise RuntimeError(f"Instagram GraphQL media not found | errors={errs}")

        prod=media.get("if_not_gated_logged_out")
        if not isinstance(prod,dict) or not prod:
            gate=media.get("gating_ruling") or {}
            title=str(gate.get("title") or "").strip()
            desc=str(gate.get("description") or "").strip()
            gate_info=f"{title}: {desc}".strip(": ") if (title or desc) else "Post is gated/age-restricted"
            raise RuntimeError(f"Instagram post not available without login | {gate_info}")

        parsed=_parse_gql_media(prod)
        if parsed.get("items") or parsed.get("caption") or parsed.get("username") or parsed.get("nickname"):
            return parsed

        raise RuntimeError("Instagram GraphQL metadata empty")

    raise RuntimeError("Instagram GraphQL metadata failed")

def _json_find_media(obj):
    if isinstance(obj,dict):
        for key in ("xdt_shortcode_media","shortcode_media"):
            val=obj.get(key)
            if isinstance(val,dict):
                return val
        for val in obj.values():
            found=_json_find_media(val)
            if isinstance(found,dict):
                return found
    elif isinstance(obj,list):
        for item in obj:
            found=_json_find_media(item)
            if isinstance(found,dict):
                return found
    return None

def _extract_context_json_string(serverjs_blob:str)->str:
    if not serverjs_blob:
        return ""
    m=re.search(r'"contextJSON"\s*:\s*"((?:\\.|[^"\\])*)"',serverjs_blob,flags=re.S)
    if m:
        try:
            return json.loads('"'+m.group(1)+'"')
        except Exception as e:
            log.debug("Instagram embed contextJSON string decode failed | err=%r",e)
    m=re.search(r'"contextJSON"\s*:\s*(\{.*?\})(?:,|})',serverjs_blob,flags=re.S)
    return m.group(1) if m else ""

def _extract_embed_shortcode_media(html_text:str):
    m=re.search(r'new ServerJS\(\)\);s\.handle\((\{.*?\})\);requireLazy',html_text or "",flags=re.S)
    if not m:
        return None
    ctx_raw=_extract_context_json_string(m.group(1))
    if not ctx_raw:
        return None
    try:
        ctx_data=json.loads(ctx_raw)
    except Exception as e:
        log.debug("Instagram embed contextJSON parse failed | err=%r",e)
        return None
    media=(ctx_data.get("gql_data") or {}).get("shortcode_media")
    return media if isinstance(media,dict) else None

async def _fetch_embed_metadata(raw_url:str)->dict:
    shortcode=_extract_shortcode(raw_url)
    if not shortcode:
        raise RuntimeError("Instagram shortcode not found")
    embed_url=f"https://www.instagram.com/p/{shortcode}/embed/captioned"
    session=await get_http_session()
    async with session.get(embed_url,headers=WEB_HEADERS,timeout=aiohttp.ClientTimeout(total=25),allow_redirects=True) as resp:
        html_text=await resp.text()
        log.info("Instagram embed metadata fetch | status=%s final=%s len=%s",resp.status,resp.url,len(html_text or ""))
        if resp.status>=400:
            raise RuntimeError(f"Instagram embed HTTP {resp.status}")
    media=_extract_embed_shortcode_media(html_text)
    if not isinstance(media,dict):
        raise RuntimeError("Instagram embed shortcode_media not found")
    parsed=_parse_gql_media(media)
    if not parsed.get("items") and not parsed.get("caption") and not parsed.get("username") and not parsed.get("nickname"):
        raise RuntimeError("Instagram embed media empty")
    if not parsed.get("caption") or not parsed.get("username") or not parsed.get("nickname"):
        fallback=_fallback_caption_meta(html_text)
        parsed["caption"]=parsed.get("caption") or fallback.get("caption") or ""
        parsed["username"]=parsed.get("username") or fallback.get("username") or ""
        parsed["nickname"]=parsed.get("nickname") or fallback.get("nickname") or ""
    return parsed
    
def _meta_cache_get(key:str)->dict|None:
    if not key:
        return None
    hit=IG_META_CACHE.get(key)
    if not hit:
        return None
    ts,value=hit
    if (time.monotonic()-ts)>=IG_META_CACHE_TTL:
        IG_META_CACHE.pop(key,None)
        return None
    IG_META_CACHE.move_to_end(key)
    return value


def _meta_cache_put(key:str,value:dict):
    if not key or not value:
        return
    IG_META_CACHE[key]=(
        time.monotonic(),
        {
            "caption":value.get("caption") or "",
            "username":value.get("username") or "",
            "nickname":value.get("nickname") or "",
            "items":list(value.get("items") or []),
        },
    )
    IG_META_CACHE.move_to_end(key)
    while len(IG_META_CACHE)>max(1,IG_META_CACHE_MAX):
        IG_META_CACHE.popitem(last=False)


def _meta_cache_prune():
    now=time.monotonic()
    stale=[k for k,(ts,_) in IG_META_CACHE.items() if (now-ts)>=IG_META_CACHE_TTL]
    for key in stale:
        IG_META_CACHE.pop(key,None)


async def _fetch_instagram_metadata(raw_url:str)->dict:
    errors=[]
    try:
        meta=await _fetch_gql_metadata(raw_url)
        if meta.get("caption") or meta.get("username") or meta.get("nickname") or meta.get("items"):
            return meta
    except Exception as e:
        errors.append(f"_fetch_gql_metadata: {e}")

    try:
        from .mobile_session import fetch_media_via_session
        shortcode=_extract_shortcode(raw_url)
        mob=await fetch_media_via_session(shortcode)
        if mob and (mob.get("items") or mob.get("caption")):
            return mob
    except Exception as e:
        errors.append(f"mobile_session: {e}")

    try:
        meta=await _fetch_embed_metadata(raw_url)
        if meta.get("caption") or meta.get("username") or meta.get("nickname") or meta.get("items"):
            return meta
    except Exception as e:
        errors.append(f"_fetch_embed_metadata: {e}")

    raise RuntimeError(" ; ".join(errors) if errors else "Instagram metadata not found")

def _normalize_caption_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{3,}", "\n\n", text)

def _build_title(meta:dict,media_type:str,count:int=1)->str:
    nickname=(meta.get("nickname") or "").strip()
    username=(meta.get("username") or "").strip()
    caption=_normalize_caption_text(meta.get("caption") or "")
    if nickname and username:
        base=f"{nickname} (@{username})"
    elif username:
        base=f"@{username}"
    elif nickname:
        base=nickname
    else:
        base="Instagram Media" if count>1 else ("Instagram Video" if media_type=="video" else "Instagram Post")
    full=f"{base} - {caption}".strip() if caption else base
    return full[:1024].rstrip()

_STRIP_QUERY_HOSTS=("cdninstagram.com","fbcdn.net")

def _uniq_media_urls(items:list[str])->list[str]:
    out=[]
    seen=set()
    for item in items:
        raw=(item or "").strip()
        if not raw:
            continue
        try:
            parsed=urlparse(raw)
            host=(parsed.hostname or "").lower()
            path=parsed.path or ""
            if any(host==h or host.endswith("."+h) for h in _STRIP_QUERY_HOSTS):
                normalized=f"{parsed.scheme}://{host}{path}"
            else:
                normalized=parsed._replace(fragment="").geturl()
        except Exception as e:
            log.warning("Failed to normalize Instagram media URL | url=%s err=%s",raw,e)
            normalized=raw
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(raw)
    return out

from utils.config import NEOXR_API_KEY

async def _fetch_neoxr_api(raw_url: str) -> dict:
    if not NEOXR_API_KEY:
        raise RuntimeError("NEOXR_API_KEY is not set in config")
        
    session = await get_http_session()
    api_url = "https://api.neoxr.eu/api/ig"
    params = {"url": raw_url, "apikey": NEOXR_API_KEY}
    
    log.info("Instagram Neoxr API fetch start | url=%s", raw_url)
    try:
        async with session.get(api_url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:100]}")
            data = await resp.json()
            
        if not data.get("status"):
            raise RuntimeError(data.get("msg") or data.get("message") or "API returned false status")
            
        results = data.get("data") or []
        urls = []
        for item in results:
            media_url = item.get("url")
            if media_url:
                urls.append(media_url)
                
        if not urls:
            raise RuntimeError("No URLs found in Neoxr response data")
            
        return {"status": True, "source": "Neoxr API", "urls": urls}
    except Exception as e:
        log.warning("Neoxr API failed | err=%r", e)
        return {"status": False, "message": str(e)}

_VIDEO_EXTS=(".mp4",".mov",".m4v",".webm")
_PHOTO_EXTS=(".jpg",".jpeg",".png",".webp")

def detect_media_signature(header:bytes)->tuple[str,str]:
    if not header:
        return "",""
    if header[:3]==b"\xff\xd8\xff":
        return "photo",".jpg"
    if header[:8]==b"\x89PNG\r\n\x1a\n":
        return "photo",".png"
    if header[:4]==b"RIFF" and header[8:12]==b"WEBP":
        return "photo",".webp"
    if header[4:8] in (b"ftyp",b"moov",b"mdat"):
        return "video",".mp4"
    if header[:4]==b"\x1aE\xdf\xa3":
        return "video",".webm"
    return "",""

def _guess_media_type_from_url(url:str)->str:
    parsed=urlparse(url or "")
    path=(parsed.path or "").lower()
    if path.endswith(_VIDEO_EXTS):
        return "video"
    if path.endswith(_PHOTO_EXTS):
        return "photo"
    return "photo"

def _guess_ext(url:str,content_type:str,media_type:str="")->str:
    path=(urlparse(url or "").path or "").lower()
    for ext in (".mp4",".mov",".m4v",".webm",".jpg",".jpeg",".png",".webp"):
        if path.endswith(ext):
            return ext
    ctype=(content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("video/") or media_type=="video":
        return ".mp4"
    if ctype=="image/png":
        return ".png"
    if ctype=="image/webp":
        return ".webp"
    return ".jpg"

def _media_types_from_meta(items:list[dict])->list[str]:
    out=[]
    for item in items or []:
        kind=str((item or {}).get("type") or "").strip().lower()
        if kind in ("photo","video"):
            out.append(kind)
    return out

def _filter_urls_for_media(urls:list[str],fmt_key:str="video",meta_items:list[dict]|None=None)->list[str]:
    urls=[u for u in urls if u]
    videos=[u for u in urls if _guess_media_type_from_url(u)=="video"]
    photos=[u for u in urls if _guess_media_type_from_url(u)!="video"]
    if fmt_key=="mp3":
        return videos[:1] if videos else []
    wanted=_media_types_from_meta(meta_items or [])
    if wanted and len(wanted)>1:
        selected=[]
        used=set()
        for kind in wanted:
            pool=videos if kind=="video" else photos
            for u in pool:
                if u in used:
                    continue
                selected.append(u)
                used.add(u)
                break
        if selected:
            log.info("Instagram media filtered by metadata | wanted=%s raw=%s selected=%s",wanted,len(urls),len(selected))
            return selected
    if fmt_key in ("video","mp4"):
        if videos and photos:
            log.info("Instagram mixed media without metadata, keeping all | raw=%s videos=%s photos=%s",len(urls),len(videos),len(photos))
            return urls
        if videos:
            log.info("Instagram format requested video-only | raw=%s videos=%s selected=%s",len(urls),len(videos),len(videos))
            return videos
        log.info("Instagram has no video, falling back to photos | raw=%s photos=%s",len(urls),len(photos))
    if wanted:
        selected=[]
        used=set()
        for kind in wanted:
            pool=videos if kind=="video" else photos
            for u in pool:
                if u in used:
                    continue
                selected.append(u)
                used.add(u)
                break
        if selected:
            log.info("Instagram media filtered by metadata | wanted=%s raw=%s selected=%s",wanted,len(urls),len(selected))
            return selected
    if videos and photos:
        log.info("Instagram mixed media without metadata, keeping all | raw=%s videos=%s photos=%s",len(urls),len(videos),len(photos))
        return urls
    return videos or photos

def _is_valid_media_content_type(content_type:str)->bool:
    ctype=(content_type or "").split(";")[0].strip().lower()
    return bool(ctype and (ctype.startswith("image/") or ctype.startswith("video/") or ctype=="application/octet-stream"))

def _is_retryable_download_exception(exc:Exception)->bool:
    return isinstance(exc,(RetryableDownloadError,aiohttp.ClientError,asyncio.TimeoutError))

def _download_headers_for_url(url:str,source:str="")->dict:
    lower_url=(url or "").lower()
    headers={
        "User-Agent":USER_AGENT,
        "Accept":"*/*",
        "Accept-Language":"en-US,en;q=0.9",
        "Accept-Encoding":"identity",
        "Connection":"keep-alive",
    }
    if "cdninstagram.com" in lower_url or "fbcdn.net" in lower_url:
        headers["Referer"]="https://www.instagram.com/"
    return headers

def _download_candidates(url:str)->list[str]:
    raw=(url or "").strip()
    return [raw] if raw else []

async def _safe_edit_status(bot,chat_id,message_id,text:str,min_interval:float=1.2):
    if not bot or not chat_id or not message_id:
        return
    cache=getattr(bot,"_ig_status_edit_cache",None)
    if cache is None:
        cache={}
        setattr(bot,"_ig_status_edit_cache",cache)
    if not message_id:
        return
    key=(int(chat_id),int(message_id))
    text=str(text or "")
    now=time.monotonic()
    prev=cache.get(key) or {}

    if prev.get("text")==text:
        return
    if now-prev.get("ts",0)<min_interval:
        return

    for _ in range(2):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            cache[key]={"text":text,"ts":time.monotonic()}
            prune_edit_cache(cache)
            return
        except RetryAfter as e:
            wait=max(int(getattr(e,"retry_after",1)),1)
            log.warning("Instagram status RetryAfter | chat_id=%s wait=%s",chat_id,wait)
            await asyncio.sleep(wait+1)
        except Exception as e:
            if "message is not modified" in str(e).lower():
                cache[key]={"text":text,"ts":time.monotonic()}
                prune_edit_cache(cache)
                return
            log.warning("Failed to edit Instagram status | chat_id=%s message_id=%s err=%r",chat_id,message_id,e)
            return

async def _safe_edit_progress(bot,chat_id,status_msg_id,title:str,downloaded:int,total:int=0,speed_bps:float=0.0,eta_seconds:float|None=None):
    if not IG_PROGRESS or not bot or not chat_id or not status_msg_id:
        return
    text=render_progress_text(title,downloaded=downloaded,total=total,speed_bps=speed_bps,eta_seconds=eta_seconds)
    await edit_status(bot,chat_id,status_msg_id,text,min_interval=2.0,label="Instagram")


def _shrink_thumb_sync(path:str,max_side:int,max_bytes:int)->str|None:
    try:
        from PIL import Image
    except Exception:
        return path if os.path.getsize(path)<=max_bytes else None
    try:
        with Image.open(path) as im:
            im=im.convert("RGB")
            if max(im.size)>max_side:
                im.thumbnail((max_side,max_side),Image.Resampling.LANCZOS)
            out=os.path.splitext(path)[0]+"_s.jpg"
            im.save(out,format="JPEG",quality=85,optimize=True)
        if os.path.getsize(out)<=max_bytes:
            _safe_remove_file(path,"thumb_replace")
            return out
        _safe_remove_file(out,"thumb_too_big")
    except Exception as e:
        log.debug("Instagram thumbnail resize failed | path=%s err=%r",path,e)
    return path if os.path.getsize(path)<=max_bytes else None


async def _fetch_thumb_file(url:str)->str|None:
    if not url:
        return None
    _ensure_tmp_dir()
    out_path=os.path.join(TMP_DIR,f"{uuid.uuid4().hex}_thumb.jpg")
    try:
        session=await get_http_session()
        async with session.get(url,headers={"User-Agent":USER_AGENT},timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status>=400:
                return None
            raw=await resp.read()
        if not raw or len(raw)<512:
            return None
        with open(out_path,"wb") as fh:
            fh.write(raw)
        shrunk=await asyncio.to_thread(_shrink_thumb_sync,out_path,IG_THUMB_WIDTH,200*1024)
        if not shrunk and os.path.exists(out_path):
            _safe_remove_file(out_path,"thumb_unusable")
        return shrunk
    except Exception as e:
        log.debug("Instagram thumbnail fetch failed | err=%r",e)
        _safe_remove_file(out_path,"fetch_thumb_file")
        return None


async def _download_remote_media(url:str,source:str="",bot=None,chat_id=None,status_msg_id=None,label:str="Downloading Instagram media",thumb_url:str="",duration:int=0,width:int=0,height:int=0)->dict:
    _ensure_tmp_dir()
    session=await get_http_session()
    last_error=None
    thumb_task=asyncio.create_task(_fetch_thumb_file(thumb_url)) if thumb_url else None

    for candidate in _download_candidates(url):
        headers=_download_headers_for_url(candidate,source)

        for attempt in range(3):
            out_path=None

            try:
                async with session.get(candidate,headers=headers,allow_redirects=True,timeout=aiohttp.ClientTimeout(total=180)) as resp:
                    if resp.status in (408,429,500,502,503,504):
                        raise RetryableDownloadError(f"Temporary download failure: HTTP {resp.status}")
                    if resp.status>=400:
                        raise RuntimeError(f"Failed to download media: HTTP {resp.status}")

                    content_type=resp.headers.get("Content-Type","")
                    if not _is_valid_media_content_type(content_type):
                        preview=""
                        try:
                            preview=await resp.text()
                            preview=_truncate_text(preview.replace("\n"," ").strip(),120)
                        except Exception as preview_err:
                            log.warning("Failed to read invalid Instagram media response preview | host=%s err=%s",urlparse(candidate).hostname,preview_err)

                        msg=f"Invalid media response: {content_type or 'unknown content-type'}"
                        if preview:
                            msg+=f" ({preview})"
                        raise RuntimeError(msg)

                    final_url=str(resp.url)
                    media_type=_guess_media_type_from_url(final_url)

                    ctype_lower=content_type.lower()
                    if ctype_lower.startswith("video/"):
                        media_type="video"
                    elif ctype_lower.startswith("image/"):
                        media_type="photo"

                    sniff_done=bool(ctype_lower.startswith(("video/","image/")))
                    peek=bytearray()
                    sig_type,sig_ext="",""

                    ext=_guess_ext(final_url,content_type,media_type)
                    out_path=os.path.join(TMP_DIR,f"{uuid.uuid4().hex}{ext}")

                    total=int(resp.headers.get("Content-Length",0) or 0)
                    if total:
                        check_media_size_limit(total,"Instagram media")
                    track_progress=bool(IG_PROGRESS and bot and chat_id and status_msg_id)
                    if track_progress and total and total<IG_PROGRESS_MIN_BYTES:
                        track_progress=False
                    written=0
                    stats=TransferStats(total)
                    chunk_size=max(64*1024,int(IG_AIOHTTP_CHUNK_SIZE or 256*1024))

                    async with aiofiles.open(out_path,"wb") as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            if not chunk:
                                continue

                            written+=len(chunk)
                            await f.write(chunk)
                            if written>MAX_TG_SIZE:
                                raise FileSizeLimitExceeded("Instagram media exceeds 2GB limit. Download canceled.")

                            if not sniff_done:
                                peek.extend(chunk[:32])
                                sig_type,sig_ext=detect_media_signature(bytes(peek))
                                if sig_type or len(peek)>=12:
                                    sniff_done=True

                            if not track_progress:
                                continue

                            stats.sample(written)
                            await stats.emit(bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,title=label,kind="Instagram download",label=os.path.basename(out_path),log_interval=2.5,edit_interval=IG_PROGRESS_INTERVAL)

                    if written<=0:
                        raise RuntimeError("Downloaded media is empty")
                    stats.log_done("Instagram download",label=os.path.basename(out_path))

                    if sig_type and sig_type!=media_type:
                        log.info(
                            "Instagram media type corrected by magic bytes | sniffed=%s guessed=%s file=%s",
                            sig_type,
                            media_type,
                            os.path.basename(out_path),
                        )
                        media_type=sig_type
                        ext=sig_ext or _guess_ext(final_url,content_type,media_type)
                        new_path=os.path.join(TMP_DIR,f"{uuid.uuid4().hex}{ext}")
                        try:
                            os.replace(out_path,new_path)
                            out_path=new_path
                        except Exception as e:
                            log.warning("Failed to rename Instagram media after sniff | err=%r",e)

                    thumb_path=None
                    if thumb_task is not None:
                        try:
                            thumb_path=await thumb_task
                        except Exception as thumb_err:
                            log.debug("Instagram thumbnail join failed | err=%r",thumb_err)
                        thumb_task=None
                    log.info(
                        "Instagram media saved | source=%s file=%s type=%s size=%s thumb=%s",
                        source or "-",
                        os.path.basename(out_path),
                        media_type,
                        written,
                        bool(thumb_path),
                    )
                    result={"path":out_path,"type":media_type}
                    if thumb_path:
                        result["thumb"]=thumb_path
                    if duration or width or height:
                        result["meta"]={
                            "duration":int(duration or 0),
                            "width":int(width or 0),
                            "height":int(height or 0),
                        }
                    return result

            except Exception as e:
                last_error=e

                if out_path and os.path.exists(out_path):
                    _safe_remove_file(out_path,"download_remote_media_cleanup")

                if attempt<2 and _is_retryable_download_exception(e):
                    log.warning(
                        "Retryable Instagram media download error | source=%s attempt=%s err=%r",
                        source,
                        attempt+1,
                        e,
                    )
                    await asyncio.sleep(1.2*(attempt+1))
                    continue

                break

    if thumb_task is not None:
        thumb_task.cancel()
        try:
            await thumb_task
        except (asyncio.CancelledError,Exception):
            pass
    raise last_error or RuntimeError("Failed to download media")

def _safe_remove_file(path:str,context:str=""):
    if not path or not os.path.exists(path):
        return
    try:
        os.remove(path)
    except Exception as e:
        log.warning("Failed to remove Instagram file | path=%s context=%s err=%s",path,context,e)

def _file_signature(path:str)->str:
    try:
        st=os.stat(path)
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return ""


def _dedupe_downloaded_items(items:list[dict])->list[dict]:
    unique=[]
    seen=set()
    for item in items:
        path=item.get("path")
        if not path or not os.path.exists(path):
            continue
        sig=_file_signature(path)
        if sig and sig in seen:
            _safe_remove_file(path,"dedupe_downloaded_items")
            continue
        if sig:
            seen.add(sig)
        unique.append(item)
    return unique

def _expected_media_types(meta:dict)->list[str]:
    out=[]
    for item in (meta or {}).get("items") or []:
        kind=str(item.get("type") or "").strip().lower()
        if kind in ("photo","video"):
            out.append(kind)
    return out

def _trim_downloaded_items_by_meta(items:list[dict],meta:dict)->list[dict]:
    expected=_expected_media_types(meta)
    if not expected or len(items)<=len(expected):
        return items
    picked=[]
    used=set()
    for kind in expected:
        for idx,item in enumerate(items):
            if idx in used:
                continue
            if item.get("type")==kind:
                picked.append(item)
                used.add(idx)
                break
    if len(picked)!=len(expected):
        log.warning("Instagram metadata trim skipped | expected=%s downloaded=%s picked=%s",expected,len(items),len(picked))
        return items
    for idx,item in enumerate(items):
        if idx not in used:
            path=item.get("path")
            if path:
                _safe_remove_file(path,"trim_downloaded_items_by_meta")
            thumb=item.get("thumb")
            if thumb:
                _safe_remove_file(thumb,"trim_downloaded_thumb_by_meta")
    log.info("Instagram metadata trim applied | expected=%s before=%s after=%s",expected,len(items),len(picked))
    return picked
    
async def _collect_instagram_downloads(url:str,fmt_key:str,bot,chat_id,status_msg_id,meta:dict|None=None)->dict:
    urls = []
    source = "Instagram Scraper"

    if meta and meta.get("items"):
        urls = [item.get("url") for item in meta.get("items") if item.get("url")]

    if not urls:
        try:
            from .mobile_session import fetch_media_via_session
            shortcode=_extract_shortcode(url)
            mob=await fetch_media_via_session(shortcode)
            if mob and mob.get("items"):
                urls=[it.get("url") for it in mob["items"] if it.get("url")]
                source="Instagram Scraper"
                if meta is not None:
                    meta["caption"]=meta.get("caption") or mob.get("caption") or ""
                    meta["username"]=meta.get("username") or mob.get("username") or ""
                    meta["nickname"]=meta.get("nickname") or mob.get("nickname") or ""
                    meta["items"]=mob.get("items") or []
        except Exception as e:
            log.debug("Instagram mobile session fallback check failed | err=%r", e)

    if not urls:
        log.info("Instagram GraphQL found no media, trying Neoxr API...")
        neoxr_res = await _fetch_neoxr_api(url)
        if neoxr_res.get("status") and neoxr_res.get("urls"):
            urls = neoxr_res.get("urls")
            source = "Neoxr API"
            
    urls = _uniq_media_urls(urls)
    urls = _filter_urls_for_media(urls, fmt_key, (meta or {}).get("items") or [])
    
    if not urls:
        if fmt_key == "mp3":
            raise RuntimeError("Instagram post does not contain video/audio")
        raise RuntimeError("No downloadable media found from Instagram Scraper or Neoxr API")
        
    downloaded = []
    failed_count = 0
    last_error = None
    total_items = len(urls)
    media_by_url={}
    for item in (meta or {}).get("items") or []:
        u=item.get("url")
        if u:
            media_by_url[u]=item

    def _media_kwargs(media_url:str)->dict:
        info=media_by_url.get(media_url) or {}
        return {
            "thumb_url":str(info.get("thumbnail") or ""),
            "duration":int(info.get("duration") or 0),
            "width":int(info.get("width") or 0),
            "height":int(info.get("height") or 0),
        }

    if total_items <= 1:
        for idx, media_url in enumerate(urls, start=1):
            try:
                downloaded.append(await _download_remote_media(
                    media_url,
                    source=source,
                    bot=bot,
                    chat_id=chat_id,
                    status_msg_id=status_msg_id,
                    label="Downloading Instagram media",
                    **_media_kwargs(media_url),
                ))
            except Exception as e:
                failed_count += 1
                last_error = e
                log.warning("Instagram media download failed | source=%s host=%s err=%r", source, urlparse(media_url).hostname, e)
    else:
        results: list = [None] * total_items
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(max(1, IG_CAROUSEL_CONCURRENCY))

        async def _one(i: int, media_url: str):
            nonlocal failed_count, last_error
            async with sem:
                try:
                    results[i] = await _download_remote_media(
                        media_url,
                        source=source,
                        bot=bot,
                        chat_id=chat_id,
                        status_msg_id=status_msg_id,
                        label=f"Downloading Instagram media ({i + 1}/{total_items})",
                        **_media_kwargs(media_url),
                    )
                except Exception as e:
                    async with lock:
                        failed_count += 1
                        last_error = e
                    log.warning("Instagram media download failed | source=%s host=%s err=%r", source, urlparse(media_url).hostname, e)

        await asyncio.gather(*(_one(i, u) for i, u in enumerate(urls)))
        downloaded = [r for r in results if r]

    if not downloaded:
        raise last_error or RuntimeError("All media downloads failed")

    downloaded = _dedupe_downloaded_items(downloaded)
    downloaded = _trim_downloaded_items_by_meta(downloaded, meta or {})

    if not downloaded:
        raise RuntimeError("All media downloads were duplicates or invalid")

    return {"items": downloaded, "source": source, "failed_count": failed_count}


async def instagram_api_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,metadata_ready:bool=False):
    meta={"caption":"","username":"","nickname":"","items":[]}
    cache_key=_extract_shortcode(raw_url)
    cached=_meta_cache_get(cache_key)
    _meta_cache_prune()

    if cached:
        meta=cached
        log.info(
            "Instagram metadata cache hit | url=%s shortcode=%s items=%s ttl=%.0fs",
            raw_url,cache_key,len(meta.get("items") or []),IG_META_CACHE_TTL,
        )
    else:
        if not metadata_ready:
            await _safe_edit_status(
                bot,
                chat_id,
                status_msg_id,
                "<b>Scraping Instagram metadata...</b>",
                min_interval=0,
            )

        try:
            meta=await _fetch_instagram_metadata(raw_url)
            log.info(
                "Instagram primary metadata success | url=%s caption_len=%s username=%r nickname=%r items=%s",
                raw_url,
                len(meta.get("caption") or ""),
                meta.get("username"),
                meta.get("nickname"),
                len(meta.get("items") or []),
            )
        except Exception as e:
            log.warning("Primary Instagram metadata extractor failed | url=%s err=%r",raw_url,e)
            try:
                fallback_meta=await _fetch_instagram_caption_meta(raw_url)
                if fallback_meta.get("caption") or fallback_meta.get("username") or fallback_meta.get("nickname"):
                    meta=fallback_meta
                    log.info(
                        "Instagram fallback metadata success | url=%s caption_len=%s username=%r nickname=%r",
                        raw_url,
                        len(meta.get("caption") or ""),
                        meta.get("username"),
                        meta.get("nickname"),
                    )
                else:
                    log.warning("Instagram fallback metadata empty | url=%s",raw_url)
            except Exception as fe:
                log.warning("Instagram fallback metadata extractor failed | url=%s err=%r",raw_url,fe)

        if meta.get("items"):
            _meta_cache_put(cache_key,meta)

    log.info(
        "Instagram metadata result | url=%s caption=%r username=%r nickname=%r items=%s cached=%s",
        raw_url,
        meta.get("caption"),
        meta.get("username"),
        meta.get("nickname"),
        len(meta.get("items") or []),
        bool(cached),
    )
    collected=await _collect_instagram_downloads(raw_url,fmt_key,bot,chat_id,status_msg_id,meta=meta)
    items=collected["items"]
    source=collected["source"]
    failed_count=collected.get("failed_count",0)
    if failed_count:
        log.warning("Instagram scraper partial success | url=%s downloaded=%s failed=%s source=%s",raw_url,len(items),failed_count,source)
    if status_msg_id:
        _LAST_IG_STATUS_TEXT.pop((int(chat_id),int(status_msg_id)),None)
    if len(items)==1:
        item=items[0]
        media_type=item.get("type") or "photo"
        title=_build_title(meta,media_type)
        log.info("Instagram scraper success | url=%s file=%s source=%s title=%r",raw_url,item.get("path"),source,title)
        result={"path":item["path"],"title":title,"source":source}
        if item.get("thumb"):
            result["thumb"]=item["thumb"]
        if item.get("meta"):
            result["meta"]=item["meta"]
        if media_type=="video":
            result["remux_done"]=True
        return result

    first_type=((items[0] or {}).get("type") or "photo")
    title=_build_title(meta,first_type,len(items))
    log.info("Instagram scraper success | url=%s items=%s source=%s title=%r",raw_url,len(items),source,title)
    if all((i.get("type") or "")=="video" for i in items):
        for i in items:
            i["remux_done"]=True
    return {"items":items,"title":title,"source":source}

async def cleanup_instagram_result(result:dict):
    if not isinstance(result,dict):
        return
    path=result.get("path")
    if path and os.path.exists(path):
        _safe_remove_file(path,"cleanup_instagram_result_single")
    thumb=result.get("thumb")
    if thumb and os.path.exists(thumb):
        _safe_remove_file(thumb,"cleanup_instagram_result_thumb")
    for item in result.get("items") or []:
        p=item.get("path")
        if p and os.path.exists(p):
            _safe_remove_file(p,"cleanup_instagram_result_items")
        t=item.get("thumb")
        if t and os.path.exists(t):
            _safe_remove_file(t,"cleanup_instagram_result_item_thumb")