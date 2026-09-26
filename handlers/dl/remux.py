import os
import uuid
import json
import time
import shutil
import logging
import subprocess
import asyncio
from .constants import TMP_DIR
from .utils import detect_media_type

log=logging.getLogger(__name__)
FFPROBE_TIMEOUT=int(os.getenv("FFPROBE_TIMEOUT","30"))
FFMPEG_REMUX_TIMEOUT=int(os.getenv("FFMPEG_REMUX_TIMEOUT","180"))
FFMPEG_THUMB_TIMEOUT=int(os.getenv("FFMPEG_THUMB_TIMEOUT","45"))

def _run_cmd(cmd:list[str],timeout:int|float|None=None)->str:
    if not cmd:
        raise RuntimeError("Empty command")
    binary=cmd[0]
    if shutil.which(binary) is None:
        raise RuntimeError(f"{binary} is not installed or not found in PATH")
    try:
        result=subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{binary} timeout after {timeout}s") from e
    if result.returncode!=0:
        err=(result.stderr or result.stdout or f"command failed with exit code {result.returncode}").strip()
        raise RuntimeError(err[-1500:])
    return result.stdout or ""

def _delete_file(path:str|None,label:str):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("Deleted %s file | file=%s",label,os.path.basename(path))
    except Exception as e:
        log.warning("Failed to delete %s file | path=%s err=%r",label,path,e)

_VIDEO_META_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0

def _ffprobe_data(path:str)->dict:
    try:
        out=_run_cmd([
            "ffprobe","-v","error",
            "-print_format","json",
            "-show_format",
            "-show_streams",
            path,
        ],timeout=FFPROBE_TIMEOUT)
        return json.loads(out or "{}")
    except Exception as e:
        log.warning("ffprobe failed | path=%s err=%s",path,e)
        return {}

def video_meta(path:str)->dict:
    """Get metadata with short TTL cache to avoid duplicate ffprobe calls.

    Same file is probed 2-3x during download (remux pre/post, then service).
    Cache keyed by path + mtime + size so a rewrite invalidates.
    """
    try:
        st = os.stat(path)
        cache_key = f"{path}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        cache_key = None
    if cache_key:
        hit = _VIDEO_META_CACHE.get(cache_key)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
            return hit[1]

    data=_ffprobe_data(path)
    streams=data.get("streams") or []
    fmt=data.get("format") or {}
    video=next((s for s in streams if s.get("codec_type")=="video"),{})
    duration_raw=video.get("duration") or fmt.get("duration") or 0
    try:
        duration=float(duration_raw or 0)
    except (TypeError,ValueError):
        duration=0.0
    meta = {
        "duration":max(int(round(duration)),0),
        "width":int(video.get("width") or 0),
        "height":int(video.get("height") or 0),
        "codec":str(video.get("codec_name") or ""),
        "pix_fmt":str(video.get("pix_fmt") or ""),
    }
    if cache_key:
        _VIDEO_META_CACHE[cache_key] = (time.monotonic(), meta)
        # keep cache small
        if len(_VIDEO_META_CACHE) > 256:
            _VIDEO_META_CACHE.clear()
    return meta

def remux_video_for_telegram(src_path:str, delete_src:bool=True)->str:
    before=video_meta(src_path)
    if before["duration"]<=0:
        raise RuntimeError("Invalid video duration")
    remux_path=f"{TMP_DIR}/{uuid.uuid4().hex}_tg_remux.mp4"
    safe_duration = str(before["duration"] + 0.5)
    try:
        _run_cmd([
            "ffmpeg","-y",
            "-i",src_path,
            "-t", safe_duration,
            "-map","0:v?",
            "-map","0:a?",
            "-c","copy",
            "-movflags","faststart",
            remux_path,
        ],timeout=FFMPEG_REMUX_TIMEOUT)
        
        # Optimal: hindari ffprobe kedua. ffmpeg returncode==0 dengan -c copy
        # mempertahankan durasi, resolusi, dan codec persis seperti 'before'.
        if os.path.exists(remux_path) and os.path.getsize(remux_path)>0:
            # Seed cache untuk remux_path memakai metadata 'before'
            try:
                st = os.stat(remux_path)
                ck = f"{remux_path}:{st.st_mtime_ns}:{st.st_size}"
                _VIDEO_META_CACHE[ck] = (time.monotonic(), before)
            except OSError:
                pass
            log.info("Video remuxed | src=%s meta=%s",os.path.basename(src_path),before)
            log.info("Remux done | original=%s output=%s",os.path.basename(src_path),os.path.basename(remux_path))
            if delete_src and remux_path!=src_path:
                _delete_file(src_path,"original video after remux")
            return remux_path
        raise RuntimeError("Remux output invalid")
    except Exception as e:
        log.warning("Video remux failed, using original | src=%s err=%s",os.path.basename(src_path),e)
        _delete_file(remux_path,"failed remux output")
    return src_path


def make_video_thumbnail(src_path:str)->str|None:
    thumb_path=f"{TMP_DIR}/{uuid.uuid4().hex}_thumb.jpg"
    try:
        _run_cmd([
            "ffmpeg","-y",
            "-ss","00:00:01",
            "-i",src_path,
            "-frames:v","1",
            "-vf","scale=320:-2",
            "-q:v","3",
            thumb_path,
        ],timeout=FFMPEG_THUMB_TIMEOUT)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path)>0:
            return thumb_path
        raise RuntimeError("Thumbnail output invalid")
    except Exception as e:
        log.warning("Failed to make video thumbnail | path=%s err=%s",src_path,e)
        _delete_file(thumb_path,"failed thumbnail")
    return None

async def remux_and_thumbnail_parallel(src_path:str)->tuple[str, str|None, dict]:
    """Jalankan faststart remux dan pembuatan thumbnail secara paralel.
    
    Menghilangkan delay sekuensial (hemat ~0.3s) dan menjamin video tetap di-remux
    (tidak ada issue blank hitam di Telegram).
    Mengembalikan (final_video_path, thumb_path, meta_dict).
    """
    if not src_path or not os.path.exists(src_path):
        return src_path, None, {}
    if detect_media_type(src_path) != "video":
        return src_path, None, {}
    
    meta = await asyncio.to_thread(video_meta, src_path)
    
    # Jalankan remux dan thumbnail bersamaan dari src_path
    remux_fut = asyncio.to_thread(remux_video_for_telegram, src_path, False)
    thumb_fut = asyncio.to_thread(make_video_thumbnail, src_path)
    remuxed_path, thumb_path = await asyncio.gather(remux_fut, thumb_fut)
    
    # Bersihkan source jika remux berhasil menghasilkan file baru
    if remuxed_path != src_path and os.path.exists(src_path):
        _delete_file(src_path, "original video after parallel remux")
        
    return remuxed_path, thumb_path, meta

def _prepare_single_path(file_path:str)->str:
    if not file_path or not os.path.exists(file_path):
        return file_path
    if detect_media_type(file_path)!="video":
        return file_path
    return remux_video_for_telegram(file_path)

async def prepare_download_result_for_send(result,fmt_key:str="mp4"):
    if fmt_key=="mp3":
        return result
    if isinstance(result,dict) and result.get("items"):
        items=result.get("items") or []
        paths=[i.get("path") for i in items]
        # Siapkan semua item bersamaan (remux+thumbnail paralel per file).
        prepped=await asyncio.gather(*(asyncio.to_thread(_prepare_single_path,p) for p in paths))
        for item,new_path in zip(items,prepped):
            if new_path:
                item["path"]=new_path
        return result
    if isinstance(result,dict):
        p=result.get("path")
        if p and os.path.exists(p):
            if detect_media_type(p)=="video":
                new_path,thumb_path,meta=await remux_and_thumbnail_parallel(p)
                result["path"]=new_path
                if thumb_path:
                    result["thumb"]=thumb_path
                if meta:
                    result["meta"]=meta
            else:
                result["path"]=await asyncio.to_thread(_prepare_single_path,p)
        return result
    if isinstance(result,str) and os.path.exists(result):
        return await asyncio.to_thread(_prepare_single_path,result)
    return result
