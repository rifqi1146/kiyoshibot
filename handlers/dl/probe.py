import shutil
import subprocess
import asyncio
import json
import logging
from urllib.parse import urlparse
from .constants import COOKIES_PATH

log = logging.getLogger(__name__)

YTDLP_RESOLUTION_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "pornhub.com",
    "xhamster.com",
    "xnxx.com",
)

def _host(url: str) -> str:
    try:
        return (urlparse((url or "").strip()).hostname or "").lower()
    except Exception:
        return ""

def _host_match(host: str, domain: str) -> bool:
    host = (host or "").lower()
    domain = (domain or "").lower()
    return host == domain or host.endswith("." + domain)

def supports_ytdlp_resolution(url: str) -> bool:
    host = _host(url)
    return any(_host_match(host, d) for d in YTDLP_RESOLUTION_DOMAINS)

def supports_resolution_picker(url: str) -> bool:
    return supports_ytdlp_resolution(url)

def supports_both_resolution_engines(url: str) -> bool:
    return False

def _format_size(num: int) -> str:
    try:
        value = float(num or 0)
    except Exception:
        value = 0.0
    if value <= 0:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"

def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return default

def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except Exception:
        return default

def _pick_bestaudio_size(formats: list[dict]) -> int:
    best = None
    best_abr = -1.0
    for f in formats:
        vcodec = str(f.get("vcodec") or "")
        acodec = str(f.get("acodec") or "")
        if vcodec != "none":
            continue
        if acodec == "none":
            continue
        abr = _safe_float(f.get("abr"))
        if abr >= best_abr:
            best_abr = abr
            best = f
    if not best:
        return 0
    size = best.get("filesize") or best.get("filesize_approx") or 0
    return _safe_int(size)

def _probe_resolutions_sync(url: str) -> list[dict]:
    yt_dlp_bin = shutil.which("yt-dlp")
    if not yt_dlp_bin:
        log.warning("yt-dlp probe failed | yt-dlp not found in PATH")
        return []

    cmd = [yt_dlp_bin]
    try:
        if COOKIES_PATH:
            cmd += ["--cookies", COOKIES_PATH]
    except NameError:
        pass
        
    host = _host(url)
    if "youtube.com" in host or "youtu.be" in host:
        cmd += [
            "--extractor-args", "youtube:player_client=ios,tv,web;client=ios,tv,web"
        ]

    cmd += ["--no-playlist", "-J", url]

    log.info("yt-dlp probe start | url=%s cookies=%s", url, "yes" if "--cookies" in cmd else "no")
    log.info("yt-dlp probe command | %s", " ".join(cmd))

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        log.warning("yt-dlp probe timed out | url=%s err=%s", url, e)
        return []

    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        log.warning("yt-dlp probe failed | code=%s err=%s", p.returncode, err[-1500:])
        return []

    try:
        info = json.loads(p.stdout)
    except Exception as e:
        log.warning("yt-dlp probe json parse failed | err=%r", e)
        return []

    formats = info.get("formats") or []
    if not isinstance(formats, list):
        log.warning("yt-dlp probe invalid formats | type=%s", type(formats).__name__)
        return []

    title = info.get("title") or "Unknown title"
    bestaudio_size = _pick_bestaudio_size(formats)
    log.info("yt-dlp probe metadata | title=%r raw_formats=%s bestaudio_size=%s", title, len(formats), _format_size(bestaudio_size))

    grouped: dict[int, list[dict]] = {}
    raw_heights = []

    for f in formats:
        if not isinstance(f, dict):
            continue

        format_id = str(f.get("format_id") or "").strip()
        ext = str(f.get("ext") or "").strip().lower()
        vcodec = str(f.get("vcodec") or "")
        acodec = str(f.get("acodec") or "")

        if not format_id:
            continue
        if format_id.startswith("sb"):
            continue
        if ext in ("mhtml", "json", "html"):
            continue
        if vcodec == "none":
            continue

        height = _safe_int(f.get("height"))
        if height <= 0:
            continue

        width = _safe_int(f.get("width"))
        fps = _safe_int(f.get("fps"))
        tbr = _safe_float(f.get("tbr"))

        filesize = f.get("filesize") or f.get("filesize_approx") or 0
        filesize = _safe_int(filesize)

        has_audio = acodec != "none"
        total_size = filesize if has_audio else (filesize + bestaudio_size if filesize else 0)

        item = {
            "height": height,
            "width": width,
            "format_id": format_id,
            "ext": ext,
            "has_audio": has_audio,
            "filesize": filesize,
            "total_size": total_size,
            "vcodec": vcodec,
            "acodec": acodec,
            "fps": fps,
            "tbr": tbr,
        }

        raw_heights.append(height)
        grouped.setdefault(height, []).append(item)

        log.info(
            "yt-dlp format found | height=%sp width=%s fps=%s id=%s ext=%s audio=%s vcodec=%s acodec=%s size=%s total=%s tbr=%.1f",
            height,
            width or "-",
            fps or "-",
            format_id,
            ext or "-",
            "yes" if has_audio else "no",
            vcodec,
            acodec,
            _format_size(filesize),
            _format_size(total_size),
            tbr,
        )

    def _score(item: dict):
        ext = (item.get("ext") or "").lower()
        has_audio = bool(item.get("has_audio"))
        total_size = int(item.get("total_size") or 0)
        filesize = int(item.get("filesize") or 0)
        fps = int(item.get("fps") or 0)
        tbr = float(item.get("tbr") or 0)
        return (
            1 if not has_audio else 0,
            1 if ext == "mp4" else 0,
            fps,
            tbr,
            total_size,
            filesize,
        )

    out = []
    for height, items in grouped.items():
        best = max(items, key=_score)
        out.append({
            "height": best["height"],
            "format_id": best["format_id"],
            "ext": best["ext"],
            "has_audio": best["has_audio"],
            "filesize": best["filesize"],
            "total_size": best["total_size"],
            "fps": best["fps"],
        })

        log.info(
            "yt-dlp picked best for height | height=%sp id=%s ext=%s fps=%s audio=%s total=%s candidates=%s",
            height,
            best["format_id"],
            best["ext"],
            best["fps"] or "-",
            "yes" if best["has_audio"] else "no",
            _format_size(best["total_size"]),
            len(items),
        )

    out.sort(key=lambda x: (int(x.get("height") or 0), int(x.get("fps") or 0)), reverse=True)

    unique_raw = sorted(set(raw_heights), reverse=True)
    final_heights = [x["height"] for x in out]

    log.info("yt-dlp probe raw heights | %s", unique_raw)
    log.info("yt-dlp probe final picker heights | %s", final_heights)
    print("yt-dlp probe raw heights:", unique_raw)
    print("yt-dlp probe final picker heights:", final_heights)

    return out


async def get_resolutions(url: str, engine: str | None = None) -> list[dict]:
    chosen = (engine or "ytdlp").strip().lower()
    if chosen != "ytdlp":
        log.warning("Unsupported resolution engine ignored | url=%s engine=%s", url, chosen)
    if not supports_ytdlp_resolution(url):
        return []
    try:
        res = await asyncio.to_thread(_probe_resolutions_sync, url)
        return res or []
    except Exception as e:
        log.warning("yt-dlp probe failed | url=%s err=%r", url, e)
        return []