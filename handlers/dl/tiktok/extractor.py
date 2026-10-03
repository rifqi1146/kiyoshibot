"""Extractor metadata TikTok (tiktok.com) — parsing HTML + fetch media URL.

FLOW EXTRACTOR
--------------
1. `is_tiktok(url)` / `_is_short_tiktok_url(url)` -> deteksi host TikTok/Douyin
   dan URL pendek (vm./vt.).
2. `_extract_aweme_id(url)` -> id video; `_resolve_tiktok_url` -> follow redirect
   URL pendek + merge cookie respons.
3. `_extract_item_struct` -> coba parser berurutan: __UNIVERSAL_DATA__,
   SIGI_STATE, __NEXT_DATA__; kalau gagal cek halaman aneh (captcha/login/SSR).
4. `_parse_direct_media(item)` -> dict {kind: video|album, video_urls, images,
   music_urls, duration, width, height}. Urutan URL via `_prioritize_video_urls`.
5. `_fetch_tiktok_govd_fast` -> fetch ringan 1 request (jalur cepat).
   `_fetch_tiktok_direct` -> retry 5x aiohttp + fallback Scrapling.
   `_fetch_tiktok_metadata` -> fast dulu, kalau gagal full-scraping.
6. PoW challenge TikTok diselesaikan oleh `utils._solve_tt_challenge`.
"""
import re
import json
import time
import asyncio
import aiohttp
import logging
from utils.http import get_http_session

from .constants import (
    WEB_HEADERS,
    UNIVERSAL_RE,
    SIGI_RE,
    NEXT_RE,
    SHORT_TIKTOK_RE,
    USE_GOVD_FAST,
    USE_SCRAPLING,
)
from .utils import (
    _ttdbg,
    _dump_tiktok_debug,
    _safe_edit_status,
    _build_tiktok_headers,
    _merge_cookie_headers,
    _cookie_header,
    _cookies_from_header,
    _solve_tt_challenge,
    _detect_weird_tiktok_page,
    _int_meta,
    _duration_meta,
    _prioritize_video_urls,
)

log = logging.getLogger(__name__)

def is_tiktok(url:str)->bool:
    return any(x in (url or "") for x in ("tiktok.com","vt.tiktok.com","vm.tiktok.com","v.douyin.com","douyin.com"))

def _is_short_tiktok_url(url:str)->bool:
    return bool(SHORT_TIKTOK_RE.search(url or ""))

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

def _looks_like_http_url(value:str)->bool:
    return str(value or "").strip().lower().startswith(("http://","https://"))

def _collect_url_list(value)->list[str]:
    out=[]
    if isinstance(value,str) and value.strip():
        val=value.strip()
        if _looks_like_http_url(val):
            out.append(val)
    elif isinstance(value,list):
        for x in value:
            if isinstance(x,str) and x.strip():
                val=x.strip()
                if _looks_like_http_url(val):
                    out.append(val)
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
        video_urls=_prioritize_video_urls(video_urls)
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
    if item.get("isAd") or item.get("is_ad"):
        raise RuntimeError("TikTok ad/sponsored video: direct media URL omitted by TikTok (triggering fallback)")
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
