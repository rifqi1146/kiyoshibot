import os
import re
import subprocess
import unicodedata

def progress_bar(percent: float, length: int = 10) -> str:
    try:
        p = max(0.0, min(100.0, float(percent)))
    except Exception:
        p = 0.0
    filled = int((p / 100.0) * length)
    empty = length - filled
    return f"[{'■' * filled}{'□' * empty}] {p:.1f}%"

def format_size(num: float) -> str:
    try:
        value = float(num or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"

def format_speed(bytes_per_sec: float) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "0 B/s"
    return f"{format_size(bytes_per_sec)}/s"

def format_eta(seconds: float) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if total < 0:
        return "?"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def fix_surrogates(name: str) -> str:
    name = str(name or "")
    name = re.sub(
        "([\ud800-\udbff])([\udc00-\udfff])",
        lambda m: chr(0x10000 + ((ord(m.group(1)) - 0xD800) << 10) + (ord(m.group(2)) - 0xDC00)),
        name,
    )
    return re.sub("[\ud800-\udfff]", "", name)

def sanitize_filename(name: str, max_bytes: int = 120) -> str:
    name = str(name or "media")
    name = fix_surrogates(name)
    name = unicodedata.normalize("NFKC", name)
    name = fix_surrogates(name)
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "media"
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    out = []
    used = 0
    for ch in name:
        b = ch.encode("utf-8")
        if used + len(b) > max_bytes:
            break
        out.append(ch)
        used += len(b)
    return ("".join(out).rstrip(" .") or "media")

def detect_media_type(path: str) -> str:
    ext = os.path.splitext(path.lower())[1]
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return "photo"
    if ext in (".mp4", ".mkv", ".webm", ".mov", ".m4v"):
        return "video"
    return "unknown"

def normalize_url(text: str) -> str:
    text = (text or "").strip()
    text = text.replace("\u200b", "")
    text = text.split("\n")[0]
    return text

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

def extract_all_urls(text: str, limit: int = 8) -> list[str]:
    """Ambil seluruh URL di dalam teks (untuk batch / multi-link download)."""
    raw = (text or "").replace("\u200b", "")
    found: list[str] = []
    seen: set[str] = set()
    for m in _URL_RE.finditer(raw):
        u = m.group(0).rstrip(".,;:!?)]}\"'")
        if u and u not in seen:
            seen.add(u)
            found.append(u)
        if len(found) >= limit:
            break
    return found

def is_invalid_video(path: str) -> bool:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=duration,width,height",
                "-of", "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        info = __import__("json").loads(p.stdout)
        stream = info["streams"][0]

        duration = float(stream.get("duration", 0))
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))

        return duration < 1.5 or width == 0 or height == 0
    except Exception:
        return True

class FileSizeLimitExceeded(RuntimeError):
    pass

def is_media_content_type(content_type: str | None) -> bool:
    """True kalau Content-Type adalah media nyata (bukan JSON, HTML, atau teks error).

    Dipakai di probe HEAD/Range sebelum mempercayai Content-Length: CDN seperti
    TikTok `aweme/v1/play/` membalas HEAD dengan HTTP 200 `Content-Type: application/json`
    berukuran 108 B, padahal videonya di-stream lewat Range/GET berukuran megabyte.
    Mempercayai Content-Length dari response non-media menyebabkan total ukuran
    salah dibaca menjadi ~108 byte.
    """
    ct = (content_type or "").strip().lower().split(";")[0].strip()
    if not ct:
        # Header tidak ada -> jangan tolak buta, biarkan lolos
        return True
    if ct.startswith(("video/", "audio/", "image/", "application/octet-stream", "binary/octet-stream")):
        return True
    if ct in ("application/x-mpegurl", "application/vnd.apple.mpegurl"):
        return True
    # JSON / HTML / text / xml jelas bukan media
    return False


def check_media_size_limit(size_bytes: int | float, label: str = "File") -> None:
    from .constants import MAX_TG_SIZE
    try:
        size = int(size_bytes or 0)
    except (TypeError, ValueError):
        size = 0
    if size > MAX_TG_SIZE:
        gb = size / (1024 * 1024 * 1024)
        raise FileSizeLimitExceeded(f"{label} exceeds 2GB limit ({gb:.2f} GB). Download canceled.")
