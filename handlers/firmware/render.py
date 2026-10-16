import html
import math
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .constants import LABEL, PER_PAGE, POPULAR_DEVICES, PREFIX, REGIONS


def _filter_items(items: list[dict], region_key: str | None, type_key: str | None = None) -> list[dict]:
    out = list(items)
    if region_key and region_key in REGIONS:
        keyword = REGIONS[region_key][1]
        out = [it for it in out if keyword in (it.get("region") or "").lower()]
    if type_key in ("recovery", "fastboot"):
        out = [it for it in out if (it.get("type") or "").lower() == type_key]
    return out


def _short_version(version: str) -> str:
    v = (version or "").strip()
    if not v:
        return "N/A"
    if len(v) > 12 and v.count(".") >= 3:
        return ".".join(v.split(".")[:4])
    return v


def _rom_flavour(version: str) -> str:
    return "HyperOS" if (version or "").upper().startswith("OS") else "MIUI"


def menu_markup() -> InlineKeyboardMarkup:
    rows = []
    pop_row = []
    for codename, name in POPULAR_DEVICES[:8]:
        pop_row.append(InlineKeyboardButton(name, callback_data=f"{PREFIX}:open:{codename}:0"))
        if len(pop_row) == 2:
            rows.append(pop_row)
            pop_row = []
    if pop_row:
        rows.append(pop_row)

    rows.append([InlineKeyboardButton("Search", callback_data=f"{PREFIX}:prompt:0:0")])
    rows.append([InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0")])
    return InlineKeyboardMarkup(rows)


def build_menu_content() -> tuple[str, str]:
    rich_html = (
        f"<h1>📱 {html.escape(LABEL)}</h1>"
        "<p>Download official ROMs and firmware for every Xiaomi, Redmi and POCO device.</p>"
        "<hr/>"
        "<h3>What You Can Download</h3>"
        "<ul>"
        "<li><b>Recovery ROM</b> — Flashable .zip via system updater</li>"
        "<li><b>Fastboot ROM</b> — Full .tgz image for MiFlash / fastboot</li>"
        "<li><b>All Regions</b> — Indonesia, Global, EEA, China, India, Russia, Turkey, Taiwan</li>"
        "</ul>"
        "<hr/>"
        "<h3>How to Search</h3>"
        "<pre><code>/firmware munch</code></pre>"
        "<pre><code>/firmware poco f4</code></pre>"
        "<hr/>"
        "<aside>Select a device below, or send <code>/firmware &lt;codename / device name&gt;</code> to start.</aside>"
        "<footer>Data source: XM Firmware Updater</footer>"
    )
    plain_html = (
        f"<b>{html.escape(LABEL)}</b>\n"
        "Download official ROMs and firmware for every Xiaomi, Redmi and POCO device.\n\n"
        "<b>What you can download:</b>\n"
        "• <b>Recovery ROM</b> — Flashable .zip via system updater\n"
        "• <b>Fastboot ROM</b> — Full .tgz image for MiFlash / fastboot\n"
        "• <b>All Regions</b> — Indonesia, Global, EEA, China, India, Russia, Turkey, Taiwan\n\n"
        "<b>How to search:</b>\n"
        "<code>/firmware munch</code>\n"
        "<code>/firmware poco f4</code>\n\n"
        "Select a device below, or send the command to start."
    )
    return rich_html, plain_html


def build_search_prompt_content() -> tuple[str, str, InlineKeyboardMarkup]:
    rich_html = (
        "<h1>🔍 Search Xiaomi Device</h1>"
        "<p>Send the command with a codename or a commercial device name.</p>"
        "<hr/>"
        "<h3>Examples</h3>"
        "<pre><code>/firmware munch</code></pre>"
        "<pre><code>/firmware poco f4</code></pre>"
        "<pre><code>/firmware redmi note 10 pro</code></pre>"
        "<hr/>"
        "<aside>Tip: codename gives the fastest and most precise result.</aside>"
    )
    plain_html = (
        "<b>Search Xiaomi Device</b>\n\n"
        "Send the command with a codename or commercial device name.\n\n"
        "<b>Examples:</b>\n"
        "<code>/firmware munch</code>\n"
        "<code>/firmware poco f4</code>\n"
        "<code>/firmware redmi note 10 pro</code>\n\n"
        "Tip: codename gives the fastest and most precise result."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Back to Menu", callback_data=f"{PREFIX}:menu:0:0")]
    ])
    return rich_html, plain_html, markup


def build_matches_content(query: str, matches: list[tuple[str, str]]) -> tuple[str, str, InlineKeyboardMarkup]:
    total = len(matches)
    rows_table = []
    for i, (codename, name) in enumerate(matches, start=1):
        rows_table.append(
            f"<tr><td><b>{i}</b></td><td><code>{html.escape(codename)}</code></td>"
            f"<td>{html.escape(name)}</td></tr>"
        )

    rich_html = (
        "<h1>🔍 Device Matches</h1>"
        f"<p>Query: <code>{html.escape(query)}</code> • {total} device(s) found</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>#</th><th>Codename</th><th>Device</th></tr>"
        f"{''.join(rows_table)}"
        "</table>"
        "<hr/>"
        "<aside>Select a device below to open its ROM list.</aside>"
    )

    plain_blocks = [
        f"<b>{i}.</b> <code>{html.escape(codename)}</code> — {html.escape(name)}"
        for i, (codename, name) in enumerate(matches, start=1)
    ]
    plain_html = (
        f"<b>Device Matches</b>\n"
        f"Query: <code>{html.escape(query)}</code> · {total} device(s) found\n\n"
        + "\n".join(plain_blocks)
    )

    keyboard = []
    for i in range(0, len(matches), 2):
        row_slice = matches[i : i + 2]
        keyboard.append([
            InlineKeyboardButton(
                f"{codename} — {name[:22]}",
                callback_data=f"{PREFIX}:open:{codename}:0",
            )
            for codename, name in row_slice
        ])
    keyboard.append([
        InlineKeyboardButton("Search Again", callback_data=f"{PREFIX}:prompt:0:0"),
        InlineKeyboardButton("Menu", callback_data=f"{PREFIX}:menu:0:0"),
        InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"),
    ])
    return rich_html, plain_html, InlineKeyboardMarkup(keyboard)


def build_results_content(session_id: str, data: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    items = _filter_items(
        data.get("results") or [],
        data.get("region_filter"),
        data.get("type_filter"),
    )
    page = int(data.get("page") or 0)
    total = len(items)
    max_page = max(1, math.ceil(total / PER_PAGE))

    device_name = data.get("device_name") or data.get("codename") or "Unknown"
    codename = data.get("codename") or "-"
    region_key = data.get("region_filter")
    type_key = data.get("type_filter")
    region_text = REGIONS[region_key][0] if region_key in REGIONS else "All Regions"
    type_text = {"recovery": "Recovery", "fastboot": "Fastboot"}.get(type_key, "All Types")

    header = f"📦 {device_name}"

    if not items:
        r_html = (
            f"<h1>{html.escape(header)}</h1>"
            f"<p>Codename: <code>{html.escape(codename)}</code></p>"
            f"<p>Region: {html.escape(region_text)} • Type: {html.escape(type_text)}</p>"
            "<hr/>"
            "<p><i>No ROM found for this filter.</i></p>"
        )
        p_html = (
            f"<b>{html.escape(header)}</b>\n"
            f"Codename: <code>{html.escape(codename)}</code>\n"
            f"Region: {html.escape(region_text)} · Type: {html.escape(type_text)}\n\n"
            "<i>No ROM found for this filter.</i>"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("All Regions", callback_data=f"{PREFIX}:regf:{session_id}:all"),
                InlineKeyboardButton("Region", callback_data=f"{PREFIX}:regions:{session_id}:0"),
                InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"),
            ]
        ])
        return r_html, p_html, markup

    start = page * PER_PAGE
    chunk = items[start : start + PER_PAGE]

    rows_table = []
    plain_blocks = []
    for i, item in enumerate(chunk):
        idx = start + i + 1
        version = html.escape(item.get("version") or "-")
        region = html.escape(item.get("region") or "-")
        rom_type = html.escape(item.get("type") or "-")
        android = html.escape(item.get("android") or "-")
        size = html.escape(item.get("size") or "-")
        date = html.escape(item.get("date") or "-")
        rows_table.append(
            f"<tr><td><b>{idx}</b></td>"
            f"<td><code>{version}</code></td>"
            f"<td>{region}</td>"
            f"<td>{rom_type}</td>"
            f"<td>{android}</td>"
            f"<td>{size}</td>"
            f"<td>{date}</td></tr>"
        )
        plain_blocks.append(
            f"<b>{idx}. <code>{version}</code></b>\n"
            f"   ├─ {region} · {rom_type} · Android {android}\n"
            f"   ├─ {size} · {date}\n"
            f"   └─ Branch: {html.escape(item.get('branch') or '-')}"
        )

    page_info = f"Page {page + 1} of {max_page} • {total} ROM packages"
    rich_html = (
        f"<h1>{header}</h1>"
        f"<p><b>Codename:</b> <code>{html.escape(codename)}</code></p>"
        f"<p>Region: {html.escape(region_text)} • Type: {html.escape(type_text)}</p>"
        f"<p>{page_info}</p>"
        "<hr/>"
        "<table bordered compact>"
        "<tr><th>#</th><th>Version</th><th>Region</th><th>Type</th><th>Android</th><th>Size</th><th>Date</th></tr>"
        f"{''.join(rows_table)}"
        "</table>"
        "<hr/>"
        "<aside>Select a numbered button below to choose a download mirror.</aside>"
        f"<footer>Device: {html.escape(codename)} • Source: XM Firmware Updater</footer>"
    )

    separator = "─" * 24
    plain_body = "\n\n".join(plain_blocks)
    if len(plain_body) > 3200:
        plain_body = plain_body[:3200].rsplit("\n\n", 1)[0] + "\n\n<i>... list truncated for display ...</i>"

    plain_html = (
        f"<b>{html.escape(header)}</b>\n"
        f"Codename: <code>{html.escape(codename)}</code>\n"
        f"Region: {html.escape(region_text)} · Type: {html.escape(type_text)}\n\n"
        f"{plain_body}\n\n{separator}\n<i>{page_info}</i>"
    )

    keyboard = []
    for i in range(0, len(chunk), 5):
        row_items = chunk[i : i + 5]
        keyboard.append([
            InlineKeyboardButton(
                str(start + i + j + 1),
                callback_data=f"{PREFIX}:mirrors:{session_id}:{start + i + j}",
            )
            for j in range(len(row_items))
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"{PREFIX}:nav:{session_id}:{page - 1}"))
    nav.append(InlineKeyboardButton("Region", callback_data=f"{PREFIX}:regions:{session_id}:0"))
    nav.append(InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"))
    if page < max_page - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"{PREFIX}:nav:{session_id}:{page + 1}"))
    keyboard.append(nav)

    return rich_html, plain_html, InlineKeyboardMarkup(keyboard)


def build_region_picker_content(session_id: str, data: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    device_name = data.get("device_name") or "Device"
    codename = data.get("codename") or "-"
    current = data.get("region_filter")
    current_label = REGIONS[current][0] if current in REGIONS else "All Regions"

    rich_html = (
        "<h1>🌍 Filter by Region</h1>"
        f"<p><b>Device:</b> {html.escape(device_name)} (<code>{html.escape(codename)}</code>)</p>"
        f"<p><b>Current filter:</b> {html.escape(current_label)}</p>"
        "<hr/>"
        "<aside>Select a region below to filter the ROM list.</aside>"
    )
    plain_html = (
        "<b>Filter by Region</b>\n"
        f"<b>Device:</b> {html.escape(device_name)} (<code>{html.escape(codename)}</code>)\n"
        f"<b>Current filter:</b> {html.escape(current_label)}\n\n"
        "Select a region below:"
    )

    rows = []
    reg_buttons = []
    for key, (label, _kw) in REGIONS.items():
        active = key == current
        reg_buttons.append(
            InlineKeyboardButton(
                f"• {label} •" if active else label,
                callback_data=f"{PREFIX}:regf:{session_id}:{key}",
            )
        )
        if len(reg_buttons) == 2:
            rows.append(reg_buttons)
            reg_buttons = []
    if reg_buttons:
        rows.append(reg_buttons)

    rows.append([
        InlineKeyboardButton("All Regions", callback_data=f"{PREFIX}:regf:{session_id}:all"),
        InlineKeyboardButton("Back", callback_data=f"{PREFIX}:back:{session_id}:0"),
    ])
    return rich_html, plain_html, InlineKeyboardMarkup(rows)


MIRROR_NAMES = {
    "cdnorg.d.miui.com": "CDN Org",
    "bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com": "Aliyun SG",
    "bn.d.miui.com": "BN CDN",
    "bigota.d.miui.com": "BigOTA",
    "hugeota.d.miui.com": "HugeOTA",
}


def _mirror_label(index: int, host: str) -> str:
    name = MIRROR_NAMES.get(host, host.split(".")[0] if host else "Mirror")
    return f"Mirror {index} ({name})"


def build_mirrors_content(item: dict, blocks: dict) -> tuple[str, str, InlineKeyboardMarkup]:
    device_name = html.escape(item.get("device_name") or "-")
    version = html.escape(item.get("version") or "-")
    region = html.escape(item.get("region") or "-")
    rom_type = html.escape(item.get("type") or "-")
    android = html.escape(item.get("android") or "-")
    size = html.escape(item.get("size") or "-")
    date = html.escape(item.get("date") or "-")
    branch = html.escape(item.get("branch") or "-")
    flavour = _rom_flavour(item.get("version") or "")
    is_fastboot = (item.get("type") or "").lower() == "fastboot"

    rtype = "fastboot" if is_fastboot else "recovery"
    block = (blocks or {}).get(rtype) or {}

    md5 = html.escape(block.get("md5") or "-")
    filename = html.escape(block.get("filename") or "-")
    mirrors = block.get("mirrors") or []
    incremental = block.get("incremental")

    table_rows = [
        f"<tr><td><b>Device</b></td><td>{device_name}</td></tr>",
        f"<tr><td><b>Version</b></td><td><code>{version}</code></td></tr>",
        f"<tr><td><b>ROM</b></td><td>{flavour} • {rom_type}</td></tr>",
        f"<tr><td><b>Android</b></td><td>{android}</td></tr>",
        f"<tr><td><b>Region</b></td><td>{region}</td></tr>",
        f"<tr><td><b>Branch</b></td><td>{branch}</td></tr>",
        f"<tr><td><b>Size</b></td><td>{size}</td></tr>",
        f"<tr><td><b>Released</b></td><td>{date}</td></tr>",
    ]

    rich_html = (
        "<h1>⬇️ Choose a Mirror</h1>"
        f"<table compact>{''.join(table_rows)}</table>"
        "<hr/>"
        "<details open>"
        "<summary><b>Package</b></summary>"
        f"<pre><code>{filename}</code></pre>"
        f"<p><b>MD5:</b> <code>{md5}</code></p>"
        "</details>"
        "<hr/>"
        f"<p><b>Available CDN mirrors ({len(mirrors)}):</b></p>"
        "<aside>All mirrors serve the <b>same file</b>. Pick the fastest one for your location.</aside>"
        "<footer>Source: XM Firmware Updater • Official Xiaomi CDN</footer>"
    )

    lines = [
        "<b>Choose a Mirror</b>\n",
        f"• <b>Device:</b> {device_name}",
        f"• <b>Version:</b> <code>{version}</code>",
        f"• <b>ROM:</b> {flavour} · {rom_type}",
        f"• <b>Android:</b> {android}",
        f"• <b>Region:</b> {region}",
        f"• <b>Branch:</b> {branch}",
        f"• <b>Size:</b> {size}",
        f"• <b>Released:</b> {date}",
        f"\n<b>File:</b>\n<code>{filename}</code>",
        f"<b>MD5:</b> <code>{md5}</code>",
        f"\n<b>Available mirrors ({len(mirrors)}):</b>",
    ]
    for i, mr in enumerate(mirrors, start=1):
        name = MIRROR_NAMES.get(mr.get("host") or "", mr.get("host") or "-")
        lines.append(f"{i}. {html.escape(name)}")
    plain_html = "\n".join(lines)

    rows = []
    for i, mr in enumerate(mirrors, start=1):
        rows.append([
            InlineKeyboardButton(_mirror_label(i, mr.get("host") or ""), url=mr["url"])
        ])
    if incremental and incremental.get("url"):
        rows.append([
            InlineKeyboardButton("Incremental OTA Update", url=incremental["url"])
        ])

    rows.append([
        InlineKeyboardButton("Back to List", callback_data=f"{PREFIX}:list:{item.get('_session_id','')}:0"),
        InlineKeyboardButton("Close", callback_data=f"{PREFIX}:close:0:0"),
    ])

    return rich_html, plain_html, InlineKeyboardMarkup(rows)
