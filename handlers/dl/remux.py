import os
import uuid
import json
import time
import shutil
import logging
import subprocess
import asyncio
import struct
from .constants import TMP_DIR
from .proc import spawn_process, PIPE
from .utils import detect_media_type

log=logging.getLogger(__name__)
FFPROBE_TIMEOUT=int(os.getenv("FFPROBE_TIMEOUT","30"))
FFMPEG_REMUX_TIMEOUT=int(os.getenv("FFMPEG_REMUX_TIMEOUT","180"))
FFMPEG_THUMB_TIMEOUT=int(os.getenv("FFMPEG_THUMB_TIMEOUT","45"))
FFMPEG_HLS_TIMEOUT=int(os.getenv("FFMPEG_HLS_TIMEOUT","900"))

_PREP_ITEM_SEM = asyncio.Semaphore(max(1, min(4, (os.cpu_count() or 2))))

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


_MP4_VIDEO_CODECS=(b"avc1",b"avc3",b"hev1",b"hvc1",b"vp09",b"av01")

def mp4_faststart_meta(path:str)->dict|None:
    try:
        total=os.path.getsize(path)
        if total<32:
            return None
        with open(path,"rb") as fh:
            pos=0
            saw_ftyp=False
            moov=b""
            faststart=False
            for _ in range(64):
                if pos+8>total:
                    break
                fh.seek(pos)
                head=fh.read(8)
                if len(head)<8:
                    break
                box_size,box_type=struct.unpack(">I4s",head)
                header_len=8
                if box_size==1:
                    ext=fh.read(8)
                    if len(ext)<8:
                        break
                    box_size=struct.unpack(">Q",ext)[0]
                    header_len=16
                elif box_size==0:
                    box_size=total-pos
                if box_size<header_len or pos+box_size>total:
                    break
                if box_type==b"ftyp":
                    saw_ftyp=True
                elif box_type==b"moov":
                    if box_size-header_len<=4*1024*1024:
                        moov=fh.read(box_size-header_len)
                elif box_type==b"mdat":
                    faststart=bool(moov)
                    break
                pos+=box_size
            if not (saw_ftyp and faststart and moov):
                return None
            state={"duration":0,"width":0,"height":0,"codec":""}

            def walk(buf):
                p=0
                n=len(buf)
                while p+8<=n:
                    try:
                        bsize,btype=struct.unpack(">I4s",buf[p:p+8])
                    except struct.error:
                        return
                    hlen=8
                    if bsize==1 and p+16<=n:
                        bsize=struct.unpack(">Q",buf[p+8:p+16])[0]
                        hlen=16
                    elif bsize==0:
                        bsize=n-p
                    if bsize<hlen or p+bsize>n:
                        return
                    body=buf[p+hlen:p+bsize]
                    if btype==b"mvhd" and len(body)>=20:
                        ver=body[0]
                        if ver==0:
                            ts,dur=struct.unpack(">II",body[12:20])
                        elif ver==1 and len(body)>=32:
                            ts,dur=struct.unpack(">IQ",body[20:32])
                        else:
                            ts,dur=0,0
                        if ts>0:
                            state["duration"]=max(int(round(dur/ts)),0)
                    elif btype==b"tkhd" and len(body)>=84:
                        ver=body[0]
                        off=76 if ver==0 else 88
                        if len(body)>=off+8:
                            w_fx,h_fx=struct.unpack(">II",body[off:off+8])
                            w_px,h_px=w_fx>>16,h_fx>>16
                            if w_px>0 and h_px>0:
                                state["width"]=w_px
                                state["height"]=h_px
                    elif btype==b"stsd" and len(body)>=16:
                        fmt=body[12:16]
                        if fmt in _MP4_VIDEO_CODECS:
                            state["codec"]="h264" if fmt in (b"avc1",b"avc3") else fmt.decode("latin1")
                    elif btype in (b"trak",b"mdia",b"minf",b"stbl"):
                        walk(body)
                    p+=bsize

            walk(moov)
            if state["duration"]<=0 or state["width"]<=0 or state["height"]<=0:
                return None
            return {
                "duration":state["duration"],
                "width":state["width"],
                "height":state["height"],
                "codec":state["codec"] or "h264",
                "faststart":True,
            }
    except Exception as e:
        log.debug("MP4 faststart probe failed | path=%s err=%r",path,e)
        return None

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
        
        if os.path.exists(remux_path) and os.path.getsize(remux_path)>0:
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
            "-vf","scale=w=320:h=320:force_original_aspect_ratio=decrease",
            "-q:v","3",
            thumb_path,
        ],timeout=FFMPEG_THUMB_TIMEOUT)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path)>0:
            return thumb_path
    except Exception:
        pass

    try:
        _run_cmd([
            "ffmpeg","-y",
            "-ss","00:00:00",
            "-i",src_path,
            "-frames:v","1",
            "-vf","scale=w=320:h=320:force_original_aspect_ratio=decrease",
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

async def remux_and_thumbnail_parallel(src_path:str,thumb_path:str|None=None)->tuple[str, str|None, dict]:
    if not src_path or not os.path.exists(src_path):
        return src_path, None, {}
    if detect_media_type(src_path) != "video":
        return src_path, None, {}

    fast_meta = await asyncio.to_thread(mp4_faststart_meta, src_path)
    have_thumb = bool(thumb_path and os.path.exists(thumb_path))

    if fast_meta:
        log.info(
            "Remux skipped, already faststart | file=%s duration=%s %sx%s thumb=%s",
            os.path.basename(src_path), fast_meta["duration"], fast_meta["width"], fast_meta["height"], have_thumb,
        )
        meta = fast_meta
        if have_thumb:
            return src_path, thumb_path, meta
        thumb = await asyncio.to_thread(make_video_thumbnail, src_path)
        return src_path, thumb, meta

    meta = await asyncio.to_thread(video_meta, src_path)
    remux_fut = asyncio.to_thread(remux_video_for_telegram, src_path, False)
    if have_thumb:
        remuxed_path = await remux_fut
        thumb = thumb_path
    else:
        remuxed_path, thumb = await asyncio.gather(remux_fut, asyncio.to_thread(make_video_thumbnail, src_path))

    if remuxed_path != src_path and os.path.exists(src_path):
        _delete_file(src_path, "original video after parallel remux")

    return remuxed_path, thumb, meta

def convert_gif_to_mp4(src_path:str, delete_src:bool=True)->str:
    if not src_path or not os.path.exists(src_path):
        return src_path
    out_path = f"{TMP_DIR}/{uuid.uuid4().hex}_gif.mp4"
    try:
        _run_cmd([
            "ffmpeg", "-y",
            "-i", src_path,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-movflags", "faststart",
            out_path,
        ], timeout=FFMPEG_REMUX_TIMEOUT)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            if delete_src and out_path != src_path:
                _delete_file(src_path, "original gif after conversion")
            return out_path
    except Exception as e:
        log.warning("GIF to MP4 conversion failed | src=%s err=%s", os.path.basename(src_path), e)
        _delete_file(out_path, "failed gif conversion")
    return src_path


def _prepare_single_path(file_path:str)->str:
    if not file_path or not os.path.exists(file_path):
        return file_path
    if file_path.lower().endswith(".gif"):
        file_path = convert_gif_to_mp4(file_path)
    if detect_media_type(file_path)!="video":
        return file_path
    return remux_video_for_telegram(file_path)

async def download_hls_aes_ffmpeg(
    playlist_url: str,
    referer: str,
    out_path: str,
    user_agent: str = "",
    audio_only: bool = False,
    timeout: float | None = None,
) -> str:
    if timeout is None:
        timeout = FFMPEG_HLS_TIMEOUT
    headers = f"User-Agent: {user_agent}\r\nReferer: {referer or playlist_url}\r\n"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-headers", headers,
        "-i", playlist_url,
    ]
    if audio_only:
        cmd += ["-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path]
    else:
        cmd += ["-c", "copy", "-movflags", "+faststart", out_path]

    log.info("HLS AES-128 ffmpeg start | audio=%s url=%s", audio_only, playlist_url[:80])
    proc = await spawn_process(
        *cmd,
        stdout=PIPE,
        stderr=PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"ffmpeg AES HLS timeout after {FFMPEG_REMUX_TIMEOUT}s")
    if proc.returncode != 0:
        err_msg = (stderr or b"").decode(errors="replace").strip()[-400:]
        raise RuntimeError(f"ffmpeg AES HLS failed: {err_msg}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError("ffmpeg AES HLS produced an empty file")
    log.info("HLS AES-128 done | size=%.2fMB", os.path.getsize(out_path) / 1024 / 1024)
    return out_path


async def prepare_download_result_for_send(result,fmt_key:str="mp4"):
    if fmt_key=="mp3":
        return result

    async def _prep_item(item):
        p = item.get("path")
        if not p or not os.path.exists(p):
            return item
        async with _PREP_ITEM_SEM:
            was_gif = p.lower().endswith(".gif")
            if was_gif:
                p = await asyncio.to_thread(convert_gif_to_mp4, p)
                item["path"] = p
            if detect_media_type(p) == "video":
                item["type"] = "animation" if was_gif else "video"
                new_path, thumb_path, meta = await remux_and_thumbnail_parallel(p, item.get("thumb"))
                item["path"] = new_path
                if thumb_path:
                    item["thumb"] = thumb_path
                if meta:
                    item["meta"] = meta
            else:
                item["path"] = await asyncio.to_thread(_prepare_single_path, p)
        return item

    if isinstance(result,dict) and result.get("items"):
        items=result.get("items") or []
        prepped = await asyncio.gather(*(_prep_item(i) for i in items))
        result["items"] = prepped
        return result
    if isinstance(result,dict):
        p=result.get("path")
        if p and os.path.exists(p):
            was_gif = p.lower().endswith(".gif")
            if was_gif:
                p = await asyncio.to_thread(convert_gif_to_mp4, p)
                result["path"] = p
                result["type"] = "animation"
                result["is_animation"] = True
            if detect_media_type(p)=="video":
                new_path,thumb_path,meta=await remux_and_thumbnail_parallel(p, result.get("thumb"))
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
