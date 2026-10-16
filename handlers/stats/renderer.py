import io
import functools

from utils.fonts import get_font, get_ui_font, get_ui_mono
from .formatting import humanize_bytes, humanize_duration, shorten_text, clamp_percent

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None

@functools.lru_cache(maxsize=32)
def load_font(size: int, mono: bool = False, weight: int = 400):
    if not ImageFont:
        return None
    if mono:
        return get_ui_mono(size)
    return get_ui_font(size, weight=weight)

def get_text_width(font, text):
    try:
        return font.getlength(text)
    except AttributeError:
        return font.getsize(text)[0]

def draw_rounded_rect(draw, xy, radius, fill=None, outline=None, width=1):
    x0, y0, x1, y1 = xy
    try:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill, outline=outline, width=width)
    except Exception:
        draw.rectangle([x0, y0, x1, y1], fill=fill, outline=outline, width=width)

def draw_progress_bar(draw, x, y, w, h, pct, bg, fg, radius=None):
    pct = clamp_percent(pct)
    if radius is None:
        radius = h // 2 
    
    draw_rounded_rect(draw, (x, y, x + w, y + h), radius, fill=bg)
    fill_width = int(round(w * (pct / 100.0)))
    if fill_width > 0:
        fill_width = max(fill_width, radius * 2) if fill_width > 0 else 0
        if fill_width > w: 
            fill_width = w
        draw_rounded_rect(draw, (x, y, x + fill_width, y + h), radius, fill=fg)

def render_dashboard(stats, net_speed=(0.0, 0.0)):
    if not Image or not ImageDraw or not ImageFont:
        return None

    width, height = 1920, 1560
    scale = 1.5

    def S(val): 
        return int(val * scale)

    text_title = (13, 20, 30)
    text_body = (51, 65, 85)
    text_muted = (100, 116, 139)
    bar_bg = (226, 232, 240)
    accent = (236, 72, 153)
    accent_soft = (249, 168, 212)
    card_border = (216, 224, 238)

    img = Image.new("RGB", (width, height), (244, 247, 251))
    draw = ImageDraw.Draw(img)

    from PIL import ImageDraw as ID
    grad = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        grad.putpixel((0, y), (
            int(230 + (255 - 230) * t),
            int(238 + (255 - 238) * t),
            int(245 + (255 - 245) * t),
        ))
    img.paste(grad.resize((width, height)), (0, 0))
    draw = ImageDraw.Draw(img)

    font_title = load_font(S(36), weight=700)
    font_heading = load_font(S(24), weight=600)
    font_body = load_font(S(18), weight=400)
    font_mono = load_font(S(18), mono=True)
    font_small = load_font(S(15), weight=400)
    font_small_mono = load_font(S(14), mono=True)
    font_tiny = load_font(S(14), weight=500)

    draw.text((S(40), S(40)), "System Stats", font=font_title, fill=text_title)

    margin = S(40)
    gap = S(24)
    col_w = (width - margin * 2 - gap) // 2
    left_x = margin
    right_x = margin + col_w + gap

    y1 = S(120)
    h1 = S(290)
    y2 = y1 + h1 + gap
    h2 = S(300)
    y3 = y2 + h2 + gap
    h3 = S(230)

    cpu_card = (left_x, y1, left_x + col_w, y1 + h1)
    sys_card = (right_x, y1, right_x + col_w, y1 + h1)
    res_card = (left_x, y2, left_x + col_w, y2 + h2)
    net_card = (right_x, y2, right_x + col_w, y2 + h2)
    bot_card = (margin, y3, width - margin, y3 + h3)

    for rect in (cpu_card, sys_card, res_card, net_card, bot_card):
        draw_rounded_rect(draw, rect, S(24), fill=(255, 255, 255), outline=card_border, width=1)

    def draw_right(text, x_base, y, font, fill):
        w = get_text_width(font, text)
        draw.text((x_base - w, y), text, font=font, fill=fill)

    cx0, cy0, cx1, cy1 = cpu_card
    draw.text((cx0 + S(24), cy0 + S(24)), "CPU", font=font_heading, fill=text_title)

    cpu = stats["cpu"]
    cpu_load = clamp_percent(cpu["load"])
    
    draw.text((cx0 + S(24), cy0 + S(76)), "Cores", font=font_body, fill=text_muted)
    draw.text((cx0 + S(130), cy0 + S(76)), str(cpu['cores']), font=font_body, fill=text_body)
    
    draw.text((cx0 + S(24), cy0 + S(108)), "Freq", font=font_body, fill=text_muted)
    draw.text((cx0 + S(130), cy0 + S(108)), f"{cpu['freq']}", font=font_body, fill=text_body)

    draw_progress_bar(draw, cx0 + S(24), cy0 + S(196), col_w - S(48), S(13), cpu_load, bar_bg, accent)
    draw.text((cx0 + S(24), cy0 + S(222)), f"Load: {cpu_load:.1f}%", font=font_body, fill=text_body)

    sx0, sy0, sx1, sy1 = sys_card
    draw.text((sx0 + S(24), sy0 + S(24)), "System + Runtime", font=font_heading, fill=text_title)

    sysi = stats["sys"]
    runtime = stats["runtime"]
    sys_data = [
        ("Host", shorten_text(sysi['hostname'], 40)),
        ("OS", shorten_text(sysi['os'], 40)),
        ("Kernel", sysi['kernel']),
        ("Python", sysi['python']),
        ("Uptime", sysi['uptime']),
        ("Node", runtime['node']),
        ("Deno", runtime['deno']),
        ("yt-dlp", runtime['ytdlp']),
        ("aria2c", runtime['aria2c']),
        ("PTB", runtime['ptb']),
        ("HTTP", f"aiohttp {runtime['aiohttp']}"),
        ("Core", f"Pillow {runtime['pillow']} • psutil {runtime['psutil']}"),
    ]

    sys_y = sy0 + S(72)
    for label, val in sys_data:
        draw.text((sx0 + S(24), sys_y), label, font=font_tiny, fill=text_muted)
        draw.text((sx0 + S(130), sys_y), f":  {val}", font=font_tiny, fill=text_body)
        sys_y += S(17)

    rx0, ry0, rx1, ry1 = res_card
    draw.text((rx0 + S(24), ry0 + S(24)), "Memory + Disk", font=font_heading, fill=text_title)
    right_align_x = rx1 - S(24)

    ram = stats["ram"]
    ram_pct = clamp_percent(ram["pct"])
    draw.text((rx0 + S(24), ry0 + S(70)), "RAM", font=font_body, fill=text_title)
    draw_right(f"{humanize_bytes(ram['used'])} / {humanize_bytes(ram['total'])}", right_align_x, ry0 + S(70), font_body, text_muted)
    draw_progress_bar(draw, rx0 + S(24), ry0 + S(98), col_w - S(48), S(13), ram_pct, bar_bg, accent)
    draw.text((rx0 + S(24), ry0 + S(118)), f"{ram_pct:.1f}%", font=font_small, fill=text_body)

    swap = stats["swap"]
    swap_pct = clamp_percent(swap["pct"])
    draw.text((rx0 + S(24), ry0 + S(156)), "Swap", font=font_body, fill=text_title)
    if int(swap["total"] or 0) > 0:
        draw_right(f"{humanize_bytes(swap['used'])} / {humanize_bytes(swap['total'])}", right_align_x, ry0 + S(156), font_body, text_muted)
        draw_progress_bar(draw, rx0 + S(24), ry0 + S(184), col_w - S(48), S(13), swap_pct, bar_bg, accent_soft)
    else:
        draw_right("N/A", right_align_x, ry0 + S(156), font_body, text_muted)

    disk = stats["disk"]
    disk_pct = clamp_percent(disk["pct"])
    draw.text((rx0 + S(24), ry0 + S(224)), "Disk (/)", font=font_body, fill=text_title)
    disk_info = f"{humanize_bytes(disk['used'])} / {humanize_bytes(disk['total'])}   •   Used {disk_pct:.1f}%   •   Free {humanize_bytes(disk['free'])}"
    draw_right(disk_info, right_align_x, ry0 + S(224), font_small, text_muted)
    draw_progress_bar(draw, rx0 + S(24), ry0 + S(252), col_w - S(48), S(13), disk_pct, bar_bg, accent)

    nx0, ny0, nx1, ny1 = net_card
    draw.text((nx0 + S(24), ny0 + S(24)), "Host Traffic", font=font_heading, fill=text_title)

    net = stats["net"]
    
    draw.text((nx0 + S(24), ny0 + S(70)), "RX Total", font=font_body, fill=text_muted)
    draw.text((nx0 + S(140), ny0 + S(70)), str(humanize_bytes(net['rx'])), font=font_body, fill=text_body)
    
    draw.text((nx0 + S(24), ny0 + S(102)), "TX Total", font=font_body, fill=text_muted)
    draw.text((nx0 + S(140), ny0 + S(102)), str(humanize_bytes(net['tx'])), font=font_body, fill=text_body)

    try:
        if isinstance(net_speed, dict):
            rxps = float(net_speed.get("rxps") or 0)
            txps = float(net_speed.get("txps") or 0)
            max_bps = float(net_speed.get("max_bps") or (10 * 1024 * 1024))
        else:
            rxps, txps = net_speed
            max_bps = 10 * 1024 * 1024

        rxp = min(100.0, max(0.0, (rxps / max_bps) * 100.0))
        txp = min(100.0, max(0.0, (txps / max_bps) * 100.0))

        draw.text((nx0 + S(24), ny0 + S(146)), "Speed", font=font_body, fill=text_title)     
        draw.text((nx0 + S(24), ny0 + S(174)), "RX/s", font=font_small, fill=text_muted)
        draw.text((nx0 + S(80), ny0 + S(174)), f"{humanize_bytes(int(rxps))}/s", font=font_small, fill=text_body)
        draw.text((nx0 + S(24), ny0 + S(200)), "TX/s", font=font_small, fill=text_muted)
        draw.text((nx0 + S(80), ny0 + S(200)), f"{humanize_bytes(int(txps))}/s", font=font_small, fill=text_body)

        bar_x = nx0 + S(64)
        bar_w = (nx1 - S(24)) - bar_x

        draw.text((nx0 + S(24), ny0 + S(234)), "RX", font=font_small, fill=text_title)
        draw_progress_bar(draw, bar_x, ny0 + S(236), bar_w, S(12), rxp, bar_bg, accent)

        draw.text((nx0 + S(24), ny0 + S(258)), "TX", font=font_small, fill=text_title)
        draw_progress_bar(draw, bar_x, ny0 + S(260), bar_w, S(12), txp, bar_bg, accent_soft)
    except Exception:
        pass

    bx0, by0, bx1, by1 = bot_card
    diag = stats.get("diagnostics") or {}
    process = diag.get("process") or {}
    draw.text((bx0 + S(24), by0 + S(24)), "Bot Process", font=font_heading, fill=text_title)
    proc_left = [
        ("Uptime", humanize_duration(process.get("uptime"))),
        ("RSS", humanize_bytes(process.get("rss"))),
        ("CPU", f"{process.get('cpu', 0.0):.1f}% (1 core = 100%)"),
        ("Threads", str(process.get("threads", "N/A"))),
    ]
    proc_right = [
        ("FDs", str(process.get("fds", "N/A"))),
        ("Loop sample", f"{diag.get('loop_delay_ms', 0.0):.1f} ms"),
        ("API mode", str(diag.get("api_mode", "N/A"))),
        ("Quote API", str(diag.get("quote", "N/A"))),
    ]
    for rows, col_x in ((proc_left, bx0 + S(24)), (proc_right, nx0 + S(24))):
        row_y = by0 + S(64)
        for label, val in rows:
            draw.text((col_x, row_y), label, font=font_small, fill=text_muted)
            draw.text((col_x + S(130), row_y), str(val), font=font_small, fill=text_body)
            row_y += S(30)

    bio = io.BytesIO()
    bio.name = "stats.png"
    img.save(bio, format="PNG", compress_level=3)
    bio.seek(0)
    return bio
