import html
from datetime import datetime
from zoneinfo import ZoneInfo

WIB_TZ = ZoneInfo("Asia/Jakarta")

def esc(val) -> str:
    return html.escape(str(val or ""))

def fmt_int(val) -> str:
    try:
        return f"{int(val):,}"
    except (TypeError, ValueError):
        return "0"

def fmt_duration(seconds) -> str:
    try:
        s = int(round(float(seconds or 0)))
        if s <= 0:
            return "N/A"
        if s >= 3600:
            h, rem = divmod(s, 3600)
            m, sec = divmod(rem, 60)
            return f"{h}h {m}m {sec}s"
        if s >= 60:
            return f"{s // 60}m {s % 60}s"
        return f"{s}s"
    except (TypeError, ValueError):
        return "N/A"

def fmt_codec(raw: str) -> str:
    c = str(raw or "").strip().lower()
    if not c:
        return "N/A"
    if "264" in c or "avc" in c:
        return "H.264"
    if "265" in c or "hvc" in c or "hevc" in c or "bytevc1" in c:
        return "H.265"
    if "av1" in c or "av01" in c:
        return "AV1"
    return c.upper()

def fmt_bitrate(bps) -> str:
    try:
        val = int(bps or 0)
        if val <= 0:
            return "N/A"
        return f"{round(val / 1000):,} kbps"
    except (TypeError, ValueError):
        return "N/A"

def fmt_size(size_bytes) -> str:
    try:
        sz = int(size_bytes or 0)
        if sz <= 0:
            return "N/A"
        return f"{sz / (1024 * 1024):.2f} MB"
    except (TypeError, ValueError):
        return "N/A"

def fmt_wib(unix_ts) -> str:
    try:
        ts = int(float(unix_ts or 0))
        if ts <= 0:
            return "N/A"
        return datetime.fromtimestamp(ts, WIB_TZ).strftime("%b %d, %Y, %H:%M WIB")
    except (TypeError, ValueError, OSError):
        return "N/A"

def tg_time_html(unix_ts, fallback: str = "N/A") -> str:
    try:
        ts = int(float(unix_ts or 0))
    except (TypeError, ValueError):
        ts = 0
    if ts > 0:
        label = fmt_wib(ts)
        return f'<tg-time unix="{ts}" format="wDT">{esc(label)}</tg-time>'
    return esc(fallback)

def quote_block(text: str) -> str:
    escaped = esc(text)
    if len(text) > 140 or text.count("\n") >= 2:
        return f"<blockquote expandable>{escaped}</blockquote>"
    return f"<blockquote>{escaped}</blockquote>"

def kv_table(col1: str, col2: str, rows: list[tuple[str, str, bool]], right_align: bool = False) -> str:
    th2 = f'<th align="right">{esc(col2)}</th>' if right_align else f"<th>{esc(col2)}</th>"
    td_align = ' align="right"' if right_align else ""
    body = []
    for lbl, val, is_code in rows:
        val_html = f"<code>{esc(val)}</code>" if is_code else esc(val)
        body.append(f"<tr><td>{esc(lbl)}</td><td{td_align}>{val_html}</td></tr>")
    return f'<table bordered striped compact><tr><th>{esc(col1)}</th>{th2}</tr>{"".join(body)}</table>'

def kv_raw_table(col1: str, col2: str, rows: list[tuple[str, str]], right_align: bool = False) -> str:
    th2 = f'<th align="right">{esc(col2)}</th>' if right_align else f"<th>{esc(col2)}</th>"
    td_align = ' align="right"' if right_align else ""
    body = "".join(f"<tr><td>{esc(lbl)}</td><td{td_align}>{val_html}</td></tr>" for lbl, val_html in rows)
    return f'<table bordered striped compact><tr><th>{esc(col1)}</th>{th2}</tr>{body}</table>'

def details_kv_block(summary: str, col1: str, col2: str, rows: list[tuple[str, str, bool]]) -> str:
    return f"<hr/><details><summary><b>{esc(summary)}</b></summary>{kv_table(col1, col2, rows)}</details>"

def plain_kv_lines(title: str, rows: list[tuple[str, str, bool]]) -> list[str]:
    lines = ["", f"<b>{esc(title)}</b>"]
    for lbl, val, is_code in rows:
        val_html = f"<code>{esc(val)}</code>" if is_code else esc(val)
        lines.append(f"• <b>{esc(lbl)}:</b> {val_html}")
    return lines

class DetailsReport:
    def __init__(self, title: str, platform: str, meta_rich: list[str], meta_plain: list[str]):
        self.platform = platform
        self.rich = [f"<h1>{esc(title)}</h1>", f"<p>{' • '.join(p for p in meta_rich if p)}</p>"]
        self.plain = [f"<b>{esc(title)}</b>", " • ".join(p for p in meta_plain if p)]

    def quote(self, text: str):
        if text:
            q = quote_block(text)
            self.rich.append(q)
            self.plain.append(q)

    def kv(self, heading: str, col1: str, col2: str, rows: list[tuple[str, str, bool]], right_align: bool = False):
        if not rows:
            return
        self.rich.append(f"<h3>{esc(heading)}</h3>")
        self.rich.append(kv_table(col1, col2, rows, right_align=right_align))
        self.plain.extend(plain_kv_lines(heading, rows))

    def kv_raw(self, heading: str, col1: str, col2: str, rows: list[tuple[str, str]], right_align: bool = False):
        if not rows:
            return
        self.rich.append(f"<h3>{esc(heading)}</h3>")
        self.rich.append(kv_raw_table(col1, col2, rows, right_align=right_align))
        self.plain.append("")
        self.plain.append(f"<b>{esc(heading)}</b>")
        self.plain.extend(f"• <b>{esc(lbl)}:</b> {val_html}" for lbl, val_html in rows)

    def author(
        self,
        heading: str,
        display_name: str,
        handle_html: str,
        badge: str,
        col1: str,
        col2: str,
        rows: list[tuple[str, str, bool]],
        right_align: bool = False,
        bio: str = "",
    ):
        self.rich.append(f"<h3>{esc(heading)}</h3>")
        sub = f"<b>{esc(display_name)}</b> ({handle_html})" if display_name else handle_html
        if badge:
            sub = f"{sub} • {esc(badge)}"
        self.rich.append(f"<p>{sub}</p>")
        if rows:
            self.rich.append(kv_table(col1, col2, rows, right_align=right_align))
        self.plain.append("")
        self.plain.append(f"<b>{esc(heading)}:</b> {sub}")
        for lbl, val, is_code in rows:
            val_html = f"<code>{esc(val)}</code>" if is_code else esc(val)
            self.plain.append(f"• <b>{esc(lbl)}:</b> {val_html}")
        if bio:
            self.quote(bio)

    def grid(self, heading: str, headers: list[tuple[str, bool]], html_rows: list[str], plain_rows: list[str]):
        if not html_rows:
            return
        ths = "".join(
            f'<th align="right">{esc(h)}</th>' if right else f"<th>{esc(h)}</th>"
            for h, right in headers
        )
        self.rich.append(f"<h3>{esc(heading)}</h3>")
        self.rich.append(f'<table bordered striped compact><tr>{ths}</tr>{"".join(html_rows)}</table>')
        self.plain.append("")
        self.plain.append(f"<b>{esc(heading)}</b>")
        self.plain.extend(plain_rows)

    def details(self, summary: str, col1: str, col2: str, rows: list[tuple[str, str, bool]]):
        if not rows:
            return
        self.rich.append(details_kv_block(summary, col1, col2, rows))
        self.plain.extend(plain_kv_lines(summary, rows))

    def build(self) -> tuple[str, str]:
        footer = f"KiyoshiBot {self.platform} Inspector"
        self.rich.append(f"<footer>{esc(footer)}</footer>")
        self.plain.append("")
        self.plain.append(f"<i>{esc(footer)}</i>")
        return "".join(self.rich), "\n".join(self.plain)
