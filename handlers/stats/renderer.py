import io
import functools

from utils.fonts import get_font
from .formatting import humanize_bytes, shorten_text, clamp_percent

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None

@functools.lru_cache(maxsize=32)
def load_font(size: int, mono: bool = False):
    if not ImageFont:
        return None
    if mono:
        return get_font(["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "FreeMono.ttf"], size)
    return get_font(["DejaVuSans.ttf", "LiberationSans-Regular.ttf"], size)

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

    width, height = 1920, 1080
    scale = 1.5

    def S(val): 
        return int(val * scale)

    bg_main = (20, 19, 23)
    card_bg = (32, 31, 38)
    text_title = (230, 224, 233)
    text_body = (202, 196, 208)
    text_muted = (147, 143, 153)
    bar_bg = (54, 52, 59)
    accent_blue = (168, 199, 250)
    accent_green = (155, 214, 124)

    img = Image.new("RGB", (width, height), bg_main)
    draw = ImageDraw.Draw(img)

    font_title = load_font(S(36), mono=False)
    font_heading = load_font(S(24), mono=False)
    font_body = load_font(S(18), mono=False)
    font_mono = load_font(S(18), mono=True)
    font_small = load_font(S(15), mono=False)
    font_small_mono = load_font(S(14), mono=True)
    font_tiny = load_font(S(14), mono=False)

    draw.text((S(40), S(40)), "System Stats", font=font_title, fill=text_title)

    col_w = (width - S(40)*2 - S(24)) // 2
    left_x = S(40)
    right_x = S(40) + col_w + S(24)

    y0 = S(120)
    top_h = S(260)
    bottom_h = height - y0 - top_h - S(24)
    card_radius = S(24)

    cpu_card = (left_x, y0, left_x + col_w, y0 + top_h)
    sys_card = (right_x, y0, right_x + col_w, y0 + top_h)
    res_card = (left_x, y0 + top_h + S(24), left_x + col_w, height - S(40))
    net_card = (right_x, y0 + top_h + S(24), right_x + col_w, height - S(40))

    for rect in (cpu_card, sys_card, res_card, net_card):
        draw_rounded_rect(draw, rect, card_radius, fill=card_bg)

    def draw_right(text, x_base, y, font, fill):
        w = get_text_width(font, text)
        draw.text((x_base - w, y), text, font=font, fill=fill)

    cx0, cy0, cx1, cy1 = cpu_card
    draw.text((cx0 + S(24), cy0 + S(24)), "CPU", font=font_heading, fill=text_title)

    cpu = stats["cpu"]
    cpu_load = clamp_percent(cpu["load"])
    
    draw.text((cx0 + S(24), cy0 + S(74)), "Cores", font=font_body, fill=text_muted)
    draw.text((cx0 + S(94), cy0 + S(74)), f": {cpu['cores']}", font=font_mono, fill=text_body)
    
    draw.text((cx0 + S(24), cy0 + S(104)), "Freq", font=font_body, fill=text_muted)
    draw.text((cx0 + S(94), cy0 + S(104)), f": {cpu['freq']}", font=font_mono, fill=text_body)

    draw_progress_bar(draw, cx0 + S(24), cy0 + S(160), col_w - S(48), S(20), cpu_load, bar_bg, accent_blue)
    draw.text((cx0 + S(24), cy0 + S(190)), f"Load: {cpu_load:.1f}%", font=font_mono, fill=text_body)

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

    sys_y = sy0 + S(64)
    for label, val in sys_data:
        draw.text((sx0 + S(24), sys_y), label, font=font_tiny, fill=text_muted)
        draw.text((sx0 + S(100), sys_y), f": {val}", font=font_small_mono, fill=text_body)
        sys_y += S(15)

    rx0, ry0, rx1, ry1 = res_card
    draw.text((rx0 + S(24), ry0 + S(24)), "Memory + Disk", font=font_heading, fill=text_title)
    right_align_x = rx1 - S(24)

    # RAM
    ram = stats["ram"]
    ram_pct = clamp_percent(ram["pct"])
    draw.text((rx0 + S(24), ry0 + S(64)), "RAM", font=font_body, fill=text_title)
    draw_right(f"{humanize_bytes(ram['used'])} / {humanize_bytes(ram['total'])}", right_align_x, ry0 + S(64), font_mono, text_muted)
    draw_progress_bar(draw, rx0 + S(24), ry0 + S(88), col_w - S(48), S(16), ram_pct, bar_bg, accent_blue)
    draw.text((rx0 + S(24), ry0 + S(112)), f"{ram_pct:.1f}%", font=font_mono, fill=text_body)

    # SWAP
    swap = stats["swap"]
    swap_pct = clamp_percent(swap["pct"])
    draw.text((rx0 + S(24), ry0 + S(142)), "Swap", font=font_body, fill=text_title)
    if int(swap["total"] or 0) > 0:
        draw_right(f"{humanize_bytes(swap['used'])} / {humanize_bytes(swap['total'])}", right_align_x, ry0 + S(142), font_mono, text_muted)
        draw_progress_bar(draw, rx0 + S(24), ry0 + S(166), col_w - S(48), S(12), swap_pct, bar_bg, accent_green)
        draw.text((rx0 + S(24), ry0 + S(184)), f"{swap_pct:.1f}%", font=font_small_mono, fill=text_muted)
    else:
        draw_right("N/A", right_align_x, ry0 + S(142), font_mono, text_muted)

    # DISK
    disk = stats["disk"]
    disk_pct = clamp_percent(disk["pct"])
    draw.text((rx0 + S(24), ry0 + S(214)), "Disk (/)", font=font_body, fill=text_title)
    
    # Disk Usage
    disk_info = f"{humanize_bytes(disk['used'])} / {humanize_bytes(disk['total'])} • Used {disk_pct:.1f}% • Free {humanize_bytes(disk['free'])}"
    draw_right(disk_info, right_align_x, ry0 + S(214), font_small_mono, text_muted)
    
    # Progress Bar
    draw_progress_bar(draw, rx0 + S(24), ry0 + S(244), col_w - S(48), S(16), disk_pct, bar_bg, accent_blue)

    # network 
    nx0, ny0, nx1, ny1 = net_card
    draw.text((nx0 + S(24), ny0 + S(24)), "Network", font=font_heading, fill=text_title)

    net = stats["net"]
    
    draw.text((nx0 + S(24), ny0 + S(64)), "RX Total", font=font_body, fill=text_muted)
    draw.text((nx0 + S(116), ny0 + S(64)), f": {humanize_bytes(net['rx'])}", font=font_mono, fill=text_body)
    
    draw.text((nx0 + S(24), ny0 + S(88)), "TX Total", font=font_body, fill=text_muted)
    draw.text((nx0 + S(116), ny0 + S(88)), f": {humanize_bytes(net['tx'])}", font=font_mono, fill=text_body)

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

        draw.text((nx0 + S(24), ny0 + S(134)), "Speed", font=font_body, fill=text_title)     
        draw.text((nx0 + S(24), ny0 + S(164)), "RX/s", font=font_small, fill=text_muted)
        draw.text((nx0 + S(74), ny0 + S(164)), f": {humanize_bytes(int(rxps))}/s", font=font_mono, fill=text_body)
        draw.text((nx0 + S(24), ny0 + S(188)), "TX/s", font=font_small, fill=text_muted)
        draw.text((nx0 + S(74), ny0 + S(188)), f": {humanize_bytes(int(txps))}/s", font=font_mono, fill=text_body)

        # Bar RX
        draw.text((nx0 + S(24), ny0 + S(230)), "RX", font=font_small, fill=text_title)
        draw_progress_bar(draw, nx0 + S(64), ny0 + S(232), col_w - S(88), S(12), rxp, bar_bg, accent_blue)

        # Bar TX
        draw.text((nx0 + S(24), ny0 + S(254)), "TX", font=font_small, fill=text_title)
        draw_progress_bar(draw, nx0 + S(64), ny0 + S(256), col_w - S(88), S(12), txp, bar_bg, accent_green)
    except Exception:
        pass

    bio = io.BytesIO()
    bio.name = "stats.png"
    img.save(bio, format="PNG", compress_level=3)
    bio.seek(0)
    return bio
