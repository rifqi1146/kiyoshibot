import os
import uuid
import time
import asyncio
import shutil
import json
import logging
import subprocess
from urllib.parse import urlparse
from telegram.error import RetryAfter
from .instagram.main import is_instagram_url
from .constants import COOKIES_PATH,TMP_DIR
from .utils import progress_bar

_SIZE_100MB=100*1024*1024
YTDLP_TIMEOUT=int(os.getenv("YTDLP_TIMEOUT","1800"))
YTDLP_PROBE_TIMEOUT=int(os.getenv("YTDLP_PROBE_TIMEOUT","90"))
GALLERYDL_TIMEOUT=int(os.getenv("GALLERYDL_TIMEOUT","900"))
YTDLP_DENO_PATH=os.getenv("YTDLP_DENO_PATH","deno:/root/.deno/bin/deno")
log=logging.getLogger(__name__)

def _format_dl_value(value:str)->str:
    value=str(value or "").strip()
    if not value or value.lower() in ("n/a","na","unknown","none","null","-"):
        return "?"
    return value

def _clean_percent(value:str)->float:
    text=str(value or "").replace("%","").strip()
    try:
        return max(0.0,min(100.0,float(text)))
    except (TypeError,ValueError):
        return 0.0

def _format_download_status(pct:float,downloaded:str="",total:str="",speed:str="",eta:str="")->str:
    downloaded=_format_dl_value(downloaded)
    total=_format_dl_value(total)
    speed=_format_dl_value(speed)
    eta=_format_dl_value(eta)
    lines=["<b>Downloading...</b>","",f"<code>{progress_bar(pct)}</code>",f"<code>{downloaded}/{total}</code>"]
    if speed!="?":
        lines.append(f"<code>Speed: {speed}</code>")
    if eta!="?":
        lines.append(f"<code>ETA: {eta}</code>")
    return "\n".join(lines)

def _extract_title_from_path(path:str,prefix:str)->str:
    base=os.path.splitext(os.path.basename(path))[0]
    if base.startswith(prefix+"_"):
        base=base[len(prefix)+1:]
    return base.strip() or "Media"

def _looks_like_media_id(text:str)->bool:
    s=(text or "").strip()
    return bool(s) and len(s)>=8 and s.isdigit()

def _host(url:str)->str:
    try:
        return (urlparse((url or "").strip()).hostname or "").lower()
    except Exception as e:
        log.debug("Failed to parse URL host | url=%r err=%r",url,e)
        return ""

def is_x_url(url:str)->bool:
    host=_host(url)
    if host:
        return host in ("x.com","www.x.com","twitter.com","www.twitter.com","mobile.twitter.com")
    text=(url or "").lower()
    return "x.com/" in text or "twitter.com/" in text

def is_youtube_url(url:str)->bool:
    host=_host(url)
    return host in ("youtube.com","www.youtube.com","m.youtube.com","music.youtube.com","youtu.be") or host.endswith(".youtube.com")

def _fallback_title_from_url(url:str)->str:
    try:
        parsed=urlparse((url or "").strip())
        host=(parsed.hostname or "").lower()
        path=(parsed.path or "").strip("/")
        parts=[x for x in path.split("/") if x]
        if host in ("x.com","www.x.com","twitter.com","www.twitter.com","mobile.twitter.com"):
            return "X Media"
        if len(parts)>=2 and parts[0] in ("p","reel","reels","tv"):
            kind=parts[0]
            if kind in ("reel","reels"):
                return "Instagram Reel"
            if kind=="p":
                return "Instagram Post"
            if kind=="tv":
                return "Instagram TV"
        if len(parts)>=3 and parts[0]=="stories":
            return f"Instagram Story @{parts[1]}"
        return "Media"
    except Exception as e:
        log.warning("Failed to derive fallback title from URL | url=%s err=%s",url,e)
        return "Media"

def title_gallerydl(path:str,prefix:str,url:str="")->str:
    title=_extract_title_from_path(path,prefix)
    title=title.replace("_"," ").strip(" -_.")
    if title and not _looks_like_media_id(title):
        return title
    parent=os.path.basename(os.path.dirname(path))
    parent=(parent or "").replace("_"," ").strip(" -_.")
    if parent and "gallerydl" not in parent.lower() and not _looks_like_media_id(parent):
        return parent
    return _fallback_title_from_url(url)

def _strip_job_prefix(path:str,prefix:str)->str:
    try:
        base=os.path.basename(path)
        if not base.startswith(prefix+"_"):
            return path
        clean_name=base[len(prefix)+1:]
        new_path=os.path.join(os.path.dirname(path),clean_name)
        if os.path.abspath(new_path)==os.path.abspath(path):
            return path
        if os.path.exists(new_path):
            stem,ext=os.path.splitext(clean_name)
            new_path=os.path.join(os.path.dirname(path),f"{stem}_{prefix}{ext}")
        os.rename(path,new_path)
        return new_path
    except Exception as e:
        log.warning("Failed to strip job prefix from path | path=%s prefix=%s err=%s",path,prefix,e)
        return path

def _pick_latest_media_file(since_ts:float,prefix:str)->str|None:
    exts=(".mp4",".mp3",".flac",".jpg",".jpeg",".png",".webp",".gif",".webm",".mkv")
    try:
        files=[]
        for f in os.listdir(TMP_DIR):
            if not f.startswith(prefix+"_"):
                continue
            p=os.path.join(TMP_DIR,f)
            if not os.path.isfile(p) or not f.lower().endswith(exts):
                continue
            try:
                mt=os.path.getmtime(p)
            except Exception as e:
                log.warning("Failed to stat candidate media file | path=%s err=%s",p,e)
                continue
            if mt>=since_ts-1:
                files.append((mt,p))
        if not files:
            return None
        files.sort(key=lambda x:x[0],reverse=True)
        return files[0][1]
    except Exception as e:
        log.warning("Failed to pick latest media file | tmp_dir=%s prefix=%s err=%s",TMP_DIR,prefix,e)
        return None

def _collect_media_files_recursive(root_dir:str)->list[str]:
    exts=(".mp4",".mp3",".flac",".jpg",".jpeg",".png",".webp",".gif",".webm",".mkv")
    files=[]
    try:
        for root,_,names in os.walk(root_dir):
            for name in names:
                if not name.lower().endswith(exts):
                    continue
                p=os.path.join(root,name)
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    files.append(p)
    except Exception as e:
        log.warning("Failed to collect media files recursively | root_dir=%s err=%s",root_dir,e)
        return []
    try:
        files.sort(key=lambda p:os.path.getmtime(p))
    except Exception as e:
        log.warning("Failed to sort collected media files | root_dir=%s err=%s",root_dir,e)
    return files



def _append_cookies_args(cmd:list[str])->list[str]:
    if COOKIES_PATH and os.path.exists(COOKIES_PATH):
        cmd.extend(["--cookies",COOKIES_PATH])
    return cmd

def _build_ytdlp_format(format_id:str|None,has_audio:bool=False)->str:
    fid=str(format_id or "").strip()
    if not fid:
        return "bestvideo+bestaudio/best"
    if fid.startswith("height:"):
        h=fid.split(":",1)[1].strip()
        return (
            f"bestvideo[height={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height={h}]+bestaudio[ext=m4a]/"
            f"bestvideo[height={h}]+bestaudio/"
            f"best[height={h}]"
        )
    if has_audio:
        return fid
    return f"{fid}+bestaudio[ext=m4a]/{fid}+bestaudio"

async def _safe_edit_status(bot,chat_id,message_id,text:str):
    try:
        await bot.edit_message_text(chat_id=chat_id,message_id=message_id,text=text,parse_mode="HTML")
    except RetryAfter as e:
        wait=int(getattr(e,"retry_after",1))
        log.warning("Progress edit RetryAfter | chat_id=%s wait=%s",chat_id,wait)
        await asyncio.sleep(wait+1)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return
        log.warning("Failed to edit status message | chat_id=%s message_id=%s err=%s",chat_id,message_id,e)

async def _kill_proc(proc,label:str,job_id:str):
    if proc.returncode is not None:
        return
    try:
        proc.kill()
        await proc.wait()
        log.warning("%s process killed | job_id=%s",label,job_id)
    except ProcessLookupError:
        return
    except Exception as e:
        log.warning("Failed to kill %s process | job_id=%s err=%r",label,job_id,e)

def _safe_rmtree(path:str,label:str):
    try:
        if path and os.path.exists(path):
            shutil.rmtree(path,ignore_errors=True)
    except Exception as e:
        log.warning("Failed to remove %s directory | dir=%s err=%s",label,path,e)

async def gallerydl_fallback(url:str,job_id:str,bot,chat_id,status_msg_id,status_text:str="<b>yt-dlp failed, fallback to gallery-dl...</b>"):
    GALLERY_DL=shutil.which("gallery-dl")
    if not GALLERY_DL:
        log.warning("gallery-dl not found in PATH")
        return None
    job_dir=os.path.join(TMP_DIR,f"{job_id}_gallerydl")
    os.makedirs(job_dir,exist_ok=True)
    proc=None
    try:
        await _safe_edit_status(bot=bot,chat_id=chat_id,message_id=status_msg_id,text=status_text)
        cmd=[GALLERY_DL]
        _append_cookies_args(cmd)
        cmd.extend([
            "--dest", job_dir,
            "--ugoira", "mp4",
            url
        ])
        log.info("Running gallery-dl fallback | url=%s job_id=%s",url,job_id)
        log.debug("gallery-dl command: %s"," ".join(cmd))
        proc=await asyncio.create_subprocess_exec(*cmd,cwd=job_dir,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            stdout,stderr=await asyncio.wait_for(proc.communicate(),timeout=GALLERYDL_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_proc(proc,"gallery-dl",job_id)
            log.warning("gallery-dl timeout | url=%s job_id=%s timeout=%ss",url,job_id,GALLERYDL_TIMEOUT)
            return None
        stdout_text=stdout.decode(errors="ignore") if stdout else ""
        stderr_text=stderr.decode(errors="ignore") if stderr else ""
        if stdout_text:
            log.debug("gallery-dl stdout | job_id=%s\n%s",job_id,stdout_text)
        if stderr_text:
            log.debug("gallery-dl stderr | job_id=%s\n%s",job_id,stderr_text)
        log.info("gallery-dl exit code | job_id=%s code=%s",job_id,proc.returncode)
        if proc.returncode!=0:
            tool_err=_extract_tool_error(stdout_text,stderr_text,proc.returncode,"gallery-dl")
            log.warning("gallery-dl failed | url=%s job_id=%s err=%s",url,job_id,tool_err)
            return None
        files=_collect_media_files_recursive(job_dir)
        if not files:
            log.warning("gallery-dl finished but no media file found | url=%s job_id=%s",url,job_id)
            return None
        moved_items=[]
        for src in files:
            final_name=f"{job_id}_{os.path.basename(src)}"
            final_path=os.path.join(TMP_DIR,final_name)
            if os.path.abspath(src)!=os.path.abspath(final_path):
                if os.path.exists(final_path):
                    stem,ext=os.path.splitext(final_name)
                    final_path=os.path.join(TMP_DIR,f"{stem}_{uuid.uuid4().hex[:6]}{ext}")
                shutil.move(src,final_path)
            moved_items.append({"path":final_path,"title":title_gallerydl(final_path,job_id,url)})
        if len(moved_items)==1:
            return moved_items[0]
        return {"items":moved_items,"title":_fallback_title_from_url(url)}
    except Exception as e:
        log.warning("gallery-dl fallback crashed | url=%s job_id=%s err=%r",url,job_id,e)
        return None
    finally:
        _safe_rmtree(job_dir,"gallery-dl temp")


def _probe_total_size_sync(url:str,fmt:str)->int:
    YT_DLP=shutil.which("yt-dlp")
    if not YT_DLP:
        return 0
    cmd=[YT_DLP]
    _append_cookies_args(cmd)
    cmd+=["--js-runtimes",YTDLP_DENO_PATH,"--no-playlist","-J","-f",fmt,url]
    try:
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=YTDLP_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp size probe timeout | url=%s fmt=%s timeout=%ss",url,fmt,YTDLP_PROBE_TIMEOUT)
        return 0
    except Exception as e:
        log.warning("Failed to probe total size with yt-dlp | url=%s fmt=%s err=%s",url,fmt,e)
        return 0
    if p.returncode!=0:
        log.debug("yt-dlp size probe failed | url=%s fmt=%s code=%s",url,fmt,p.returncode)
        return 0
    try:
        info=json.loads(p.stdout or "{}")
    except Exception as e:
        log.warning("Failed to parse yt-dlp probe JSON | url=%s fmt=%s err=%s",url,fmt,e)
        return 0
    total=info.get("filesize") or info.get("filesize_approx") or 0
    try:
        total=int(total) if total else 0
    except (TypeError,ValueError) as e:
        log.warning("Invalid filesize value from probe | url=%s fmt=%s value=%r err=%s",url,fmt,total,e)
        total=0
    if total:
        return total
    req=info.get("requested_downloads") or []
    s=0
    for d in req:
        fs=d.get("filesize") or d.get("filesize_approx") or 0
        try:
            fs=int(fs) if fs else 0
        except (TypeError,ValueError) as e:
            log.warning("Invalid requested_download filesize value | url=%s fmt=%s value=%r err=%s",url,fmt,fs,e)
            fs=0
        s+=fs
    return s

def _extract_tool_error(stdout_text:str,stderr_text:str,code:int,tool_name:str="yt-dlp")->str:
    skip_starts=("[download]","[info]","[debug]","[generic]","[redirect]","[metadata]")
    merged_lines=[]
    if stderr_text:
        merged_lines.extend(stderr_text.splitlines())
    if stdout_text:
        merged_lines.extend(stdout_text.splitlines())
    for raw in reversed(merged_lines):
        line=(raw or "").strip()
        if not line:
            continue
        lower=line.lower()
        if lower.startswith(skip_starts):
            continue
        if "error:" in lower:
            idx=lower.rfind("error:")
            msg=line[idx+len("error:"):].strip()
            return msg or f"{tool_name} exited with code {code}"
        keys=(
            "unsupported url","unable to extract","video unavailable","private video",
            "sign in to confirm","requested format is not available","http error",
            "forbidden","cloudflare","login required","members only","429","403"
        )
        if any(key in lower for key in keys):
            return line
    tail=[x.strip() for x in merged_lines if (x or "").strip()]
    if tail:
        return tail[-1][:700]
    return f"{tool_name} exited with code {code}"

def _media_priority(p:str)->int:
    p=p.lower()
    if p.endswith(".mp4"):
        return 0
    if p.endswith((".mp3",".flac")):
        return 1
    if p.endswith((".jpg",".jpeg",".png",".webp")):
        return 2
    return 9

def _list_job_outputs(job_id:str)->list[str]:
    exts=(".mp4",".mp3",".flac",".jpg",".jpeg",".png",".webp",".gif",".webm",".mkv")
    try:
        files=[
            os.path.join(TMP_DIR,f)
            for f in os.listdir(TMP_DIR)
            if f.startswith(job_id+"_") and f.lower().endswith(exts)
        ]
        return sorted(files,key=lambda p:(_media_priority(p),-os.path.getmtime(p)))
    except Exception as e:
        log.warning("Failed to list yt-dlp outputs | job_id=%s err=%r",job_id,e)
        return []

async def ytdlp_download(url,fmt_key,bot,chat_id,status_msg_id,format_id:str|None=None,has_audio:bool=False):
    YT_DLP=shutil.which("yt-dlp")
    if not YT_DLP:
        raise RuntimeError("yt-dlp not found in PATH")
    os.makedirs(TMP_DIR,exist_ok=True)
    job_id=uuid.uuid4().hex[:10]
    out_tpl=f"{TMP_DIR}/{job_id}_%(title).120s.%(ext)s"
    update_interval=5
    is_ig=is_instagram_url(url)
    is_x=is_x_url(url)
    is_yt=is_youtube_url(url)

    async def run(cmd):
        nonlocal update_interval
        log.info("Running yt-dlp | url=%s job_id=%s fmt_key=%s",url,job_id,fmt_key)
        log.debug("yt-dlp command | %s"," ".join(cmd))
        proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        last_edit=0.0
        last_pct=-1.0
        stdout_lines=[]
        stderr_lines=[]

        async def read_stdout():
            nonlocal last_edit,last_pct
            while True:
                line=await proc.stdout.readline()
                if not line:
                    break
                raw=line.decode(errors="ignore").strip()
                stdout_lines.append(raw)
                log.debug("yt-dlp stdout | job_id=%s %s",job_id,raw)
                if "|" not in raw:
                    continue
                parts=raw.split("|")
                if len(parts)<6:
                    continue
                pct=_clean_percent(parts[0])
                downloaded=parts[1] if len(parts)>1 else ""
                total_exact=parts[2] if len(parts)>2 else ""
                total_est=parts[3] if len(parts)>3 else ""
                speed=parts[4] if len(parts)>4 else ""
                eta=parts[5] if len(parts)>5 else ""
                total=total_exact if _format_dl_value(total_exact)!="?" else total_est
                if pct<=last_pct and pct<100:
                    continue
                last_pct=pct
                now=time.time()
                if now-last_edit>=update_interval or pct>=100:
                    await _safe_edit_status(
                        bot=bot,
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=_format_download_status(pct=pct,downloaded=downloaded,total=total,speed=speed,eta=eta),
                    )
                    last_edit=now

        async def read_stderr():
            while True:
                line=await proc.stderr.readline()
                if not line:
                    break
                raw=line.decode(errors="ignore").rstrip()
                stderr_lines.append(raw)
                log.debug("yt-dlp stderr | job_id=%s %s",job_id,raw)

        stdout_task=asyncio.create_task(read_stdout())
        stderr_task=asyncio.create_task(read_stderr())
        try:
            await asyncio.wait_for(proc.wait(),timeout=YTDLP_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_proc(proc,"yt-dlp",job_id)
            raise RuntimeError(f"yt-dlp timeout after {YTDLP_TIMEOUT}s")
        await asyncio.gather(stdout_task,stderr_task,return_exceptions=True)
        stdout_text="\n".join(stdout_lines)
        stderr_text="\n".join(stderr_lines)
        if stdout_text:
            log.debug("yt-dlp full stdout | job_id=%s\n%s",job_id,stdout_text)
        if stderr_text:
            log.debug("yt-dlp full stderr | job_id=%s\n%s",job_id,stderr_text)
        log.info("yt-dlp exit code | job_id=%s code=%s",job_id,proc.returncode)
        return proc.returncode,stdout_text,stderr_text

    start_ts=time.time()
    if fmt_key=="mp3":
        update_interval=2
        cmd=[YT_DLP]
        _append_cookies_args(cmd)
        cmd+=[
            "--js-runtimes",YTDLP_DENO_PATH,
            "--extractor-args","youtube:player_client=web",
            "--concurrent-fragments","8",
            "--no-playlist",
            "-f","bestaudio/best",
            "--extract-audio",
            "--audio-format","flac",
            "--audio-quality","0",
            "--newline",
            "--progress-template","%(progress._percent_str)s|%(progress._downloaded_bytes_str)s|%(progress._total_bytes_str)s|%(progress._total_bytes_estimate_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
            "-o",out_tpl,
            url,
        ]
        code,stdout_text,stderr_text=await run(cmd)
        if code!=0:
            raise RuntimeError(_extract_tool_error(stdout_text,stderr_text,code,"yt-dlp"))
    else:
        if is_x:
            fallback=await gallerydl_fallback(url=url,job_id=job_id,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,status_text="<b>Downloading with gallery-dl...</b>")
            if fallback:
                return fallback
        fmt=_build_ytdlp_format(format_id,has_audio)
        log.info("yt-dlp selected format | url=%s format_id=%s has_audio=%s fmt=%s",url,format_id,has_audio,fmt)
        est_size=await asyncio.to_thread(_probe_total_size_sync,url,fmt)
        update_interval=7 if (not est_size and format_id) or est_size>=_SIZE_100MB else 5
        cmd=[YT_DLP]
        if is_ig:
            cmd+=["--ignore-errors","--no-abort-on-error"]
        _append_cookies_args(cmd)
        cmd+=[
            "--js-runtimes",YTDLP_DENO_PATH,
            "--concurrent-fragments","8",
            "--no-playlist",
            "-f",fmt,
            "--merge-output-format","mp4",
            "--newline",
            "--progress-template","%(progress._percent_str)s|%(progress._downloaded_bytes_str)s|%(progress._total_bytes_str)s|%(progress._total_bytes_estimate_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
            "-o",out_tpl,
            url,
        ]
        code,stdout_text,stderr_text=await run(cmd)
        yt_error=_extract_tool_error(stdout_text,stderr_text,code,"yt-dlp")
        if code!=0:
            if is_ig:
                picked=_pick_latest_media_file(start_ts,job_id)
                if picked:
                    return {"path":picked,"title":_extract_title_from_path(picked,job_id)}
            if not is_yt:
                log.warning("yt-dlp video download failed, trying gallery-dl fallback | url=%s job_id=%s err=%s",url,job_id,yt_error)
                fallback=await gallerydl_fallback(url=url,job_id=job_id,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id)
                if fallback:
                    return fallback
            if is_ig:
                picked=_pick_latest_media_file(start_ts,job_id)
                if picked:
                    return {"path":picked,"title":_extract_title_from_path(picked,job_id)}
            raise RuntimeError(yt_error)

    files=_list_job_outputs(job_id)
    log.info("yt-dlp output candidates | job_id=%s count=%s",job_id,len(files))
    log.debug("yt-dlp output files | job_id=%s files=%s",job_id,files)
    if not files:
        raise RuntimeError("yt-dlp selesai tapi file output tidak ditemukan")
    picked=files[0]
    title=title_gallerydl(picked,job_id,url)
    final_path=_strip_job_prefix(picked,job_id)
    return {"path":final_path,"title":title}