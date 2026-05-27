import os
import re
import json
import html
import uuid
import hashlib
import time
import base64
import random
import string
import logging
import asyncio
import aiohttp
import aiofiles
from urllib.parse import urlparse,parse_qs,unquote
from telegram.error import RetryAfter
from utils.http import get_http_session
from handlers.dl.constants import TMP_DIR
from curl_cffi.requests import AsyncSession
from handlers.dl.utils import progress_bar

log=logging.getLogger(__name__)
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
_LAST_IG_STATUS_TEXT={}

IG_PROGRESS=os.getenv("INSTAGRAM_PROGRESS","1").lower() in ("1","true","on","yes")
IG_PROGRESS_INTERVAL=float(os.getenv("INSTAGRAM_PROGRESS_INTERVAL","1.5"))
IG_AIOHTTP_CHUNK_SIZE=int(os.getenv("INSTAGRAM_AIOHTTP_CHUNK_SIZE",str(256*1024)))

GRAPHQL_ENDPOINT="https://www.instagram.com/graphql/query/"
POLARIS_ACTION="PolarisPostActionLoadPostQueryQuery"
GRAPHQL_DOC_ID="8845758582119845"
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

def _parse_gql_media(media:dict)->dict:
    if not isinstance(media,dict):
        return {"caption":"","username":"","nickname":"","items":[]}
    typename=str(media.get("__typename") or media.get("typename") or "").strip()
    owner=media.get("owner") or {}
    username=(owner.get("username") or "").strip()
    nickname=(owner.get("full_name") or owner.get("fullName") or "").strip()
    caption=_caption_from_media(media)
    items=[]
    def add_item(kind:str,url:str,thumb:str="",dims:dict|None=None):
        url=str(url or "").strip()
        if not url:
            return
        dims=dims or {}
        items.append({"type":kind,"url":url,"thumbnail":str(thumb or "").strip(),"width":int(dims.get("width") or 0),"height":int(dims.get("height") or 0)})
    if typename in ("GraphVideo","XDTGraphVideo"):
        add_item("video",media.get("video_url"),media.get("display_url"),media.get("dimensions"))
    elif typename in ("GraphImage","XDTGraphImage"):
        add_item("photo",media.get("display_url"),"",media.get("dimensions"))
    elif typename in ("GraphSidecar","XDTGraphSidecar"):
        for edge in (((media.get("edge_sidecar_to_children") or {}).get("edges")) or []):
            node=(edge or {}).get("node") or {}
            node_type=str(node.get("__typename") or node.get("typename") or "").strip()
            if node_type in ("GraphVideo","XDTGraphVideo"):
                add_item("video",node.get("video_url"),node.get("display_url"),node.get("dimensions"))
            elif node_type in ("GraphImage","XDTGraphImage"):
                add_item("photo",node.get("display_url"),"",node.get("dimensions"))
    return {"caption":caption,"username":username,"nickname":nickname,"items":items}

def _rand_alpha(n:int)->str:
    return "".join(random.choice(string.ascii_letters) for _ in range(n))

def _rand_b64(n_bytes:int)->str:
    return base64.urlsafe_b64encode(os.urandom(n_bytes)).decode().rstrip("=")

def _build_gql_request():
    rollout_hash="1019933358"
    session_data=_rand_b64(8)
    csrf_token=_rand_b64(24)
    device_id=_rand_b64(18)
    machine_id=_rand_b64(18)
    headers={
        "x-ig-app-id":"936619743392459",
        "x-fb-lsd":session_data,
        "x-csrftoken":csrf_token,
        "x-bloks-version-id":"6309c8d03d8a3f47a1658ba38b304a3f837142ef5f637ebf1f8f52d4b802951e",
        "x-asbd-id":"129477",
        "x-fb-friendly-name":POLARIS_ACTION,
        "content-type":"application/x-www-form-urlencoded",
        "cookie":"; ".join([f"csrftoken={csrf_token}",f"ig_did={device_id}","wd=1280x720","dpr=2",f"mid={machine_id}","ig_nrcb=1"]),
    }
    body={
        "__d":"www","__a":"1","__s":"::"+_rand_alpha(6),"__hs":"20126.HYP:instagram_web_pkg.2.1...0","__req":"b",
        "__ccg":"EXCELLENT","__rev":rollout_hash,"__hsi":"7436540909012459023","__dyn":_rand_b64(90),"__csr":_rand_b64(90),
        "__user":"0","__comet_req":"7","libav":"0","dpr":"2","lsd":session_data,"jazoest":str(random.randint(1000,99999)),
        "__spin_r":rollout_hash,"__spin_b":"trunk","__spin_t":str(int(time.time())),
        "fb_api_caller_class":"RelayModern","fb_api_req_friendly_name":POLARIS_ACTION,"server_timestamps":"true","doc_id":GRAPHQL_DOC_ID,
    }
    return headers,body

async def _fetch_gql_metadata(raw_url:str)->dict:
    shortcode=_extract_shortcode(raw_url)
    if not shortcode:
        raise RuntimeError("Instagram shortcode not found")
    gql_headers,gql_body=_build_gql_request()
    headers={**WEB_HEADERS,**gql_headers}
    body=dict(gql_body)
    body["variables"]=json.dumps({"shortcode":shortcode,"fetch_tagged_user_count":None,"hoisted_comment_id":None,"hoisted_reply_id":None},separators=(",",":"))
    session=await get_http_session()
    async with session.post(GRAPHQL_ENDPOINT,data=body,headers=headers,timeout=aiohttp.ClientTimeout(total=25),allow_redirects=True) as resp:
        text=await resp.text()
        log.info("Instagram GraphQL metadata fetch | status=%s len=%s",resp.status,len(text or ""))
        if resp.status>=400:
            raise RuntimeError(f"Instagram GraphQL HTTP {resp.status}")
        try:
            data=json.loads(text)
        except Exception as e:
            raise RuntimeError(f"Instagram GraphQL invalid JSON: {e}") from e
    if not isinstance(data,dict):
        raise RuntimeError("Instagram GraphQL invalid response")
    if str(data.get("status") or "").lower() not in ("ok",""):
        raise RuntimeError(f"Instagram GraphQL bad status: {data.get('status')}")
    media=(data.get("data") or {}).get("xdt_shortcode_media") or (data.get("data") or {}).get("shortcode_media") or _json_find_media(data)
    if not isinstance(media,dict):
        keys=list((data.get("data") or {}).keys()) if isinstance(data.get("data"),dict) else []
        raise RuntimeError(f"Instagram GraphQL shortcode_media not found | data_keys={keys}")
    parsed=_parse_gql_media(media)
    if parsed.get("caption") or parsed.get("username") or parsed.get("nickname") or parsed.get("items"):
        return parsed
    raise RuntimeError("Instagram GraphQL metadata empty")

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
    
async def _fetch_instagram_metadata(raw_url:str)->dict:
    errors=[]
    for func in (_fetch_gql_metadata,_fetch_embed_metadata):
        try:
            meta=await func(raw_url)
            if meta.get("caption") or meta.get("username") or meta.get("nickname") or meta.get("items"):
                return meta
        except Exception as e:
            errors.append(f"{func.__name__}: {e}")
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
    
def _safe_title(media_type:str,count:int)->str:
    if count>1:
        return "Instagram Media"
    return "Instagram Video" if media_type=="video" else "Instagram Post"

def _uniq_media_urls(items:list[str])->list[str]:
    out=[]
    seen=set()
    strip_query_hosts=("cdninstagram.com","fbcdn.net","d.rapidcdn.app")
    for item in items:
        raw=(item or "").strip()
        if not raw:
            continue
        try:
            parsed=urlparse(raw)
            host=(parsed.hostname or "").lower()
            path=parsed.path or ""
            if any(host==h or host.endswith("."+h) for h in strip_query_hosts):
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

def _decode_indown_fetch(link:str)->str:
    try:
        parsed=urlparse(link)
        host=(parsed.hostname or "").lower()
        if "indown.io" not in host:
            return link
        qs=parse_qs(parsed.query)
        raw=(qs.get("url") or qs.get("rl") or [""])[0]
        raw=unquote(raw or "").strip()
        return raw or link
    except Exception as e:
        log.warning("Failed to decode Indown fetch URL | url=%s err=%s",link,e)
        return link

def _is_indown_real_download(link:str)->bool:
    try:
        parsed=urlparse(link)
        host=(parsed.hostname or "").lower()
        if "indown.io" not in host:
            return True
        qs=parse_qs(parsed.query)
        is_download=(qs.get("is_download") or [""])[0]
        if is_download=="0":
            return False
        raw=(qs.get("url") or qs.get("rl") or [""])[0]
        raw=unquote(raw or "")
        if not raw:
            return False
        return bool(re.search(r"\.(mp4|mov|m4v|webm|jpg|jpeg|png|webp)(?:\?|$)",raw,flags=re.I))
    except Exception as e:
        log.warning("Failed to validate Indown download URL | url=%s err=%s",link,e)
        return False

def _normalize_media_link(link:str)->str:
    link=(link or "").strip().replace("&amp;","&")
    if not link:
        return ""
    if "indown.io/fetch" in link and not _is_indown_real_download(link):
        return ""
    if "indown.io/fetch" in link:
        link=_decode_indown_fetch(link)
    return link.strip()

def _media_url_allowed(link:str)->bool:
    return bool(re.search(r"(cdninstagram\.com|fbcdn\.net|d\.rapidcdn\.app|indown\.io/fetch)",link or "",flags=re.I))

def _collect_urls_from_html(text:str)->list[str]:
    found=[]
    for match in re.findall(r'''href=["']([^"']+)["']''',text or "",flags=re.I):
        link=_normalize_media_link(match)
        if link and _media_url_allowed(link):
            found.append(link)
    for match in re.findall(r'''https://d\.rapidcdn\.app/v2\?[^"'<> ]+''',text or "",flags=re.I):
        link=_normalize_media_link(match)
        if link and _media_url_allowed(link):
            found.append(link)
    for match in re.findall(r'''https://[^"'\s<>]+''',text or "",flags=re.I):
        if "indown.io/fetch" not in match:
            continue
        link=_normalize_media_link(match)
        if link and _media_url_allowed(link):
            found.append(link)
    return [re.sub(r"&dl=1$","",link) for link in _uniq_media_urls(found)]

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

async def _indown(url:str)->dict:
    async with AsyncSession(impersonate="chrome120") as session:
        headers={"User-Agent":USER_AGENT,"Accept":"text/html,application/xhtml+xml"}
        try:
            resp = await session.get("https://indown.io/en1", headers=headers, timeout=25)
            page_data = resp.text
        except Exception as e:
            return {"status":False, "message":f"Indown get token failed: {e}"}

        token_match=re.search(r'''name=["']_token["'][^>]*value=["']([^"']+)["']''',page_data,flags=re.I)
        token=token_match.group(1).strip() if token_match else ""
        if not token:
            return {"status":False,"message":"Token Indown not found (Blocked by Cloudflare)"}
            
        form={"referer":"https://indown.io/en1","locale":"en","_token":token,"link":url,"p":"i"}
        try:
            resp_post = await session.post(
                "https://indown.io/download",
                data=form,
                headers={
                    "Content-Type":"application/x-www-form-urlencoded",
                    "User-Agent":USER_AGENT,
                    "Referer":"https://indown.io/en1",
                    "Origin":"https://indown.io",
                },
                timeout=30,
            )
            result_data = resp_post.text
        except Exception as e:
            return {"status":False, "message":f"Indown POST failed: {e}"}

    urls=_collect_urls_from_html(result_data)
    if not urls:
        return {"status":False,"message":"No media found in Indown HTML"}
    return {"status":True,"source":"Indown","urls":urls}

async def _snapsave(url:str)->dict:
    async with AsyncSession(impersonate="chrome120") as session:
        try:
            resp = await session.post(
                "https://snapsave.app/id/action.php?lang=id",
                data={"url":url},
                headers={
                    "Origin":"https://snapsave.app",
                    "Referer":"https://snapsave.app/id/download-video-instagram",
                    "User-Agent":USER_AGENT,
                    "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
                },
                timeout=30,
            )
            data = resp.text
        except Exception as e:
            return {"status":False, "message":f"Snapsave POST failed: {e}"}
            
    urls=_collect_urls_from_html(data)
    if not urls:
        rapid=re.findall(r'''https://d\.rapidcdn\.app/v2\?[^"'<> ]+''',data,flags=re.I)
        urls=_uniq_media_urls([x.replace("&amp;","&") for x in rapid])
    if not urls:
        return {"status":False,"message":"No media found in Snapsave HTML"}
    return {"status":True,"source":"Snapsave","urls":urls}


async def igdl_scrape(url:str)->dict:
    indown = await _indown(url)
    if indown.get("status") and indown.get("urls"):
        log.info("Instagram scraper source selected | source=Indown urls=%s",len(indown.get("urls") or []))
        return indown
    log.warning("Instagram Indown failed, trying Snapsave | err=%s",indown.get("message"))
    
    snapsave = await _snapsave(url)
    if snapsave.get("status") and snapsave.get("urls"):
        log.info("Instagram scraper source selected | source=Snapsave urls=%s",len(snapsave.get("urls") or []))
        return snapsave
        
    raise RuntimeError(snapsave.get("message") or indown.get("message") or cobalt.get("message") or "No media found")


def _guess_media_type_from_url(url:str)->str:
    parsed=urlparse(url or "")
    host=(parsed.hostname or "").lower()
    path=(parsed.path or "").lower()
    if path.endswith((".mp4",".mov",".m4v",".webm")) or "rapidcdn.app" in host:
        return "video"
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
    source_lower=(source or "").lower()
    headers={
        "User-Agent":USER_AGENT,
        "Accept":"*/*",
        "Accept-Language":"en-US,en;q=0.9",
        "Accept-Encoding":"identity",
        "Connection":"keep-alive",
    }
    if "rapidcdn.app" in lower_url or source_lower=="snapsave":
        headers["Referer"]="https://snapsave.app/"
    elif "cdninstagram.com" in lower_url or "fbcdn.net" in lower_url or source_lower=="indown":
        headers["Referer"]="https://www.instagram.com/"
    elif "indown.io" in lower_url:
        headers["Referer"]="https://indown.io/en1"
    return headers

def _download_candidates(url:str)->list[str]:
    raw=(url or "").strip()
    if not raw:
        return []
    decoded=_decode_indown_fetch(raw) if "indown.io/fetch" in raw else raw
    out=[]
    for item in (decoded,raw):
        if item and item not in out:
            out.append(item)
    return out

async def _safe_edit_status(bot,chat_id,message_id,text:str,min_interval:float=1.2):
    if not bot or not chat_id or not message_id:
        return
    cache=getattr(bot,"_ig_status_edit_cache",{})
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
            setattr(bot,"_ig_status_edit_cache",cache)
            return
        except RetryAfter as e:
            wait=max(int(getattr(e,"retry_after",1)),1)
            log.warning("Instagram status RetryAfter | chat_id=%s wait=%s",chat_id,wait)
            await asyncio.sleep(wait+1)
        except Exception as e:
            if "message is not modified" in str(e).lower():
                cache[key]={"text":text,"ts":time.monotonic()}
                setattr(bot,"_ig_status_edit_cache",cache)
                return
            log.warning("Failed to edit Instagram status | chat_id=%s message_id=%s err=%r",chat_id,message_id,e)
            return

async def _safe_edit_progress(bot,chat_id,status_msg_id,title:str,downloaded:int,total:int=0,speed_bps:float=0.0,eta_seconds:float|None=None):
    if not IG_PROGRESS or not bot or not chat_id or not status_msg_id:
        return

    cache=getattr(bot,"_ig_status_edit_cache",{})
    key=(int(chat_id),int(status_msg_id))
    now=time.monotonic()
    prev=cache.get(key) or {}

    if now - prev.get("ts", 0) < 2.0:
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

    text = "\n".join(lines)
    if prev.get("text") == text:
        return

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        cache[key]={"text":text,"ts":time.monotonic()}
        setattr(bot,"_ig_status_edit_cache",cache)
    except RetryAfter as e:
        wait=max(int(getattr(e,"retry_after",1)),1)
        log.warning("Instagram progress RetryAfter | chat_id=%s wait=%s",chat_id,wait)
        await asyncio.sleep(wait+1)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            log.debug("Instagram progress edit failed | chat_id=%s message_id=%s err=%r",chat_id,status_msg_id,e)

                        
async def _download_remote_media(url:str,source:str="",bot=None,chat_id=None,status_msg_id=None,label:str="Downloading Instagram media")->dict:
    _ensure_tmp_dir()
    session=await get_http_session()
    last_error=None

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

                    if content_type.lower().startswith("video/"):
                        media_type="video"
                    elif content_type.lower().startswith("image/"):
                        media_type="photo"

                    ext=_guess_ext(final_url,content_type,media_type)
                    out_path=os.path.join(TMP_DIR,f"{uuid.uuid4().hex}{ext}")

                    total=int(resp.headers.get("Content-Length",0) or 0)
                    written=0
                    last_edit=-10.0
                    last_sample_size=0
                    last_sample_ts=time.time()
                    chunk_size=max(64*1024,int(IG_AIOHTTP_CHUNK_SIZE or 256*1024))

                    async with aiofiles.open(out_path,"wb") as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            if not chunk:
                                continue

                            written+=len(chunk)
                            await f.write(chunk)

                            if not IG_PROGRESS or not bot or not chat_id or not status_msg_id:
                                continue

                            now=time.time()
                            elapsed=max(now-last_sample_ts,0.001)
                            speed_bps=max(written-last_sample_size,0)/elapsed
                            eta_seconds=((total-written)/speed_bps) if total>0 and speed_bps>0 and written<=total else None

                            if now-last_edit<IG_PROGRESS_INTERVAL and last_edit>=0:
                                continue

                            await _safe_edit_progress(
                                bot,
                                chat_id,
                                status_msg_id,
                                label,
                                written,
                                total,
                                speed_bps,
                                eta_seconds,
                            )
                            last_edit,last_sample_size,last_sample_ts=now,written,now

                    if written<=0:
                        raise RuntimeError("Downloaded media is empty")

                    log.info(
                        "Instagram media saved | source=%s file=%s type=%s size=%s",
                        source or "-",
                        os.path.basename(out_path),
                        media_type,
                        written,
                    )
                    return {"path":out_path,"type":media_type}

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

    raise last_error or RuntimeError("Failed to download media")

def _safe_remove_file(path:str,context:str=""):
    if not path or not os.path.exists(path):
        return
    try:
        os.remove(path)
    except Exception as e:
        log.warning("Failed to remove Instagram file | path=%s context=%s err=%s",path,context,e)

def _file_sha1_sync(path:str)->str:
    h=hashlib.sha1()
    with open(path,"rb") as f:
        while True:
            chunk=f.read(1024*1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

async def _dedupe_downloaded_items(items:list[dict])->list[dict]:
    unique=[]
    seen=set()
    for item in items:
        path=item.get("path")
        if not path or not os.path.exists(path):
            continue
        try:
            sig=await asyncio.to_thread(_file_sha1_sync,path)
        except Exception as e:
            log.warning("Failed to hash downloaded Instagram media | path=%s err=%s",path,e)
            unique.append(item)
            continue
        if sig in seen:
            _safe_remove_file(path,"dedupe_downloaded_items")
            continue
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
    log.info("Instagram metadata trim applied | expected=%s before=%s after=%s",expected,len(items),len(picked))
    return picked
    
async def _collect_instagram_downloads(url:str,fmt_key:str,bot,chat_id,status_msg_id,meta:dict|None=None)->dict:
    urls = []
    source = "Instagram Scraper"
    
    try:
        scraped = await igdl_scrape(url)
        urls = scraped.get("urls") or []
        source = scraped.get("source") or "Instagram Scraper"
    except Exception as e:
        log.warning("Step 1 Failed: Internal scraper broken | err=%r", e)
        
    if not urls:
        log.info("Step 2: Scraper internal gagal, mencoba Neoxr API...")
        neoxr_res = await _fetch_neoxr_api(url)
        if neoxr_res.get("status") and neoxr_res.get("urls"):
            urls = neoxr_res.get("urls")
            source = "Neoxr API"
            
    urls = _uniq_media_urls(urls)
    urls = _filter_urls_for_media(urls, fmt_key, (meta or {}).get("items") or [])
    
    if not urls:
        if fmt_key == "mp3":
            raise RuntimeError("Instagram post does not contain video/audio")
        raise RuntimeError("No downloadable media found from Scraper or Neoxr API")
        
    downloaded = []
    failed_count = 0
    last_error = None
    total_items = len(urls)
    
    for idx, media_url in enumerate(urls, start=1):
        try:
            label = f"Downloading Instagram media ({idx}/{total_items})" if total_items > 1 else "Downloading Instagram media"
            downloaded.append(await _download_remote_media(
                media_url, 
                source=source, 
                bot=bot, 
                chat_id=chat_id, 
                status_msg_id=status_msg_id, 
                label=label
            ))
        except Exception as e:
            failed_count += 1
            last_error = e
            log.warning("Instagram media download failed | source=%s host=%s err=%r", source, urlparse(media_url).hostname, e)
            
    if not downloaded:
        raise last_error or RuntimeError("All media downloads failed")
        
    downloaded = await _dedupe_downloaded_items(downloaded)
    downloaded = _trim_downloaded_items_by_meta(downloaded, meta or {})
    
    if not downloaded:
        raise RuntimeError("All media downloads were duplicates or invalid")
        
    return {"items": downloaded, "source": source, "failed_count": failed_count}

async def instagram_api_download(raw_url:str,fmt_key:str,bot,chat_id,status_msg_id,metadata_ready:bool=False):
    meta={"caption":"","username":"","nickname":"","items":[]}

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

    log.info(
        "Instagram metadata result | url=%s caption=%r username=%r nickname=%r items=%s",
        raw_url,
        meta.get("caption"),
        meta.get("username"),
        meta.get("nickname"),
        len(meta.get("items") or []),
    )

    collected=await _collect_instagram_downloads(raw_url,fmt_key,bot,chat_id,status_msg_id,meta=meta)
    items=collected["items"]
    source=collected["source"]
    failed_count=collected.get("failed_count",0)

    if failed_count:
        log.warning("Instagram scraper partial success | url=%s downloaded=%s failed=%s source=%s",raw_url,len(items),failed_count,source)

    _LAST_IG_STATUS_TEXT.pop((int(chat_id),int(status_msg_id)),None)

    if len(items)==1:
        item=items[0]
        media_type=item.get("type") or "photo"
        title=_build_title(meta,media_type)
        log.info("Instagram scraper success | url=%s file=%s source=%s title=%r",raw_url,item.get("path"),source,title)
        return {"path":item["path"],"title":title,"source":source}

    first_type=((items[0] or {}).get("type") or "photo")
    title=_build_title(meta,first_type,len(items))
    log.info("Instagram scraper success | url=%s items=%s source=%s title=%r",raw_url,len(items),source,title)
    return {"items":items,"title":title,"source":source}

async def cleanup_instagram_result(result:dict):
    if not isinstance(result,dict):
        return
    path=result.get("path")
    if path and os.path.exists(path):
        _safe_remove_file(path,"cleanup_instagram_result_single")
    for item in result.get("items") or []:
        p=item.get("path")
        if p and os.path.exists(p):
            _safe_remove_file(p,"cleanup_instagram_result_items")