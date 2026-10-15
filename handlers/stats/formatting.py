def humanize_bytes(n: int) -> str:
    try:
        f = float(n)
    except Exception:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024.0
    return f"{f:.1f}B"


def humanize_duration(sec) -> str:
    try:
        sec = int(sec)
    except Exception:
        return "N/A"
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def humanize_frequency(mhz):
    try:
        mhz = float(mhz)
    except Exception:
        return "N/A"
    if mhz >= 1000:
        return f"{mhz / 1000:.2f} GHz"
    return f"{mhz:.0f} MHz"


def shorten_text(text, limit=64):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def clamp_percent(x):
    try:
        value = float(x)
    except Exception:
        return 0.0
    if value < 0:
        return 0.0
    if value > 100:
        return 100.0
    return value


def build_fallback_text(stats, net_speed=None):
    cpu = stats["cpu"]
    ram = stats["ram"]
    swap = stats["swap"]
    disk = stats["disk"]
    net = stats["net"]
    sysi = stats["sys"]
    runtime = stats["runtime"]

    lines = []
    lines.append("System Stats")
    lines.append("")
    lines.append(f"Host: {sysi['hostname']}")
    lines.append(f"OS: {sysi['os']}")
    lines.append(f"Kernel: {sysi['kernel']}")
    lines.append(f"Python: {sysi['python']}")
    lines.append(f"Uptime: {sysi['uptime']}")
    lines.append("")
    lines.append(f"CPU: {cpu['load']:.1f}% | Cores: {cpu['cores']} | Freq: {cpu['freq']}")
    lines.append(f"RAM: {humanize_bytes(ram['used'])}/{humanize_bytes(ram['total'])} ({ram['pct']:.1f}%)")
    if swap["total"]:
        lines.append(f"SWAP: {humanize_bytes(swap['used'])}/{humanize_bytes(swap['total'])} ({swap['pct']:.1f}%)")
    else:
        lines.append("SWAP: N/A")
    lines.append(f"DISK(/): {humanize_bytes(disk['used'])}/{humanize_bytes(disk['total'])} (used {disk['pct']:.1f}%)")
    lines.append(f"DISK FREE: {humanize_bytes(disk['free'])}")
    lines.append(f"HOST NET: RX {humanize_bytes(net['rx'])} | TX {humanize_bytes(net['tx'])}")
    if isinstance(net_speed, dict):
        lines.append(f"HOST SPEED: RX {humanize_bytes(net_speed.get('rxps', 0))}/s | TX {humanize_bytes(net_speed.get('txps', 0))}/s")
    diag = stats.get("diagnostics", {})
    process = diag.get("process", {})
    lines.append("")
    lines.append("BOT PROCESS")
    lines.append(f"Uptime: {humanize_duration(process.get('uptime'))}")
    lines.append(f"RSS: {humanize_bytes(process.get('rss'))}")
    lines.append(f"CPU: {process.get('cpu', 'N/A')}% (one core = 100%)")
    lines.append(f"Threads: {process.get('threads', 'N/A')} | FDs: {process.get('fds', 'N/A')}")
    lines.append(f"Loop delay (on demand): {diag.get('loop_delay_ms', 0):.1f} ms")
    lines.append(f"Telegram API: {diag.get('api_mode', 'N/A')}")
    lines.append(f"Quote API: {diag.get('quote', 'N/A')}")
    lines.append("")
    lines.append(f"yt-dlp: {runtime['ytdlp']}")
    lines.append(f"Node: {runtime['node']}")
    lines.append(f"Deno: {runtime['deno']}")
    lines.append(f"PTB: {runtime['ptb']}")
    lines.append(f"aiohttp: {runtime['aiohttp']}")
    lines.append(f"aria2c: {runtime['aria2c']}")
    lines.append(f"Pillow: {runtime['pillow']}")
    lines.append(f"psutil: {runtime['psutil']}")
    lines.append(f"aiofiles: {runtime['aiofiles']}")
    return "\n".join(lines)