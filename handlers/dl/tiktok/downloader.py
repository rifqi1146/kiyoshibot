"""Downloader TikTok — engine transfer + render media.

FLOW DOWNLOADER
---------------
1. Engine: `_download_with_best_engine` pilih aria2c atau aiohttp sesuai
   TIKTOK_DOWNLOAD_ENGINE; masing-masing punya fallback silang.
2. `aria2c_download` / `aiohttp_download` -> stream ke TMP_DIR, ukur pakai
   `TransferStats`, cek batas MAX_TG_SIZE (2GB), progress via edit_status.
3. Video: `_download_direct_video` -> coba semua kandidat video_urls berurutan.
   Album: `_download_album_images` -> paralel 8 koneksi.
   Slideshow: `_download_slideshow_audio` -> render `_render_slideshow_video`
   (ffmpeg xfade) -> mp4.
4. `_download_tiktok_media` -> dispatcher per fmt_key (mp4/mp3/slideshow_video)
   dan kind (video/album); album >1 gambar + fmt mp4 -> minta picker
   (`choice_required`) supaya user pilih Images vs Slideshow video.
"""
import os
import re
import time
import uuid
import shutil
import asyncio
import aiohttp
import aiofiles
import logging
import subprocess
from utils.http import get_http_session

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, is_invalid_video, check_media_size_limit, is_media_content_type, FileSizeLimitExceeded
from handlers.dl.progress import TransferStats

from .constants import (
    USER_AGENT,
    TIKTOK_PROGRESS,
    TIKTOK_PROGRESS_INTERVAL,
    TIKTOK_DOWNLOAD_ENGINE,
    TIKTOK_AIOHTTP_CHUNK_SIZE,
    TIKTOK_ALBUM_CHUNK_SIZE,
    ARIA2C_TIMEOUT,
    AIOHTTP_DOWNLOAD_TIMEOUT,
    TIKTOK_SLIDESHOW_IMAGE_DURATION,
    TIKTOK_SLIDESHOW_LOOP_IMAGES,
    TIKTOK_SLIDESHOW_WIDTH,
    TIKTOK_SLIDESHOW_HEIGHT,
    TIKTOK_SLIDESHOW_FPS,
    TIKTOK_SLIDESHOW_TRANSITION,
    TIKTOK_SLIDESHOW_TRANSITION_DURATION,
    TIKTOK_SLIDESHOW_MIN_IMAGE_DURATION,
    TIKTOK_SLIDESHOW_SYNC_AUDIO,
)
from .utils import (
    _ttdbg,
    _safe_remove_file,
    _kill_process,
    _safe_edit_status,
    _cookie_header,
    _purge_tiktok_session_cookies,
    _prioritize_video_urls,
)

log = logging.getLogger(__name__)

async def _probe_total_bytes(session,url:str,headers:dict|None=None)->int:
    try:
        async with session.head(url,headers=headers,timeout=aiohttp.ClientTimeout(total=20),allow_redirects=True) as resp:
            # JANGAN percaya Content-Length dari response non-media: CDN TikTok
            # `aweme/v1/play/` membalas HEAD dengan HTTP 200
            # `Content-Type: application/json` + `Content-Length: 108` (bukan
            # video). Tanpa guard ini total jadi 108 B dan UI memaparkan
            # `1.3 MB/108 B` dengan persentase jutaan persen. Video aslinya
            # di-stream lewat GET/Range dan berukuran megabyte.
            if is_media_content_type(resp.headers.get("Content-Type")):
                total=int(resp.headers.get("Content-Length",0) or 0)
                if total>0:
                    return total
            else:
                log.debug("TikTok HEAD size probe rejected non-media content-type | type=%s url=%s",resp.headers.get("Content-Type"),url[:160])
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
            if resp.headers.get("Content-Length") and is_media_content_type(resp.headers.get("Content-Type")):
                return int(resp.headers.get("Content-Length",0) or 0)
    except Exception as e:
        log.debug("TikTok Range size probe failed | err=%r",e)
    return 0

async def aria2c_download(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    aria2=shutil.which("aria2c")
    if not aria2:
        raise RuntimeError("aria2c not found in PATH")
    total=await _probe_total_bytes(session,media_url,headers=headers) if (TIKTOK_PROGRESS and status_msg_id) else 0
    if total:
        check_media_size_limit(total, "TikTok media")
    out_dir=os.path.dirname(out_path) or "."
    out_name=os.path.basename(out_path)

    cmd=[
        aria2,"--dir",out_dir,"--out",out_name,"--file-allocation=none","--allow-overwrite=true",
        "--auto-file-renaming=false","--continue=true",
        "--max-connection-per-server=4","--split=4","--min-split-size=1M",
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
    stats=TransferStats(total,started=started)
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
        if downloaded>MAX_TG_SIZE:
            await _kill_process(proc,"aria2c")
            raise FileSizeLimitExceeded("TikTok media exceeds 2GB limit. Download canceled.")
        stats.sample(downloaded)
        await stats.emit(bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,title=title_text,kind="TikTok download",label=os.path.basename(out_path),log_interval=2.5,edit_interval=TIKTOK_PROGRESS_INTERVAL)
    _,stderr=await proc.communicate()
    if proc.returncode!=0:
        err=stderr.decode(errors="ignore").strip() if stderr else ""
        raise RuntimeError(err or f"aria2c exited with code {proc.returncode}")
    stats.log_done("TikTok download",label=os.path.basename(out_path))


async def aiohttp_download(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    async with session.get(media_url,headers=headers,timeout=aiohttp.ClientTimeout(total=AIOHTTP_DOWNLOAD_TIMEOUT),allow_redirects=True) as r:
        if r.status>=400:
            raise RuntimeError(f"Download failed: HTTP {r.status}")
        total=int(r.headers.get("Content-Length",0) or 0)
        if total:
            check_media_size_limit(total, "TikTok media")
        downloaded=0
        stats=TransferStats(total)
        chunk_size=max(64*1024,int(TIKTOK_AIOHTTP_CHUNK_SIZE or 256*1024))
        async with aiofiles.open(out_path,"wb") as f:
            async for chunk in r.content.iter_chunked(chunk_size):
                if not chunk:
                    continue
                await f.write(chunk)
                downloaded+=len(chunk)
                if downloaded>MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("TikTok media exceeds 2GB limit. Download canceled.")
                stats.sample(downloaded)
                await stats.emit(bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,title=title_text,kind="TikTok download",label=os.path.basename(out_path),log_interval=2.5,edit_interval=TIKTOK_PROGRESS_INTERVAL)
        stats.log_done("TikTok download",label=os.path.basename(out_path))

async def _download_with_aria2_first(session,media_url:str,out_path:str,bot,chat_id,status_msg_id,title_text:str,headers:dict|None=None):
    aria2_path=shutil.which("aria2c")
    if aria2_path:
        try:
            log.info("TikTok download engine | engine=aria2c path=%s progress=%s",aria2_path,TIKTOK_PROGRESS)
            await aria2c_download(session,media_url,out_path,bot,chat_id,status_msg_id,title_text,headers=headers)
            return
        except FileSizeLimitExceeded:
            _safe_remove_file(out_path,"too large")
            raise
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
    except FileSizeLimitExceeded:
        _safe_remove_file(out_path,"too large")
        raise
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

async def _download_direct_video(media:dict,bot,chat_id,status_msg_id)->dict:
    os.makedirs(TMP_DIR,exist_ok=True)
    session=await get_http_session()
    _purge_tiktok_session_cookies(session)
    title=(media.get("title") or "TikTok Video").strip()
    video_urls=media.get("video_urls") or []
    if media.get("video_url") and media.get("video_url") not in video_urls:
        video_urls.insert(0,media.get("video_url"))
        video_urls=_prioritize_video_urls(video_urls)
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
        except FileSizeLimitExceeded:
            _safe_remove_file(out_path,"too large")
            raise
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
    os.makedirs(TMP_DIR,exist_ok=True)
    session=await get_http_session()
    _purge_tiktok_session_cookies(session)
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
    os.makedirs(TMP_DIR,exist_ok=True)
    session=await get_http_session()
    _purge_tiktok_session_cookies(session)
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
    os.makedirs(TMP_DIR,exist_ok=True)
    session=await get_http_session()
    _purge_tiktok_session_cookies(session)
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
