import re
import subprocess

try:
    from importlib.metadata import version as pkg_version
except Exception:
    pkg_version = None

def get_package_version(*names):
    if not pkg_version:
        return "N/A"
    for name in names:
        try:
            return pkg_version(name)
        except Exception:
            pass
    return "N/A"

def run_version_command(cmd):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1.5)
        out = (proc.stdout or proc.stderr or "").strip()
        if not out:
            return "N/A"
        return out.splitlines()[0].strip()
    except Exception:
        return "N/A"

def get_node_version():
    out = run_version_command(["node", "-v"])
    return out if out else "N/A"

def get_deno_version():
    out = run_version_command(["deno", "--version"])
    if out == "N/A":
        return "N/A"
    parts = out.split()
    if len(parts) >= 2 and parts[0].lower() == "deno":
        return parts[1]
    return out

def get_ytdlp_version():
    ver = get_package_version("yt-dlp")
    if ver != "N/A":
        return ver
    try:
        from yt_dlp.version import __version__ as yt_dlp_version
        return yt_dlp_version
    except Exception:
        pass
    return get_package_version("yt-dlp-ejs")

def get_aria2c_version():
    try:
        from handlers.dl.proc import resolve_aria2
        binary = resolve_aria2()
    except Exception:
        binary = None
    if not binary:
        return "N/A"
    out = run_version_command([binary, "--version"])
    if out == "N/A":
        return "N/A"
    m = re.search(r"aria2\s+version\s+([^\s]+)", out, flags=re.I)
    if m:
        return m.group(1)
    return out.replace("aria2 version", "").strip() or out

def get_runtime_versions():
    return {
        "ytdlp": get_ytdlp_version(),
        "aria2c": get_aria2c_version(),
        "node": get_node_version(),
        "deno": get_deno_version(),
        "ptb": get_package_version("python-telegram-bot"),
        "aiohttp": get_package_version("aiohttp"),
        "pillow": get_package_version("Pillow"),
        "psutil": get_package_version("psutil"),
        "aiofiles": get_package_version("aiofiles"),
    }

_runtime_cache = {"ts": 0.0, "data": None}

def get_runtime_versions_cached(ttl=300):
    import time
    now = time.time()
    if _runtime_cache["data"] and now - _runtime_cache["ts"] < ttl:
        return dict(_runtime_cache["data"])
    data = get_runtime_versions()
    _runtime_cache["ts"] = now
    _runtime_cache["data"] = data
    return dict(data)