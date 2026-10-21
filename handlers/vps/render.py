import html
import math
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import vps_db

from .constants import (
    DEFAULT_REGION,
    FEE_BPS,
    DURATIONS,
    OS_IMAGES,
    OWNER_PREFIX,
    REGIONS_PER_PAGE,
    TIERS,
    USER_PREFIX,
    WARRANTY_DAYS,
    NASIONALPAY_MIN_AMOUNT,
    ceil_to_step,
    get_os,
    get_tier,
    normalize_tier,
    region_display,
    sort_regions,
)


def fmt_rp(amount: int) -> str:
    return f"Rp {int(amount):,}".replace(",", ".")


def _fmt_dt(epoch: float) -> str:
    from datetime import datetime, timedelta, timezone
    wib = timezone(timedelta(hours=7))
    return datetime.fromtimestamp(epoch, tz=wib).strftime("%d %b %Y %H:%M WIB")


def user_cb(action: str, *args) -> str:
    parts = [USER_PREFIX, action] + [str(a) for a in args]
    return ":".join(parts)


def owner_cb(action: str, *args) -> str:
    parts = [OWNER_PREFIX, action] + [str(a) for a in args]
    return ":".join(parts)


def _pricing() -> dict[str, dict[str, int]]:
    return vps_db.get_all_pricing()


def _price_for(tier_id: str, days: int) -> int:
    pr = _pricing().get(normalize_tier(tier_id), {})
    key = {30: "price_30d", 14: "price_14d", 7: "price_7d"}.get(int(days), "price_30d")
    return int(pr.get(key, 0) or 0)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Choose Plan", callback_data=user_cb("packages"))],
        [InlineKeyboardButton("My VPS", callback_data=user_cb("myvps"))],
        [InlineKeyboardButton("Close", callback_data=user_cb("close"))],
    ]
    return InlineKeyboardMarkup(rows)


def _fmt_short(amount: int) -> str:
    v = int(amount or 0)
    if v >= 1000:
        return f"{v // 1000}rb"
    return str(v)


def packages_keyboard() -> InlineKeyboardMarkup:
    pricing = _pricing()
    rows = []
    for t in TIERS:
        pr = pricing.get(t["id"], {})
        p30 = pr.get("price_30d", 0)
        p14 = pr.get("price_14d", 0)
        p7 = pr.get("price_7d", 0)
        label = (
            f"{t['label']} / {t['vcpu']} vCPU • "
            f"7d {_fmt_short(p7)} | 14d {_fmt_short(p14)} | 30d {_fmt_short(p30)}"
        )
        rows.append([InlineKeyboardButton(label, callback_data=user_cb("tier", t["id"], "30"))])
    rows.append([InlineKeyboardButton("Back", callback_data=user_cb("menu"))])
    rows.append([InlineKeyboardButton("Close", callback_data=user_cb("close"))])
    return InlineKeyboardMarkup(rows)


def duration_keyboard(tier_id: str) -> InlineKeyboardMarkup:
    pricing = _pricing()
    pr = pricing.get(tier_id, {})
    p30 = pr.get("price_30d", 0)
    p14 = pr.get("price_14d", 0)
    p7 = pr.get("price_7d", 0)
    rows = [
        [InlineKeyboardButton(f"30 Days — {fmt_rp(p30)}", callback_data=user_cb("region", tier_id, "30", "0"))],
        [InlineKeyboardButton(f"14 Days — {fmt_rp(p14)}", callback_data=user_cb("region", tier_id, "14", "0"))],
        [InlineKeyboardButton(f"7 Days — {fmt_rp(p7)}", callback_data=user_cb("region", tier_id, "7", "0"))],
        [InlineKeyboardButton("Back", callback_data=user_cb("packages"))],
        [InlineKeyboardButton("Close", callback_data=user_cb("close"))],
    ]
    return InlineKeyboardMarkup(rows)


def region_keyboard(tier_id: str, days: int, page: int, regions: list[str]) -> InlineKeyboardMarkup:
    ordered = sort_regions(regions)
    total = len(ordered)
    pages = max(1, (total + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * REGIONS_PER_PAGE
    chunk = ordered[start:start + REGIONS_PER_PAGE]

    rows = []
    for r in chunk:
        rows.append([
            InlineKeyboardButton(region_display(r), callback_data=user_cb("os", tier_id, str(days), r))
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=user_cb("region", tier_id, str(days), str(page - 1))))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=user_cb("noop")))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=user_cb("region", tier_id, str(days), str(page + 1))))
    if len(nav) > 1:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Back", callback_data=user_cb("tier", tier_id, str(days)))])
    rows.append([InlineKeyboardButton("Close", callback_data=user_cb("close"))])
    return InlineKeyboardMarkup(rows)


def os_keyboard(
    tier_id: str,
    days: int,
    region: str,
    os_list: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    for o in (os_list if os_list is not None else OS_IMAGES):
        rows.append([
            InlineKeyboardButton(
                o["label"],
                callback_data=user_cb("confirm", tier_id, str(days), region, o["id"]),
            )
        ])
    rows.append([InlineKeyboardButton("Back", callback_data=user_cb("region", tier_id, str(days), "0"))])
    rows.append([InlineKeyboardButton("Close", callback_data=user_cb("close"))])
    return InlineKeyboardMarkup(rows)


def calculate_gross_amount(base: int, fee_bps: int = FEE_BPS) -> int:
    denom = 10000 - fee_bps
    if denom <= 0:
        return base
    return int(math.ceil(base * 10000 / denom))


def _charged_amounts(tier_id: str, days: int) -> tuple[int, int, int]:
    base = _price_for(tier_id, days)
    gross = max(calculate_gross_amount(base, FEE_BPS), NASIONALPAY_MIN_AMOUNT)
    gross = ceil_to_step(gross, 100)
    return base, gross, max(0, gross - base)


def confirm_content(tier_id: str, days: int, region: str, os_id: str) -> tuple[str, str]:
    t = get_tier(tier_id) or {"label": normalize_tier(tier_id), "vcpu": "-", "ram_gb": "-",
                              "disk_gb": "-", "bandwidth": "-", "instance_type": "-"}
    o = get_os(os_id) or {"label": os_id}
    pricing = _pricing().get(normalize_tier(tier_id), {})
    p30, p14, p7 = pricing.get("price_30d", 0), pricing.get("price_14d", 0), pricing.get("price_7d", 0)
    base, gross, fee = _charged_amounts(tier_id, days)
    rich = (
        "<h1>🧾 Confirm Your Order</h1>"
        "<p><b>Are you sure you want to buy this VPS?</b></p>"
        "<hr/>"
        "<table>"
        f"<tr><th>Plan</th><td>{html.escape(t['label'])}</td></tr>"
        f"<tr><th>vCPU</th><td>{t['vcpu']} vCPU</td></tr>"
        f"<tr><th>Memory</th><td>{t['ram_gb']} GB RAM</td></tr>"
        f"<tr><th>Storage</th><td>{t['disk_gb']} GB SSD (gp3)</td></tr>"
        f"<tr><th>Transfer</th><td>{html.escape(str(t['bandwidth']))}</td></tr>"
        f"<tr><th>Instance type</th><td>{html.escape(str(t['instance_type']))}</td></tr>"
        f"<tr><th>Duration</th><td>{days} days</td></tr>"
        f"<tr><th>Region</th><td>{html.escape(region_display(region))}</td></tr>"
        f"<tr><th>Operating System</th><td>{html.escape(o['label'])}</td></tr>"
        f"<tr><th>Root access</th><td>Full root, password sent in chat</td></tr>"
        f"<tr><th>Warranty</th><td>{WARRANTY_DAYS} days</td></tr>"
        "</table>"
        "<hr/>"
        "<table>"
        f"<tr><th>Plan price</th><td>{fmt_rp(base)}</td></tr>"
        f"<tr><th>Admin fee</th><td>{fmt_rp(fee)}</td></tr>"
        f"<tr><th><b>Total to pay</b></th><td><b>{fmt_rp(gross)}</b></td></tr>"
        "</table>"
        "<hr/>"
        "<p><b>Other durations</b></p>"
        "<ul>"
        f"<li>7 days: {fmt_rp(p7)}</li>"
        f"<li>14 days: {fmt_rp(p14)}</li>"
        f"<li>30 days: {fmt_rp(p30)}</li>"
        "</ul>"
        "<p>Pressing <b>Buy Now</b> creates a QRIS invoice valid for 5 minutes. "
        "The server is provisioned automatically once payment is confirmed.</p>"
    )
    plain = (
        "<b>🧾 Confirm Your Order</b>\n\n"
        "<b>Are you sure you want to buy this VPS?</b>\n\n"
        f"<b>Plan:</b> {t['label']}\n"
        f"<b>vCPU:</b> {t['vcpu']} vCPU\n"
        f"<b>Memory:</b> {t['ram_gb']} GB RAM\n"
        f"<b>Storage:</b> {t['disk_gb']} GB SSD (gp3)\n"
        f"<b>Transfer:</b> {t['bandwidth']}\n"
        f"<b>Instance type:</b> {t['instance_type']}\n"
        f"<b>Duration:</b> {days} days\n"
        f"<b>Region:</b> {region_display(region)}\n"
        f"<b>Operating System:</b> {o['label']}\n"
        f"<b>Root access:</b> Full root, password sent in chat\n"
        f"<b>Warranty:</b> {WARRANTY_DAYS} days\n\n"
        f"Plan price: {fmt_rp(base)}\n"
        f"Admin fee: {fmt_rp(fee)}\n"
        f"<b>Total to pay: {fmt_rp(gross)}</b>\n\n"
        "<b>Other durations</b>\n"
        f"• 7 days: {fmt_rp(p7)}\n• 14 days: {fmt_rp(p14)}\n• 30 days: {fmt_rp(p30)}\n\n"
        "Pressing Buy Now creates a QRIS invoice valid for 5 minutes. "
        "The server is provisioned automatically once payment is confirmed."
    )
    return rich, plain


def confirm_keyboard(tier_id: str, days: int, region: str, os_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Buy Now", callback_data=user_cb("checkout", tier_id, str(days), region, os_id))],
        [InlineKeyboardButton("Back", callback_data=user_cb("os", tier_id, str(days), region))],
        [InlineKeyboardButton("Close", callback_data=user_cb("close"))],
    ])


def invoice_keyboard(short_id: str, qr_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Check Status", callback_data=user_cb("ref", short_id)),
            InlineKeyboardButton("Open QR", url=qr_url),
        ],
        [InlineKeyboardButton("Cancel", callback_data=user_cb("cancel", short_id))],
    ])


def myvps_keyboard(instances: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for inst in instances[:8]:
        tier = get_tier(inst["tier"]) or {"label": inst["tier"]}
        label = f"{tier['label']} — {inst['vps_id'].upper()}"
        rows.append([InlineKeyboardButton(label, callback_data=user_cb("detail", inst["vps_id"]))])
    rows.append([InlineKeyboardButton("Back", callback_data=user_cb("menu"))])
    rows.append([InlineKeyboardButton("Close", callback_data=user_cb("close"))])
    return InlineKeyboardMarkup(rows)


def instance_detail_keyboard(
    vps_id: str, status: str, warranty_active: bool, has_firewall: bool = True
) -> InlineKeyboardMarkup:
    rows = []
    if status == "active" and has_firewall:
        rows.append([InlineKeyboardButton("Firewall", callback_data=f"fwr:{vps_id}:menu")])
        rows.append([InlineKeyboardButton("Restart", callback_data=user_cb("restart", vps_id))])
    if warranty_active:
        rows.append([InlineKeyboardButton("Claim Warranty", callback_data=user_cb("warranty", vps_id))])
    rows.append([InlineKeyboardButton("Back", callback_data=user_cb("myvps"))])
    rows.append([InlineKeyboardButton("Close", callback_data=user_cb("close"))])
    return InlineKeyboardMarkup(rows)


def warranty_confirm_keyboard(vps_id: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Confirm Replacement", callback_data=user_cb("wconfirm", vps_id))],
        [InlineKeyboardButton("Cancel", callback_data=user_cb("detail", vps_id))],
    ]
    return InlineKeyboardMarkup(rows)


def owner_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("All Instances", callback_data=owner_cb("list"))],
        [InlineKeyboardButton("Accounts", callback_data=owner_cb("accounts"))],
        [InlineKeyboardButton("Statistics", callback_data=owner_cb("stats"))],
        [InlineKeyboardButton("Set Pricing", callback_data=owner_cb("price"))],
        [InlineKeyboardButton("Close", callback_data=owner_cb("close"))],
    ]
    return InlineKeyboardMarkup(rows)


def owner_list_keyboard(instances: list[dict], show_terminated: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for inst in instances[:10]:
        is_managed = bool(inst.get("managed")) and inst.get("vps_id")
        label = (
            inst["vps_id"].upper() if is_managed
            else (inst.get("name") or str(inst.get("instance_id"))[:18])
        )
        aws_state = inst.get("aws_state") or inst.get("status")
        mark = "" if aws_state == "running" else f" ({aws_state})"
        if is_managed:
            tag = ""
        elif inst.get("tagged_bot") or inst.get("status") == "orphan":
            tag = "[bot] "
        else:
            tag = "[manual] "
        if is_managed:
            cb = owner_cb("info", inst["vps_id"])
        elif inst.get("instance_id"):
            if inst.get("account"):
                cb = owner_cb(
                    "live",
                    inst["instance_id"],
                    inst.get("region") or DEFAULT_REGION,
                    inst["account"],
                )
            else:
                cb = owner_cb("live", inst["instance_id"], inst.get("region") or DEFAULT_REGION)
        else:
            continue
        rows.append([InlineKeyboardButton(f"{tag}{label}{mark}", callback_data=cb)])
    rows.append([InlineKeyboardButton("Rescan", callback_data=owner_cb("rescan"))])
    if show_terminated:
        rows.append([InlineKeyboardButton("Hide Terminated", callback_data=owner_cb("list", "0"))])
    else:
        rows.append([InlineKeyboardButton("Show Terminated", callback_data=owner_cb("list", "1"))])
    rows.append([InlineKeyboardButton("Back", callback_data=owner_cb("menu"))])
    rows.append([InlineKeyboardButton("Close", callback_data=owner_cb("close"))])
    return InlineKeyboardMarkup(rows)


def owner_instance_keyboard(vps_id: str, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "active":
        rows.append([InlineKeyboardButton("Terminate", callback_data=owner_cb("term", vps_id))])
        rows.append([InlineKeyboardButton("Renew +30d", callback_data=owner_cb("renew", vps_id, "30"))])
    rows.append([InlineKeyboardButton("Back", callback_data=owner_cb("list"))])
    rows.append([InlineKeyboardButton("Close", callback_data=owner_cb("close"))])
    return InlineKeyboardMarkup(rows)


def owner_price_keyboard() -> InlineKeyboardMarkup:
    pricing = _pricing()
    rows = []
    for t in TIERS:
        pr = pricing.get(t["id"], {})
        label = f"{t['label']} — {fmt_rp(pr.get('price_30d', 0))} / {fmt_rp(pr.get('price_14d', 0))} / {fmt_rp(pr.get('price_7d', 0))}"
        rows.append([InlineKeyboardButton(label, callback_data=owner_cb("price_tier", t["id"]))])
    rows.append([InlineKeyboardButton("Back", callback_data=owner_cb("menu"))])
    rows.append([InlineKeyboardButton("Close", callback_data=owner_cb("close"))])
    return InlineKeyboardMarkup(rows)


def main_menu_content() -> tuple[str, str]:
    pricing = _pricing()
    min_7d = min((pr.get("price_7d", 0) for pr in pricing.values() if pr.get("price_7d")), default=10000)
    min_14d = min((pr.get("price_14d", 0) for pr in pricing.values() if pr.get("price_14d")), default=15000)
    min_30d = min((pr.get("price_30d", 0) for pr in pricing.values() if pr.get("price_30d")), default=25000)
    rows_html = []
    rows_plain = []
    for t in TIERS:
        pr = pricing.get(t["id"], {})
        p30 = pr.get("price_30d", 0)
        p14 = pr.get("price_14d", 0)
        p7 = pr.get("price_7d", 0)
        rows_html.append(
            f"<tr><td>{html.escape(t['label'])}</td>"
            f"<td>{t['vcpu']} vCPU</td>"
            f"<td>{t['ram_gb']} GB</td>"
            f"<td>{t['disk_gb']} GB</td>"
            f"<td>{fmt_rp(p7)}</td>"
            f"<td>{fmt_rp(p14)}</td>"
            f"<td>{fmt_rp(p30)}</td></tr>"
        )
        rows_plain.append(
            f"{t['label']} — {t['vcpu']} vCPU / {t['ram_gb']} GB / {t['disk_gb']} GB SSD — "
            f"7d: {fmt_rp(p7)} | 14d: {fmt_rp(p14)} | 30d: {fmt_rp(p30)}"
        )

    rich = (
        "<h1>🖥️ AWS VPS</h1>"
        "<p>High-performance cloud servers with low-latency global routing. "
        "Provisioned automatically right after your QRIS payment is confirmed.</p>"
        "<hr/>"
        "<h3>🛡 Service Terms</h3>"
        "<ul>"
        f"<li>Full {WARRANTY_DAYS}-day warranty on every plan</li>"
        "<li>Active period up to 30 days, renewable anytime</li>"
        "<li>Full root access over SSH with password login</li>"
        "<li>Choose Ubuntu 24.04, Ubuntu 22.04, or Debian 12</li>"
        "<li>Pick the region closest to your users</li>"
        "</ul>"
        "<h3>📦 Available Plans</h3>"
        "<table>"
        "<tr><td><b>Plan</b></td><td><b>vCPU</b></td><td><b>RAM</b></td><td><b>Disk</b></td>"
        "<td><b>7 Days</b></td><td><b>14 Days</b></td><td><b>30 Days</b></td></tr>"
        + "".join(rows_html) +
        "</table>"
        "<hr/>"
        f"<p>Plans starting from <b>{fmt_rp(min_7d)}</b> (7 days), <b>{fmt_rp(min_14d)}</b> (14 days), "
        f"or <b>{fmt_rp(min_30d)}</b> (30 days). "
        "Tap <b>Choose Plan</b> to start, or <b>My VPS</b> to manage your servers.</p>"
    )

    plain = (
        "<b>🖥️ AWS VPS</b>\n\n"
        "High-performance cloud servers with low-latency global routing.\n"
        "Provisioned automatically right after your QRIS payment is confirmed.\n\n"
        "<b>🛡 Service Terms</b>\n"
        f"• Full {WARRANTY_DAYS}-day warranty on every plan\n"
        "• Active period up to 30 days, renewable anytime\n"
        "• Full root access over SSH with password login\n"
        "• Choose Ubuntu 24.04, Ubuntu 22.04, or Debian 12\n"
        "• Pick the region closest to your users\n\n"
        "<b>📦 Available Plans</b>\n"
        + "\n".join(rows_plain) +
        "\n\n"
        f"Plans starting from <b>{fmt_rp(min_7d)}</b> (7 days), <b>{fmt_rp(min_14d)}</b> (14 days), "
        f"or <b>{fmt_rp(min_30d)}</b> (30 days)."
    )
    return rich, plain


def packages_content() -> tuple[str, str]:
    pricing = _pricing()
    items_html = []
    items_plain = []
    for t in TIERS:
        pr = pricing.get(t["id"], {})
        p30 = pr.get("price_30d", 0)
        p14 = pr.get("price_14d", 0)
        p7 = pr.get("price_7d", 0)
        items_html.append(
            f"<li><b>{html.escape(t['label'])}</b> — {t['vcpu']} vCPU, {t['ram_gb']} GB RAM, "
            f"{t['disk_gb']} GB SSD, {html.escape(t['bandwidth'])} transfer — "
            f"<b>{fmt_rp(p7)}</b> (7d) / <b>{fmt_rp(p14)}</b> (14d) / <b>{fmt_rp(p30)}</b> (30d)</li>"
        )
        items_plain.append(
            f"• {t['label']} — {t['vcpu']} vCPU, {t['ram_gb']} GB RAM, {t['disk_gb']} GB SSD — "
            f"{fmt_rp(p7)} (7d) / {fmt_rp(p14)} (14d) / {fmt_rp(p30)} (30d)"
        )

    rich = (
        "<h1>📦 Choose a VPS Plan</h1>"
        f"<p>Every plan includes a full {WARRANTY_DAYS}-day warranty from activation.</p>"
        "<ul>" + "".join(items_html) + "</ul>"
        "<hr/>"
        "<p>Select a plan below to choose your duration and region.</p>"
    )
    plain = (
        "<b>📦 Choose a VPS Plan</b>\n\n"
        f"Every plan includes a full {WARRANTY_DAYS}-day warranty from activation.\n\n"
        + "\n".join(items_plain) +
        "\n\nSelect a plan below to choose your duration and region."
    )
    return rich, plain


def duration_content(tier_id: str) -> tuple[str, str]:
    tier_id = normalize_tier(tier_id)
    t = get_tier(tier_id)
    pricing = _pricing()
    pr = pricing.get(tier_id, {})
    if not t:
        return "<h1>Plan Not Found</h1>", "<b>Plan Not Found</b>"
    p30 = pr.get("price_30d", 0)
    p14 = pr.get("price_14d", 0)
    p7 = pr.get("price_7d", 0)
    rich = (
        f"<h1>⏳ {html.escape(t['label'])} — Select Duration</h1>"
        "<ul>"
        f"<li>{t['vcpu']} vCPU</li>"
        f"<li>{t['ram_gb']} GB RAM</li>"
        f"<li>{t['disk_gb']} GB SSD</li>"
        f"<li>{html.escape(t['bandwidth'])} transfer</li>"
        f"<li>{WARRANTY_DAYS}-day warranty</li>"
        "</ul>"
        "<hr/>"
        f"<p><b>30 Days:</b> {fmt_rp(p30)}<br/><b>14 Days:</b> {fmt_rp(p14)}<br/>"
        f"<b>7 Days:</b> {fmt_rp(p7)}</p>"
        "<p>The 30-day option offers the lowest cost per day.</p>"
    )
    plain = (
        f"<b>⏳ {t['label']} — Select Duration</b>\n\n"
        f"• {t['vcpu']} vCPU\n• {t['ram_gb']} GB RAM\n• {t['disk_gb']} GB SSD\n"
        f"• {t['bandwidth']} transfer\n• {WARRANTY_DAYS}-day warranty\n\n"
        f"<b>30 Days:</b> {fmt_rp(p30)}\n<b>14 Days:</b> {fmt_rp(p14)}\n<b>7 Days:</b> {fmt_rp(p7)}\n\n"
        "The 30-day option offers the lowest cost per day."
    )
    return rich, plain


def region_content(tier_id: str, days: int, page: int, regions: list[str]) -> tuple[str, str]:
    t = get_tier(tier_id)
    price = _price_for(tier_id, days)
    if not t:
        return "<h1>Plan Not Found</h1>", "<b>Plan Not Found</b>"
    ordered = sort_regions(regions)
    total = len(ordered)
    pages = max(1, (total + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * REGIONS_PER_PAGE
    chunk = ordered[start:start + REGIONS_PER_PAGE]

    items_html = "".join(
        f"<li>{html.escape(region_display(r))} — <code>{html.escape(r)}</code></li>" for r in chunk
    )
    items_plain = "\n".join(
        f"• {region_display(r)} — {r}" for r in chunk
    )

    rich = (
        f"<h1>🌍 Select a Region</h1>"
        f"<p>Plan: <b>{html.escape(t['label'])}</b> · Duration: <b>{days} days</b></p>"
        "<p>Choose the datacenter closest to your users for the best latency. "
        "Your server will be created in the region you pick.</p>"
        f"<p>Page {page + 1} of {pages} · {total} regions available</p>"
        "<ul>" + items_html + "</ul>"
        "<hr/>"
        f"<p><b>Total amount:</b> {fmt_rp(price)} ({days} days, {WARRANTY_DAYS}-day warranty)</p>"
    )
    plain = (
        f"<b>🌍 Select a Region</b>\n\n"
        f"Plan: <b>{t['label']}</b> · Duration: <b>{days} days</b>\n\n"
        "Choose the datacenter closest to your users for the best latency.\n\n"
        f"Page {page + 1} of {pages} · {total} regions available\n\n"
        + items_plain +
        f"\n\n<b>Total amount:</b> {fmt_rp(price)} ({days} days, {WARRANTY_DAYS}-day warranty)"
    )
    return rich, plain


def os_content(
    tier_id: str,
    days: int,
    region: str,
    os_list: list[dict] | None = None,
) -> tuple[str, str]:
    t = get_tier(tier_id)
    price = _price_for(tier_id, days)
    if not t:
        return "<h1>Plan Not Found</h1>", "<b>Plan Not Found</b>"
    op_html = []
    op_plain = []
    for o in (os_list if os_list is not None else OS_IMAGES):
        op_html.append(f"<li><b>{html.escape(o['label'])}</b></li>")
        op_plain.append(f"• {o['label']}")
    rich = (
        f"<h1>💿 Select Operating System</h1>"
        f"<p>Plan: <b>{html.escape(t['label'])}</b> · Duration: <b>{days} days</b><br/>"
        f"Region: <b>{html.escape(region_display(region))}</b></p>"
        "<p>After payment is confirmed, your server is provisioned automatically "
        "with a root password delivered right here in this chat.</p>"
        "<h3>Available Operating Systems</h3>"
        "<ul>" + "".join(op_html) + "</ul>"
        "<hr/>"
        f"<p><b>Total amount:</b> {fmt_rp(price)} ({days} days, {WARRANTY_DAYS}-day warranty)</p>"
    )
    plain = (
        f"<b>💿 Select Operating System</b>\n\n"
        f"Plan: <b>{t['label']}</b> · Duration: <b>{days} days</b>\n"
        f"Region: <b>{region_display(region)}</b>\n\n"
        "After payment is confirmed, your server is provisioned automatically "
        "with a root password delivered right here in this chat.\n\n"
        + "\n".join(op_plain) +
        f"\n\n<b>Total amount:</b> {fmt_rp(price)}"
    )
    return rich, plain


def _invoice_fees(inv: dict) -> tuple[int, int]:
    base = int(inv.get("base_amount") or 0)
    gross = int(inv.get("gross_amount") or 0)
    return base, max(0, gross - base)


def invoice_content(inv: dict, *, status: str, remaining: int) -> tuple[str, str]:
    t = get_tier(inv["tier"])
    o = get_os(inv["os_image"])
    rem_m, rem_s = divmod(max(0, remaining), 60)
    tier_label = t["label"] if t else normalize_tier(inv["tier"])
    os_label = o["label"] if o else inv["os_image"]
    region = inv.get("region") or "-"
    base, fee = _invoice_fees(inv)
    rich = (
        "<h1>🧾 VPS Invoice — QRIS</h1>"
        f"<p><b>Status:</b> {html.escape(status)}<br/>"
        f"<b>Plan:</b> {html.escape(tier_label)} / {inv['days']} days<br/>"
        f"<b>Region:</b> {html.escape(region_display(region))}<br/>"
        f"<b>Operating System:</b> {html.escape(os_label)}<br/>"
        f"<b>Reference:</b> <code>{html.escape(inv['trx_id'])}</code></p>"
        "<hr/>"
        "<table>"
        f"<tr><th>Plan price</th><td>{fmt_rp(base)}</td></tr>"
        f"<tr><th>Admin fee</th><td>{fmt_rp(fee)}</td></tr>"
        f"<tr><th><b>Total to pay</b></th><td><b>{fmt_rp(inv['gross_amount'])}</b></td></tr>"
        "</table>"
        "<hr/>"
        f"<p><b>Time Remaining:</b> {rem_m:02d}:{rem_s:02d}</p>"
        "<p>Scan the QR code above with any QRIS-compatible app (GoPay, OVO, DANA, "
        "ShopeePay, or mobile banking). Payment status is polled automatically every 15 seconds "
        "and provisioning starts the moment payment is confirmed. You can also press "
        "<b>Check Status</b> to refresh instantly.</p>"
    )
    plain = (
        "<b>🧾 VPS Invoice — QRIS</b>\n\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Plan:</b> {tier_label} / {inv['days']} days\n"
        f"<b>Region:</b> {region_display(region)}\n"
        f"<b>Operating System:</b> {os_label}\n"
        f"<b>Reference:</b> <code>{html.escape(inv['trx_id'])}</code>\n\n"
        f"Plan price: {fmt_rp(base)}\n"
        f"Admin fee: {fmt_rp(fee)}\n"
        f"<b>Total to pay: {fmt_rp(inv['gross_amount'])}</b>\n\n"
        f"<b>Time Remaining:</b> {rem_m:02d}:{rem_s:02d}\n\n"
        "Scan the QR code above to pay. Provisioning starts automatically once payment is confirmed."
    )
    return rich, plain


def provisioning_content(inv: dict) -> tuple[str, str]:
    t = get_tier(inv["tier"])
    o = get_os(inv["os_image"])
    tier_label = t["label"] if t else normalize_tier(inv["tier"])
    os_label = o["label"] if o else inv["os_image"]
    region = inv.get("region") or "-"
    rich = (
        "<h1>✅ Payment Successful</h1>"
        "<p><b>Preparing your VPS, please wait.</b></p>"
        "<p>Estimated time: <b>30–60 seconds</b>. Do not close this chat.</p>"
        "<hr/>"
        "<ul>"
        f"<li>Plan: {html.escape(tier_label)}</li>"
        f"<li>Region: {html.escape(region_display(region))}</li>"
        f"<li>Operating System: {html.escape(os_label)}</li>"
        f"<li>Duration: {inv['days']} days</li>"
        "</ul>"
        "<p>We are booting your server and verifying SSH access. "
        "Login credentials will be sent here automatically once the server responds.</p>"
    )
    plain = (
        "<b>✅ Payment Successful</b>\n\n"
        "<b>Preparing your VPS, please wait.</b>\n"
        "Estimated time: <b>30–60 seconds</b>. Do not close this chat.\n\n"
        f"• Plan: {tier_label}\n"
        f"• Region: {region_display(region)}\n"
        f"• OS: {os_label}\n"
        f"• Duration: {inv['days']} days\n\n"
        "We are booting your server and verifying SSH access. "
        "Login credentials will be sent here automatically once the server responds."
    )
    return rich, plain


def provisioned_content(inst: dict) -> tuple[str, str]:
    t = get_tier(inst["tier"])
    o = get_os(inst["os_image"])
    tier_label = t["label"] if t else normalize_tier(inst["tier"])
    os_label = o["label"] if o else inst["os_image"]
    region = inst.get("region") or "-"
    rich = (
        "<h1>🚀 VPS Active</h1>"
        f"<p><b>Server ID:</b> <code>{html.escape(inst['vps_id'].upper())}</code></p>"
        "<h3>🔑 Access Details</h3>"
        "<ul>"
        f"<li>IP Address: <code>{html.escape(inst['ip_address'] or '-')}</code></li>"
        "<li>SSH Port: <code>22</code></li>"
        "<li>Username: <code>root</code></li>"
        f"<li>Password: <code>{html.escape(inst['root_password'] or '-')}</code></li>"
        "</ul>"
        "<h3>Specifications</h3>"
        "<ul>"
        f"<li>Plan: {html.escape(tier_label)} ({t['vcpu'] if t else 2} vCPU, "
        f"{t['ram_gb'] if t else '-'} GB RAM, {t['disk_gb'] if t else '-'} GB SSD)</li>"
        f"<li>Operating System: {html.escape(os_label)}</li>"
        f"<li>Region: {html.escape(region_display(region))}</li>"
        "</ul>"
        "<h3>Validity</h3>"
        "<ul>"
        f"<li>Active Until: {_fmt_dt(inst['expires_at'])}</li>"
        f"<li>Warranty Until: {_fmt_dt(inst['warranty_until'])}</li>"
        "</ul>"
        "<pre><code>ssh root@" + html.escape(inst["ip_address"] or "-") + "</code></pre>"
        "<p>Save these credentials somewhere safe. Use the <b>My VPS</b> button to manage your server.</p>"
    )
    plain = (
        "<b>🚀 VPS Active</b>\n\n"
        f"<b>Server ID:</b> <code>{inst['vps_id'].upper()}</code>\n\n"
        "<b>🔑 Access Details</b>\n"
        f"• IP: <code>{inst['ip_address'] or '-'}</code>\n"
        "• SSH Port: <code>22</code>\n"
        "• Username: <code>root</code>\n"
        f"• Password: <code>{inst['root_password'] or '-'}</code>\n\n"
        "<b>Specifications</b>\n"
        f"• Plan: {tier_label}\n"
        f"• OS: {os_label}\n"
        f"• Region: {region_display(region)}\n\n"
        "<b>Validity</b>\n"
        f"• Active Until: {_fmt_dt(inst['expires_at'])}\n"
        f"• Warranty Until: {_fmt_dt(inst['warranty_until'])}\n\n"
        f"<pre><code>ssh root@{inst['ip_address'] or '-'}</code></pre>"
    )
    return rich, plain


def myvps_content(instances: list[dict]) -> tuple[str, str]:
    if not instances:
        rich = (
            "<h1>💻 My VPS</h1>"
            "<p>You do not have any active servers yet. Open the VPS menu to deploy your first server.</p>"
        )
        plain = "<b>💻 My VPS</b>\n\nYou do not have any active servers yet."
        return rich, plain

    items_html = []
    items_plain = []
    for inst in instances:
        t = get_tier(inst["tier"]) or {"label": inst["tier"], "vcpu": "-", "ram_gb": "-", "disk_gb": "-"}
        status = inst["status"]
        region = inst.get("region") or "-"
        items_html.append(
            f"<li><b>{html.escape(inst['vps_id'].upper())}</b> — {html.escape(t['label'])}, "
            f"{html.escape(region_display(region))}, status <b>{html.escape(status)}</b>, "
            f"expires {_fmt_dt(inst['expires_at'])}</li>"
        )
        items_plain.append(
            f"• {inst['vps_id'].upper()} — {t['label']}, {region_display(region)}, "
            f"{status}, until {_fmt_dt(inst['expires_at'])}"
        )
    rich = (
        "<h1>💻 My VPS</h1>"
        "<p>Your servers. Select one below to view details and management options.</p>"
        "<ul>" + "".join(items_html) + "</ul>"
    )
    plain = "<b>💻 My VPS</b>\n\nSelect a server below.\n\n" + "\n".join(items_plain)
    return rich, plain


def instance_detail_content(inst: dict) -> tuple[str, str]:
    t = get_tier(inst["tier"])
    o = get_os(inst["os_image"])
    tier_label = t["label"] if t else normalize_tier(inst["tier"])
    os_label = o["label"] if o else inst["os_image"]
    status = inst["status"]
    region = inst.get("region") or "-"
    warranty_active = status == "active" and time.time() < inst["warranty_until"] and not inst["warranty_claimed"]
    warranty_line = (
        f"Warranty active until {_fmt_dt(inst['warranty_until'])}"
        if warranty_active
        else ("Warranty expired" if not inst["warranty_claimed"] else "Warranty already claimed")
    )
    rich = (
        f"<h1>🖥 {html.escape(inst['vps_id'].upper())}</h1>"
        f"<p><b>Status:</b> {html.escape(status.capitalize())}</p>"
        "<h3>🔑 Access Details</h3>"
        "<ul>"
        f"<li>IP Address: <code>{html.escape(inst['ip_address'] or '-')}</code></li>"
        "<li>SSH Port: <code>22</code></li>"
        "<li>Username: <code>root</code></li>"
        f"<li>Password: <code>{html.escape(inst['root_password'] or '-')}</code></li>"
        "</ul>"
        "<h3>Specifications</h3>"
        "<ul>"
        f"<li>Plan: {html.escape(tier_label)}</li>"
        f"<li>Operating System: {html.escape(os_label)}</li>"
        f"<li>Region: {html.escape(region_display(region))}</li>"
        "</ul>"
        "<h3>Validity</h3>"
        "<ul>"
        f"<li>Active Until: {_fmt_dt(inst['expires_at'])}</li>"
        f"<li>{html.escape(warranty_line)}</li>"
        "</ul>"
        "<pre><code>ssh root@" + html.escape(inst["ip_address"] or "-") + "</code></pre>"
    )
    plain = (
        f"<b>🖥 {inst['vps_id'].upper()}</b>\n\n"
        f"<b>Status:</b> {status.capitalize()}\n\n"
        "<b>🔑 Access Details</b>\n"
        f"• IP: <code>{inst['ip_address'] or '-'}</code>\n"
        "• SSH Port: <code>22</code>\n"
        "• Username: <code>root</code>\n"
        f"• Password: <code>{inst['root_password'] or '-'}</code>\n\n"
        "<b>Specifications</b>\n"
        f"• Plan: {tier_label}\n"
        f"• OS: {os_label}\n"
        f"• Region: {region_display(region)}\n\n"
        "<b>Validity</b>\n"
        f"• Active Until: {_fmt_dt(inst['expires_at'])}\n"
        f"• {warranty_line}"
    )
    return rich, plain


def owner_menu_content(live_summary: dict | None = None) -> tuple[str, str]:
    stats = vps_db.get_stats()
    region_stats = vps_db.get_region_stats()
    region_html = []
    region_plain = []
    for region, rs in sorted(region_stats.items(), key=lambda kv: -kv[1]["total"]):
        region_html.append(
            f"<li>{html.escape(region_display(region))} — {rs['active']} active / "
            f"{rs['total']} total · {fmt_rp(rs['revenue'])}</li>"
        )
        region_plain.append(
            f"• {region_display(region)} — {rs['active']} active / {rs['total']} total · {fmt_rp(rs['revenue'])}"
        )

    live_line_html = ""
    live_line_plain = ""
    if live_summary:
        cnt = live_summary.get("count", 0)
        reg = live_summary.get("regions", 0)
        live_line_html = f"<li>Total EC2 live in AWS: <b>{cnt}</b> across {reg} region(s)</li>"
        live_line_plain = f"\n• Total EC2 live in AWS: {cnt} across {reg} region(s)"

    from . import accounts as accounts_store

    acct_rows = accounts_store.describe()
    if acct_rows:
        parts_html = []
        parts_plain = []
        for a in acct_rows:
            hits = (live_summary or {}).get("accounts", {}).get(a["name"], 0)
            state = "" if a["enabled"] else " (disabled)"
            parts_html.append(
                f"<li>{html.escape(a['label'])}{state} — {a['region_count']} region(s) · "
                f"{hits} live instance(s)</li>"
            )
            parts_plain.append(
                f"• {a['label']}{state} — {a['region_count']} region(s) · {hits} live instance(s)"
            )
        acct_html = (
            f"<h3>🔑 AWS Accounts ({len(acct_rows)})</h3><ul>"
            + "".join(parts_html)
            + "</ul>"
        )
        acct_plain = f"\n\nAWS Accounts ({len(acct_rows)})\n" + "\n".join(parts_plain)
    else:
        acct_html = "<h3>🔑 AWS Accounts (1)</h3><p>Using the default AWS CLI profile.</p>"
        acct_plain = "\n\nAWS Accounts (1)\nUsing the default AWS CLI profile."

    rich = (
        "<h1>🛠️ VPS Owner Panel</h1>"
        "<p>Manage every VPS instance across all regions, review sales statistics, "
        "and configure plan pricing.</p>"
        "<h3>📊 Summary</h3>"
        "<ul>"
        f"<li>Bot instances: <b>{stats['total_instances']}</b> (active: {stats['active_instances']})</li>"
        f"{live_line_html}"
        f"<li>Total revenue: <b>{fmt_rp(stats['total_revenue'])}</b></li>"
        f"<li>Warranty claims: <b>{stats['warranty_claims']}</b></li>"
        "</ul>"
        + (
            "<h3>🌍 Bot Instances by Region</h3><ul>" + "".join(region_html) + "</ul>"
            if region_html else "<p>No instances provisioned by the bot yet.</p>"
        )
        + acct_html
    )
    plain = (
        "<b>🛠️ VPS Owner Panel</b>\n\n"
        f"Bot instances: {stats['total_instances']} (active: {stats['active_instances']})"
        f"{live_line_plain}\n"
        f"Total revenue: {fmt_rp(stats['total_revenue'])}\n"
        f"Warranty claims: {stats['warranty_claims']}"
        + (
            "\n\n<b>🌍 Bot Instances by Region</b>\n" + "\n".join(region_plain)
            if region_plain else "\n\nNo instances provisioned by the bot yet."
        )
        + acct_plain
    )
    return rich, plain


def _match_key(instance_id, account) -> tuple:
    return (str(instance_id or ""), str(account or ""))


def merge_with_live(db_instances: list[dict], live: list[dict], show_terminated: bool = False) -> list[dict]:
    live_by_id = {
        _match_key(r.get("instance_id"), r.get("account")): r for r in live if r.get("instance_id")
    }
    merged = []
    seen_ids = set()

    for inst in db_instances:
        iid = str(inst.get("instance_id") or "")
        row = dict(inst)
        row["managed"] = True
        key = _match_key(iid, inst.get("account"))
        live_match = live_by_id.get(key)
        if live_match is None:
            for k, v in live_by_id.items():
                if k[0] == iid and k not in seen_ids:
                    live_match = v
                    row["account"] = v.get("account") or inst.get("account")
                    key = k
                    break
        if live_match is not None:
            row["aws_state"] = live_match.get("state")
            row["live"] = True
            if live_match.get("public_ip"):
                row["ip_address"] = live_match["public_ip"]
            seen_ids.add(key)
            seen_ids.add(_match_key(iid, None))
            seen_ids.add(_match_key(iid, live_match.get("account")))
        else:
            row["aws_state"] = None
            row["live"] = False
            seen_ids.add(_match_key(iid, None))
        merged.append(row)

    for live_row in live:
        iid = str(live_row.get("instance_id") or "")
        if not iid:
            continue
        key = _match_key(iid, live_row.get("account"))
        if key in seen_ids or _match_key(iid, None) in seen_ids:
            continue
        seen_ids.add(key)
        seen_ids.add(_match_key(iid, None))
        is_bot_tagged = str(live_row.get("managed_by") or "").strip().lower() == "kiyoshibot"
        merged.append({
            "vps_id": (live_row.get("name") or None) if is_bot_tagged else None,
            "instance_id": iid,
            "instance_type": live_row.get("instance_type"),
            "region": live_row.get("region"),
            "account": live_row.get("account"),
            "status": "orphan" if is_bot_tagged else "unmanaged",
            "aws_state": live_row.get("state"),
            "ip_address": live_row.get("public_ip") or "",
            "user_id": None,
            "name": live_row.get("name"),
            "live": True,
            "managed": False,
            "tagged_bot": is_bot_tagged,
        })

    merged.sort(
        key=lambda r: (
            not r.get("live"),
            str(r.get("region") or ""),
            str(r.get("account") or ""),
            str(r.get("instance_id") or ""),
        )
    )

    if not show_terminated:
        merged = [
            r for r in merged
            if r.get("live") or r.get("status") not in ("terminated", "replaced")
        ]
    return merged


def owner_list_content(instances: list[dict], scan_meta: dict | None = None) -> tuple[str, str]:
    scan_meta = scan_meta or {}
    if not instances:
        scanned = scan_meta.get("scanned", 0)
        elapsed = scan_meta.get("elapsed")
        note = f" Scanned {scanned} regions" + (f" in {elapsed}s" if elapsed is not None else "") + "."
        return (
            f"<h1>🖥 All Instances</h1><p>No instances found across any region.{html.escape(note)}</p>",
            f"<b>🖥 All Instances</b>\n\nNo instances found across any region.{note}",
        )

    items_html = []
    items_plain = []
    region_count: dict[str, int] = {}
    managed = 0
    unmanaged = 0

    for inst in instances:
        region = inst.get("region") or "-"
        region_count[region] = region_count.get(region, 0) + 1
        is_managed = bool(inst.get("managed"))
        if is_managed:
            managed += 1
        else:
            unmanaged += 1

        tag = "bot" if is_managed else "manual"
        aws_state = inst.get("aws_state") or inst.get("status")
        label = inst.get("vps_id").upper() if inst.get("vps_id") else (inst.get("name") or inst["instance_id"])
        owner = f"user <code>{inst['user_id']}</code>" if inst.get("user_id") else "unmanaged"

        items_html.append(
            f"<li><b>{html.escape(str(label))}</b> ({html.escape(tag)}) — {owner}, "
            f"{html.escape(inst.get('instance_type') or '-')}, {html.escape(region_display(region))}, "
            f"<b>{html.escape(str(aws_state))}</b>, IP {html.escape(inst.get('ip_address') or '-')}</li>"
        )
        items_plain.append(
            f"• {label} ({tag}) — {owner}, {inst.get('instance_type') or '-'}, "
            f"{region_display(region)}, {aws_state}, {inst.get('ip_address') or '-'}"
        )

    summary_html = "".join(
        f"<li>{html.escape(region_display(r))}: <b>{c}</b></li>"
        for r, c in sorted(region_count.items(), key=lambda kv: -kv[1])
    )
    summary_plain = "\n".join(
        f"• {region_display(r)}: {c}" for r, c in sorted(region_count.items(), key=lambda kv: -kv[1])
    )

    scanned = scan_meta.get("scanned")
    elapsed = scan_meta.get("elapsed")
    scan_line = ""
    scan_line_plain = ""
    if scanned:
        scan_line = f" Scanned {scanned} regions" + (f" in {elapsed}s" if elapsed is not None else "") + "."
        scan_line_plain = f" Scanned {scanned} regions" + (f" in {elapsed}s" if elapsed is not None else "") + "."

    rich = (
        "<h1>🖥 All Instances</h1>"
        f"<p>Total <b>{len(instances)}</b> instances across <b>{len(region_count)}</b> region(s). "
        f"<b>{managed}</b> from the bot, <b>{unmanaged}</b> unmanaged.{html.escape(scan_line)}</p>"
        "<h3>🌍 Regions</h3>"
        "<ul>" + summary_html + "</ul>"
        "<h3>Instances</h3>"
        "<ul>" + "".join(items_html) + "</ul>"
    )
    plain = (
        f"<b>🖥 All Instances</b>\n\n"
        f"Total {len(instances)} across {len(region_count)} region(s). "
        f"{managed} from the bot, {unmanaged} unmanaged.{scan_line_plain}\n\n"
        "<b>🌍 Regions</b>\n" + summary_plain + "\n\n"
        "<b>Instances</b>\n" + "\n".join(items_plain)
    )
    return rich, plain


def _account_label(inst: dict) -> str:
    from . import accounts as accounts_store

    raw = inst.get("account")
    if not raw or raw == "default":
        enabled = accounts_store.base_account_enabled()
        return "default (aws credentials)" if enabled else "default (disabled)"
    label = accounts_store.account_label(raw)
    if label and label != raw:
        return f"{label} ({raw})"
    return str(label or raw)


def owner_info_content(inst: dict) -> tuple[str, str]:
    t = get_tier(inst["tier"])
    o = get_os(inst["os_image"])
    tier_label = t["label"] if t else normalize_tier(inst["tier"])
    os_label = o["label"] if o else inst["os_image"]
    region = inst.get("region") or "-"
    spec_bits = []
    if t:
        if t.get("vcpu"):
            spec_bits.append(f"{t['vcpu']} vCPU")
        if t.get("ram_gb"):
            spec_bits.append(f"{t['ram_gb']} GB RAM")
        if t.get("disk_gb"):
            spec_bits.append(f"{t['disk_gb']} GB disk")
    if not spec_bits:
        live = getattr(inst, "get", lambda k, d=None: d)("live_specs") or {}
        if live.get("vcpu"):
            spec_bits.append(f"{live['vcpu']} vCPU")
        if live.get("memory_gib"):
            spec_bits.append(f"{live['memory_gib']} GB RAM")
    spec_line = " / ".join(spec_bits) or "-"
    rich = (
        f"<h1>🖥 {html.escape(inst['vps_id'].upper())}</h1>"
        "<h3>Instance</h3>"
        "<ul>"
        f"<li>AWS ID: <code>{html.escape(inst['instance_id'])}</code></li>"
        f"<li>Type: {html.escape(inst['instance_type'])}</li>"
        f"<li>Region: {html.escape(region_display(region))} "
        f"(<code>{html.escape(region)}</code>)</li>"
        f"<li>Account: <b>{html.escape(_account_label(inst))}</b></li>"
        f"<li>Status: <b>{html.escape(inst['status'])}</b></li>"
        "</ul>"
        "<h3>Owner</h3>"
        "<ul>"
        f"<li>User ID: <code>{inst['user_id']}</code></li>"
        f"<li>Plan: {html.escape(tier_label)} / {inst['days']} days</li>"
        f"<li>Specs: <b>{html.escape(spec_line)}</b></li>"
        f"<li>Price: {fmt_rp(inst['price'])}</li>"
        "</ul>"
        "<h3>Access</h3>"
        "<ul>"
        f"<li>IP: <code>{html.escape(inst['ip_address'] or '-')}</code></li>"
        f"<li>Password: <code>{html.escape(inst['root_password'] or '-')}</code></li>"
        f"<li>OS: {html.escape(os_label)}</li>"
        "</ul>"
        "<h3>Timestamps</h3>"
        "<ul>"
        f"<li>Created: {_fmt_dt(inst['created_at'])}</li>"
        f"<li>Expires: {_fmt_dt(inst['expires_at'])}</li>"
        f"<li>Warranty: {_fmt_dt(inst['warranty_until'])}</li>"
        "</ul>"
    )
    plain = (
        f"<b>🖥 {inst['vps_id'].upper()}</b>\n\n"
        f"AWS ID: <code>{inst['instance_id']}</code>\n"
        f"Type: {inst['instance_type']}\n"
        f"Region: {region_display(region)} ({region})\n"
        f"Account: {_account_label(inst)}\n"
        f"Status: {inst['status']}\n"
        f"User: <code>{inst['user_id']}</code>\n"
        f"Plan: {tier_label} / {inst['days']} days\n"
        f"Specs: {spec_line}\n"
        f"Price: {fmt_rp(inst['price'])}\n"
        f"IP: <code>{inst['ip_address'] or '-'}</code>\n"
        f"Password: <code>{inst['root_password'] or '-'}</code>\n"
        f"OS: {os_label}\n"
        f"Created: {_fmt_dt(inst['created_at'])}\n"
        f"Expires: {_fmt_dt(inst['expires_at'])}\n"
        f"Warranty: {_fmt_dt(inst['warranty_until'])}"
    )
    return rich, plain


def _fmt_ts(value) -> str:
    if value in (None, "", "-"):
        return "-"
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif hasattr(value, "tzinfo"):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Asia/Jakarta")).strftime("%d %b %Y %H:%M WIB")


def _storage_line(info: dict) -> str:
    disks = info.get("instance_storage") or []
    total_gib = sum(
        (d.get("SizeInGB") or 0) * (d.get("Count") or 1) for d in disks
    )
    kind = disks[0].get("Type") if disks else None
    if total_gib:
        return f"{total_gib} GB instance storage ({kind})"
    vols = info.get("volumes") or []
    if vols:
        v = vols[0]
        bits = [f"{v.get('size_gb')} GB"]
        if v.get("type"):
            bits.append(str(v["type"]))
        if v.get("iops"):
            bits.append(f"{v['iops']} IOPS")
        if v.get("throughput"):
            bits.append(f"{v['throughput']} MB/s")
        if v.get("encrypted"):
            bits.append("encrypted")
        return " · ".join(bits)
    root = info.get("root_device") or "ebs"
    return "EBS" if root == "ebs" else "EBS"


GRANT_STATE: dict[tuple[int, int], dict] = {}


def purge_grant_state(max_age: float = 3600.0, max_items: int = 50) -> None:
    now = time.time()
    for key, value in list(GRANT_STATE.items()):
        if now - float(value.get("touched", value.get("created_at", 0))) > max_age:
            GRANT_STATE.pop(key, None)
    while len(GRANT_STATE) > max_items:
        oldest = min(GRANT_STATE, key=lambda k: GRANT_STATE[k].get("touched", 0))
        GRANT_STATE.pop(oldest, None)


def grant_closed_content() -> tuple[str, str]:
    rich = (
        "<h1>🎁 Grant VPS</h1>"
        "<p>This grant screen was closed or replaced. Send "
        "<code>/vpsgrant &lt;user&gt;</code> to start a new one.</p>"
    )
    plain = "<b>🎁 Grant VPS</b>\n\nThis grant screen was closed. Send /vpsgrant &lt;user&gt; to start again."
    return rich, plain


def grant_tier_content(username: str, target_id: int) -> tuple[str, str]:
    rows_html = "".join(
        f"<li><b>{html.escape(t['label'])}</b> — {t['vcpu']} vCPU, {t['ram_gb']} GB RAM, "
        f"{t['disk_gb']} GB SSD</li>"
        for t in TIERS
    )
    rich = (
        "<h1>🎁 Grant VPS — Plan</h1>"
        f"<p>Recipient <b>@{html.escape(username)}</b> (<code>{target_id}</code>). "
        "Free server, no invoice.</p>"
        f"<ul>{rows_html}</ul>"
    )
    plain = (
        f"<b>🎁 Grant VPS — Plan</b>\n\n"
        f"Recipient: @{username} ({target_id})\n\n"
        + "\n".join(
            f"• {t['label']} — {t['vcpu']} vCPU, {t['ram_gb']} GB RAM, {t['disk_gb']} GB SSD"
            for t in TIERS
        )
        + "\n\nPick a plan."
    )
    return rich, plain


def grant_tier_keyboard(target_id: int) -> InlineKeyboardMarkup:
    rows = []
    for t in TIERS:
        rows.append([
            InlineKeyboardButton(
                f"{t['label']} / {t['vcpu']} vCPU • {t['ram_gb']} GB RAM",
                callback_data=owner_cb("gtier", target_id, t["id"]),
            )
        ])
    rows.append([InlineKeyboardButton("Cancel", callback_data=owner_cb("gcancel"))])
    return InlineKeyboardMarkup(rows)


def grant_days_content(target_id: int, tier_id: str) -> tuple[str, str]:
    t = get_tier(tier_id) or {"label": tier_id}
    rich = (
        "<h1>🎁 Grant VPS — Duration</h1>"
        f"<p>Plan <b>{html.escape(t['label'])}</b> for <code>{target_id}</code>. "
        "Duration only sets the expiry date; the price stays free.</p>"
    )
    plain = (
        f"<b>🎁 Grant VPS — Duration</b>\n\n"
        f"Plan: {t['label']} for {target_id}\n\nPick how long the server runs."
    )
    return rich, plain


def grant_days_keyboard(target_id: int, tier_id: str) -> InlineKeyboardMarkup:
    rows = []
    for d in DURATIONS:
        days = d["days"]
        rows.append([
            InlineKeyboardButton(
                f"{days} Days",
                callback_data=owner_cb("gdays", target_id, tier_id, days),
            )
        ])
    rows.append([InlineKeyboardButton("Back", callback_data=owner_cb("gtier", target_id))])
    rows.append([InlineKeyboardButton("Cancel", callback_data=owner_cb("gcancel"))])
    return InlineKeyboardMarkup(rows)


def grant_region_content(target_id: int, tier_id: str, days: int, page: int, regions: list[str]) -> tuple[str, str]:
    ordered = sort_regions(regions)
    pages = max(1, (len(ordered) + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    t = get_tier(tier_id) or {"label": tier_id}
    rich = (
        "<h1>🌍 Grant VPS — Region</h1>"
        f"<p><b>{html.escape(t['label'])}</b> for <b>{days} days</b>, recipient "
        f"<code>{target_id}</code>. Page {page + 1} of {pages}.</p>"
        "<aside>Regions are listed from your AWS accounts. The bot tries every account "
        "that serves the chosen region.</aside>"
    )
    plain = (
        f"<b>🌍 Grant VPS — Region</b>\n\n"
        f"{t['label']} for {days} days, recipient {target_id}"
        f" — page {page + 1} of {pages}\n\nPick a region."
    )
    return rich, plain


def grant_region_keyboard(target_id: int, tier_id: str, days: int, page: int, regions: list[str]) -> InlineKeyboardMarkup:
    ordered = sort_regions(regions)
    pages = max(1, (len(ordered) + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = ordered[page * REGIONS_PER_PAGE:page * REGIONS_PER_PAGE + REGIONS_PER_PAGE]
    rows = []
    for r in chunk:
        rows.append([
            InlineKeyboardButton(
                region_display(r),
                callback_data=owner_cb("gos", target_id, tier_id, days, r, page),
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "Prev", callback_data=owner_cb("gregion", target_id, tier_id, days, page - 1)
        ))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=owner_cb("noop")))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            "Next", callback_data=owner_cb("gregion", target_id, tier_id, days, page + 1)
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Back", callback_data=owner_cb("gdays", target_id, tier_id))])
    rows.append([InlineKeyboardButton("Cancel", callback_data=owner_cb("gcancel"))])
    return InlineKeyboardMarkup(rows)


def grant_os_content(target_id: int, tier_id: str, days: int, region: str, os_list: list[dict] | None = None) -> tuple[str, str]:
    t = get_tier(tier_id) or {"label": tier_id}
    items = os_list if os_list is not None else OS_IMAGES
    rich = (
        "<h1>💿 Grant VPS — Operating System</h1>"
        f"<p><b>{html.escape(t['label'])}</b>, {days} days, "
        f"{html.escape(region_display(region))}, recipient <code>{target_id}</code>.</p>"
        f"<p>{len(items)} image(s) available in this region.</p>"
        "<aside>Pick the last option to start the server right away with this plan, "
        "duration and region already filled in.</aside>"
    )
    plain = (
        f"<b>💿 Grant VPS — Operating System</b>\n\n"
        f"{t['label']}, {days} days, {region_display(region)}, recipient {target_id}\n\n"
        f"{len(items)} image(s) available. Pick one to deploy."
    )
    return rich, plain


def grant_os_keyboard(
    target_id: int,
    tier_id: str,
    days: int,
    region: str,
    os_list: list[dict] | None = None,
    page: int = 0,
) -> InlineKeyboardMarkup:
    items = os_list if os_list is not None else OS_IMAGES
    per = 10
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = items[page * per:page * per + per]
    rows = []
    for o in chunk:
        rows.append([
            InlineKeyboardButton(
                o["label"],
                callback_data=owner_cb("ggo", target_id, tier_id, days, region, o["id"]),
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "Prev", callback_data=owner_cb("gos", target_id, tier_id, days, region, page - 1)
        ))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=owner_cb("noop")))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            "Next", callback_data=owner_cb("gos", target_id, tier_id, days, region, page + 1)
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        "Back", callback_data=owner_cb("gregion", target_id, tier_id, days, 0)
    )])
    rows.append([InlineKeyboardButton("Cancel", callback_data=owner_cb("gcancel"))])
    return InlineKeyboardMarkup(rows)


def grant_confirm_content(target_id: int, tier_id: str, days: int, region: str, os_id: str) -> tuple[str, str]:
    t = get_tier(tier_id) or {"label": tier_id}
    o = get_os(os_id) or {"label": os_id}
    rich = (
        "<h1>🎁 Grant VPS — Confirm</h1>"
        "<table compact>"
        f"<tr><td>Recipient</td><td><code>{target_id}</code></td></tr>"
        f"<tr><td>Plan</td><td>{html.escape(t['label'])}</td></tr>"
        f"<tr><td>Specs</td><td>{t['vcpu']} vCPU, {t['ram_gb']} GB RAM, {t['disk_gb']} GB SSD</td></tr>"
        f"<tr><td>Duration</td><td>{days} days</td></tr>"
        f"<tr><td>Region</td><td>{html.escape(region_display(region))}</td></tr>"
        f"<tr><td>OS</td><td>{html.escape(o['label'])}</td></tr>"
        f"<tr><td>Price</td><td>Free</td></tr>"
        "</table>"
        "<aside>Deploying now. Credentials go to the recipient in private, and you get "
        "the full server notice here.</aside>"
    )
    plain = (
        "<b>🎁 Grant VPS — Confirm</b>\n\n"
        f"Recipient: {target_id}\n"
        f"Plan: {t['label']}\n"
        f"Specs: {t['vcpu']} vCPU, {t['ram_gb']} GB RAM, {t['disk_gb']} GB SSD\n"
        f"Duration: {days} days\n"
        f"Region: {region_display(region)}\n"
        f"OS: {o['label']}\n"
        f"Price: Free"
    )
    return rich, plain


def grant_confirm_keyboard(target_id: int, tier_id: str, days: int, region: str, os_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Deploy Now",
            callback_data=owner_cb("ggrant", target_id, tier_id, days, region, os_id),
        )],
        [InlineKeyboardButton(
            "Back", callback_data=owner_cb("gos", target_id, tier_id, days, region, 0)
        )],
        [InlineKeyboardButton("Cancel", callback_data=owner_cb("gcancel"))],
    ])


def grant_progress_content(target_id: int, tier_id: str, days: int, region: str, os_id: str) -> tuple[str, str]:
    t = get_tier(tier_id) or {"label": tier_id}
    o = get_os(os_id) or {"label": os_id}
    rich = (
        "<h1>⚙️ Deploying VPS...</h1>"
        f"<p>{html.escape(t['label'])}, {html.escape(o['label'])}, "
        f"{html.escape(region_display(region))}, {days} days for <code>{target_id}</code>.</p>"
        "<p>Waiting for the server to boot and open SSH.</p>"
    )
    plain = (
        "<b>⚙️ Deploying VPS...</b>\n\n"
        f"{t['label']}, {o['label']}, {region_display(region)}, {days} days for {target_id}.\n"
        "Waiting for the server to boot and open SSH."
    )
    return rich, plain


def grant_result_content(inst: dict, delivered: bool) -> tuple[str, str]:
    t = get_tier(inst["tier"]) or {"label": inst["tier"]}
    o = get_os(inst["os_image"]) or {"label": inst["os_image"]}
    hint = (
        "Credentials were sent to the recipient in private."
        if delivered
        else "Could not message the recipient. Deliver the credentials yourself."
    )
    rich = (
        "<h1>✅ Grant Complete</h1>"
        "<table compact>"
        f"<tr><td>Server</td><td><code>{html.escape(str(inst['vps_id']))}</code></td></tr>"
        f"<tr><td>AWS ID</td><td><code>{html.escape(str(inst['instance_id']))}</code></td></tr>"
        f"<tr><td>Plan</td><td>{html.escape(t['label'])}</td></tr>"
        f"<tr><td>Specs</td><td>{t['vcpu']} vCPU, {t['ram_gb']} GB RAM, {t['disk_gb']} GB SSD</td></tr>"
        f"<tr><td>OS</td><td>{html.escape(o['label'])}</td></tr>"
        f"<tr><td>Region</td><td>{html.escape(region_display(inst.get('region') or '-'))}</td></tr>"
        f"<tr><td>Account</td><td>{html.escape(_account_label(inst))}</td></tr>"
        f"<tr><td>IP</td><td><code>{html.escape(str(inst.get('ip_address') or '-'))}</code></td></tr>"
        f"<tr><td>Expires</td><td>{_fmt_dt(inst['expires_at'])}</td></tr>"
        "</table>"
        f"<aside>{html.escape(hint)}</aside>"
    )
    plain = (
        "<b>✅ Grant Complete</b>\n\n"
        f"Server: {inst['vps_id']}\n"
        f"AWS ID: {inst['instance_id']}\n"
        f"Plan: {t['label']}\n"
        f"Specs: {t['vcpu']} vCPU, {t['ram_gb']} GB RAM, {t['disk_gb']} GB SSD\n"
        f"OS: {o['label']}\n"
        f"Region: {region_display(inst.get('region') or '-')}\n"
        f"Account: {_account_label(inst)}\n"
        f"IP: {inst.get('ip_address') or '-'}\n"
        f"Expires: {_fmt_dt(inst['expires_at'])}\n\n"
        f"{hint}"
    )
    return rich, plain


def grant_result_keyboard(inst: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Grant Another", callback_data=owner_cb("gtier", inst["user_id"], inst["tier"])
        )],
        [InlineKeyboardButton(
            "View Server", callback_data=owner_cb("info", inst["vps_id"])
        )],
        [InlineKeyboardButton("Close", callback_data=owner_cb("close"))],
    ])


def grant_failed_content(target_id: int, detail: str) -> tuple[str, str]:
    rich = (
        "<h1>⚠️ Grant Failed</h1>"
        f"<p>Could not create a server for <code>{target_id}</code>.</p>"
        f"<pre><code>{html.escape(detail[:400])}</code></pre>"
    )
    plain = (
        "<b>⚠️ Grant Failed</b>\n\n"
        f"Could not create a server for {target_id}.\n\n{detail[:400]}"
    )
    return rich, plain


def grant_failed_keyboard(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Try Again", callback_data=owner_cb("gtier", target_id))],
        [InlineKeyboardButton("Close", callback_data=owner_cb("close"))],
    ])


def grant_recipient_content(inst: dict, granted_by: str) -> tuple[str, str]:
    t = get_tier(inst["tier"]) or {"label": inst["tier"]}
    o = get_os(inst["os_image"]) or {"label": inst["os_image"]}
    rich = (
        "<h1>🎁 You received a free VPS</h1>"
        f"<p><b>{html.escape(granted_by)}</b> gave you a server. No payment, no invoice.</p>"
        "<h3>Specifications</h3>"
        "<ul>"
        f"<li>Plan: <b>{html.escape(t['label'])}</b></li>"
        f"<li>CPU: <b>{t['vcpu']} vCPU</b></li>"
        f"<li>Memory: <b>{t['ram_gb']} GB RAM</b></li>"
        f"<li>Disk: <b>{t['disk_gb']} GB SSD</b></li>"
        f"<li>Operating System: {html.escape(o['label'])}</li>"
        f"<li>Region: {html.escape(region_display(inst.get('region') or '-'))}</li>"
        f"<li>Server ID: <code>{html.escape(str(inst['vps_id']))}</code></li>"
        "</ul>"
        "<h3>Access</h3>"
        "<ul>"
        f"<li>IP: <code>{html.escape(str(inst.get('ip_address') or '-'))}</code></li>"
        "<li>SSH port: <code>22</code></li>"
        "<li>Username: <code>root</code></li>"
        f"<li>Password: <code>{html.escape(str(inst.get('root_password') or '-'))}</code></li>"
        "</ul>"
        "<h3>Validity</h3>"
        "<ul>"
        f"<li>Active until: {_fmt_dt(inst['expires_at'])}</li>"
        f"<li>Warranty until: {_fmt_dt(inst['warranty_until'])}</li>"
        "</ul>"
        f"<pre><code>ssh root@{html.escape(str(inst.get('ip_address') or ''))}</code></pre>"
        "<aside>Open My VPS to manage the server and its firewall. To keep the credentials "
        "safe, delete this message after saving them.</aside>"
    )
    plain = (
        "<b>🎁 You received a free VPS</b>\n\n"
        f"{granted_by} gave you a server. No payment, no invoice.\n\n"
        "<b>Specifications</b>\n"
        f"• Plan: {t['label']}\n"
        f"• CPU: {t['vcpu']} vCPU\n"
        f"• Memory: {t['ram_gb']} GB RAM\n"
        f"• Disk: {t['disk_gb']} GB SSD\n"
        f"• Operating System: {o['label']}\n"
        f"• Region: {region_display(inst.get('region') or '-')}\n"
        f"• Server ID: {inst['vps_id']}\n\n"
        "<b>Access</b>\n"
        f"• IP: {inst.get('ip_address') or '-'}\n"
        "• SSH port: 22\n"
        "• Username: root\n"
        f"• Password: {inst.get('root_password') or '-'}\n\n"
        "<b>Validity</b>\n"
        f"• Active until: {_fmt_dt(inst['expires_at'])}\n"
        f"• Warranty until: {_fmt_dt(inst['warranty_until'])}\n\n"
        f"ssh root@{inst.get('ip_address') or ''}\n\n"
        "Open My VPS to manage the server and its firewall."
    )
    return rich, plain


def grant_recipient_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("My VPS", callback_data=user_cb("myvps"))],
    ])


def owner_live_info_content(
    instance_id: str,
    region: str,
    info: dict | None,
    account: str | None = None,
) -> tuple[str, str]:
    info = info or {}
    state = info.get("state") or "unknown"
    ip = info.get("public_ip") or "-"
    private_ip = info.get("private_ip") or "-"
    name = info.get("name") or "-"
    itype = info.get("instance_type") or "unknown"
    acct = _account_label({"account": account})
    vcpu = info.get("vcpu")
    mem = info.get("memory_gib")
    net = info.get("network_perf") or "-"
    arch = info.get("architecture") or "-"
    platform = info.get("platform") or "-"
    gs = info.get("security_groups") or []
    gs_html = "".join(
        f"<li><code>{html.escape(str(g.get('id')))}</code> {html.escape(str(g.get('name') or ''))}</li>"
        for g in gs
    ) or "<li>-</li>"
    gs_plain = "\n".join(
        f"  • {g.get('id')} {g.get('name') or ''}" for g in gs
    ) or "  • -"
    spec_bits = []
    if vcpu:
        spec_bits.append(f"{vcpu} vCPU")
    if mem:
        spec_bits.append(f"{mem} GB RAM")
    spec_line = " / ".join(spec_bits) or "-"

    rich = (
        f"<h1>🖥 {html.escape(str(instance_id))}</h1>"
        + (
            f"<p>Name tag <b>{html.escape(str(name))}</b>. Unmanaged instance: it runs "
            "in your AWS account but was not provisioned by the bot, so it has no "
            "customer, plan, or warranty attached.</p>"
            if name == "-"
            else f"<p>Unmanaged instance <b>{html.escape(str(name))}</b>. It runs in your "
            "AWS account but was not provisioned by the bot, so it has no customer, "
            "plan, or warranty attached.</p>"
        )
        + "<h3>Instance</h3>"
        "<ul>"
        f"<li>AWS ID: <code>{html.escape(str(instance_id))}</code></li>"
        f"<li>Name: {html.escape(str(name))}</li>"
        f"<li>Type: <b>{html.escape(str(itype))}</b></li>"
        f"<li>Specs: <b>{html.escape(spec_line)}</b></li>"
        f"<li>Storage: {html.escape(_storage_line(info))}</li>"
        f"<li>Network: {html.escape(str(net))}</li>"
        f"<li>Architecture: {html.escape(str(arch))}</li>"
        f"<li>Platform: {html.escape(str(platform))}</li>"
        f"<li>Region: {html.escape(region_display(region))} "
        f"(<code>{html.escape(region)}</code>)</li>"
        f"<li>Zone: <code>{html.escape(str(info.get('availability_zone') or '-'))}</code></li>"
        f"<li>Account: <b>{html.escape(acct)}</b></li>"
        f"<li>State: <b>{html.escape(str(state))}</b></li>"
        f"<li>Launched: {_fmt_ts(info.get('launch_time'))}</li>"
        f"<li>Image: <code>{html.escape(str(info.get('image_id') or '-'))}</code></li>"
        "</ul>"
        "<h3>Network</h3>"
        "<ul>"
        f"<li>Public IP: <code>{html.escape(str(ip))}</code></li>"
        f"<li>Private IP: <code>{html.escape(str(private_ip))}</code></li>"
        f"<li>Public DNS: <code>{html.escape(str(info.get('public_dns') or '-'))}</code></li>"
        f"<li>VPC: <code>{html.escape(str(info.get('vpc_id') or '-'))}</code></li>"
        f"<li>Subnet: <code>{html.escape(str(info.get('subnet_id') or '-'))}</code></li>"
        "</ul>"
        "<h3>Security Groups</h3>"
        f"<ul>{gs_html}</ul>"
        "<h3>Storage Volume</h3>"
        "<ul>"
        f"<li>Root: <code>{html.escape(str(info.get('root_volume') or '-'))}</code></li>"
        f"<li>EBS optimized: {'yes' if info.get('ebs_optimized') else 'no'}</li>"
        "</ul>"
    )
    plain = (
        f"<b>🖥 {instance_id}</b>\n\n"
        + (f"Name: {name}\n" if name != "-" else "")
        + "Unmanaged instance. Not provisioned by the bot.\n\n"
        f"AWS ID: <code>{instance_id}</code>\n"
        f"Type: {itype}\n"
        f"Specs: {spec_line}\n"
        f"Storage: {_storage_line(info)}\n"
        f"Network: {net}\n"
        f"Architecture: {arch}\n"
        f"Platform: {platform}\n"
        f"Region: {region_display(region)} ({region})\n"
        f"Zone: {info.get('availability_zone') or '-'}\n"
        f"Account: {acct}\n"
        f"State: {state}\n"
        f"Launched: {_fmt_ts(info.get('launch_time'))}\n"
        f"Image: <code>{info.get('image_id') or '-'}</code>\n\n"
        f"Public IP: <code>{ip}</code>\n"
        f"Private IP: <code>{private_ip}</code>\n"
        f"Public DNS: <code>{info.get('public_dns') or '-'}</code>\n"
        f"VPC: <code>{info.get('vpc_id') or '-'}</code>\n"
        f"Subnet: <code>{info.get('subnet_id') or '-'}</code>\n\n"
        "<b>Security Groups</b>\n" + gs_plain + "\n\n"
        f"Root volume: <code>{info.get('root_volume') or '-'}</code>\n"
        f"EBS optimized: {'yes' if info.get('ebs_optimized') else 'no'}"
    )
    return rich, plain


def owner_live_keyboard(
    instance_id: str,
    region: str,
    state: str,
    account: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    if str(state) in ("running", "stopped", "stopping", "pending"):
        if account:
            cb = owner_cb("liveterm", instance_id, region, account)
        else:
            cb = owner_cb("liveterm", instance_id, region)
        rows.append([InlineKeyboardButton("Terminate", callback_data=cb)])
    rows.append([InlineKeyboardButton("Back", callback_data=owner_cb("list"))])
    rows.append([InlineKeyboardButton("Close", callback_data=owner_cb("close"))])
    return InlineKeyboardMarkup(rows)


def warranty_confirm_content(inst: dict) -> tuple[str, str]:
    t = get_tier(inst["tier"])
    o = get_os(inst["os_image"])
    tier_label = t["label"] if t else normalize_tier(inst["tier"])
    os_label = o["label"] if o else inst["os_image"]
    region = inst.get("region") or "-"
    remaining_days = max(0, int((inst["expires_at"] - time.time()) // 86400))
    rich = (
        "<h1>🛡 Claim Warranty</h1>"
        f"<p>Server: <b>{html.escape(inst['vps_id'].upper())}</b></p>"
        "<h3>What will happen</h3>"
        "<ul>"
        "<li>We test your server's SSH port first. If it responds normally, the claim is refused.</li>"
        "<li>If the server is unreachable, we build a replacement server for you.</li>"
        f"<li>Your remaining <b>{remaining_days} day(s)</b> of active time is transferred.</li>"
        "<li>The old server is terminated and its data is permanently lost.</li>"
        "<li>Warranty can only be claimed once per server.</li>"
        "</ul>"
        "<h3>Replacement specification</h3>"
        "<ul>"
        f"<li>Plan: {html.escape(tier_label)} ({t['vcpu'] if t else 2} vCPU, "
        f"{t['ram_gb'] if t else '-'} GB RAM, {t['disk_gb'] if t else '-'} GB SSD)</li>"
        f"<li>Region: {html.escape(region_display(region))}</li>"
        f"<li>Operating System: {html.escape(os_label)}</li>"
        "</ul>"
        "<p><b>Only continue if your server is genuinely unreachable.</b></p>"
    )
    plain = (
        "<b>🛡 Claim Warranty</b>\n\n"
        f"Server: <b>{inst['vps_id'].upper()}</b>\n\n"
        "<b>What will happen</b>\n"
        "• We test your server's SSH port first. If it responds, the claim is refused.\n"
        "• If unreachable, we build a replacement server.\n"
        f"• Your remaining {remaining_days} day(s) are transferred.\n"
        "• The old server is terminated and its data is permanently lost.\n"
        "• Warranty can only be claimed once per server.\n\n"
        "<b>Replacement specification</b>\n"
        f"• Plan: {tier_label}\n"
        f"• Region: {region_display(region)}\n"
        f"• OS: {os_label}\n\n"
        "<b>Only continue if your server is genuinely unreachable.</b>"
    )
    return rich, plain


def warranty_progress_content(inst: dict) -> tuple[str, str]:
    rich = (
        "<h1>🔍 Testing Server</h1>"
        f"<p>Checking SSH port 22 on <code>{html.escape(inst['ip_address'] or '-')}</code>...</p>"
        "<p>This takes a few seconds. Please wait.</p>"
    )
    plain = (
        "<b>🔍 Testing Server</b>\n\n"
        f"Checking SSH port 22 on <code>{inst['ip_address'] or '-'}</code>...\n\n"
        "This takes a few seconds. Please wait."
    )
    return rich, plain


def warranty_healthy_content(inst: dict) -> tuple[str, str]:
    rich = (
        "<h1>✅ Server Responds Normally</h1>"
        "<p>Your server is reachable on SSH port 22, so the warranty claim is refused. "
        "Warranty only covers servers that stop responding.</p>"
        "<h3>Try this first</h3>"
        "<ul>"
        "<li>Reconnect with <code>ssh root@" + html.escape(inst["ip_address"] or "-") + "</code></li>"
        "<li>If you cannot log in, your password may differ from what you saved.</li>"
        "<li>If the server is slow or stuck, try <b>Restart</b> from the server menu.</li>"
        "</ul>"
        "<p>If you believe the server is genuinely broken, contact support with your server ID.</p>"
    )
    plain = (
        "<b>✅ Server Responds Normally</b>\n\n"
        "Your server is reachable on SSH port 22, so the warranty claim is refused. "
        "Warranty only covers servers that stop responding.\n\n"
        "<b>Try this first</b>\n"
        f"• Reconnect with ssh root@{inst['ip_address'] or '-'}\n"
        "• If you cannot log in, your password may differ from what you saved.\n"
        "• If the server is slow or stuck, try Restart from the server menu.\n\n"
        "If you believe the server is genuinely broken, contact support with your server ID."
    )
    return rich, plain


def warranty_building_content(inst: dict) -> tuple[str, str]:
    rich = (
        "<h1>🛠 Building Replacement</h1>"
        "<p>Your server did not respond. A replacement is being created now.</p>"
        "<p>Estimated time: <b>30–60 seconds</b>. New credentials will be sent to this chat "
        "once SSH is reachable.</p>"
    )
    plain = (
        "<b>🛠 Building Replacement</b>\n\n"
        "Your server did not respond. A replacement is being created now.\n\n"
        "Estimated time: 30–60 seconds. New credentials will be sent to this chat once SSH is reachable."
    )
    return rich, plain


def warranty_failed_content(inst: dict, error: str) -> tuple[str, str]:
    rich = (
        "<h1>⚠ Replacement Failed</h1>"
        "<p>We could not build a replacement server right now.</p>"
        f"<p><code>{html.escape(str(error)[:300])}</code></p>"
        f"<p>Your original server <b>{html.escape(str(inst.get('vps_id') or '').upper())}</b> "
        "has not been terminated. Contact support with this message.</p>"
    )
    plain = (
        "<b>⚠ Replacement Failed</b>\n\n"
        "We could not build a replacement server right now.\n\n"
        f"{str(error)[:300]}\n\n"
        f"Your original server {str(inst.get('vps_id') or '').upper()} has not been terminated. "
        "Contact support with this message."
    )
    return rich, plain


def owner_price_content() -> tuple[str, str]:
    pricing = _pricing()
    items_html = []
    items_plain = []
    for t in TIERS:
        pr = pricing.get(t["id"], {})
        items_html.append(
            f"<li>{html.escape(t['label'])} — 30d: <b>{fmt_rp(pr.get('price_30d', 0))}</b>, "
            f"14d: <b>{fmt_rp(pr.get('price_14d', 0))}</b>, "
            f"7d: <b>{fmt_rp(pr.get('price_7d', 0))}</b></li>"
        )
        items_plain.append(
            f"• {t['label']} — 30d: {fmt_rp(pr.get('price_30d', 0))}, "
            f"14d: {fmt_rp(pr.get('price_14d', 0))}, "
            f"7d: {fmt_rp(pr.get('price_7d', 0))}"
        )
    rich = (
        "<h1>🏷 Set Plan Pricing</h1>"
        "<p>Select a plan to modify its price. Changes apply immediately to all new invoices.</p>"
        "<ul>" + "".join(items_html) + "</ul>"
    )
    plain = "<b>🏷 Set Plan Pricing</b>\n\nSelect a plan to modify its price.\n\n" + "\n".join(items_plain)
    return rich, plain


def accounts_content(show_secrets: bool = False) -> tuple[str, str]:
    from . import accounts as accounts_store

    rows = accounts_store.describe()
    if not rows:
        rich = (
            "<h1>🔑 AWS Accounts</h1>"
            "<p>No extra AWS accounts configured. The bot is using the default "
            "AWS CLI profile from <code>~/.aws/credentials</code>.</p>"
            "<p>Add one with <code>/addawsaccount</code>.</p>"
        )
        plain = (
            "<b>🔑 AWS Accounts</b>\n\n"
            "No extra AWS accounts configured. Using the default AWS CLI profile.\n\n"
            "Add one with:\n/addawsaccount"
        )
        return rich, plain

    configured = [row for row in rows if not row.get("is_base")]
    base_rows = [row for row in rows if row.get("is_base")]
    total_regions = sorted({r for row in configured for r in row["regions"]})
    shown_count = len(configured) + (1 if base_rows else 0)
    rich_items = []
    plain_items = []
    for row in rows:
        cfg = accounts_store.get_account(row["name"]) or {}
        secret = cfg.get("secret_access_key") or ""
        secret_show = secret if show_secrets else accounts_store.mask_key(secret)
        regions = ", ".join(sorted(row["regions"])) or "none"
        rich_items.append(
            "<details open>"
            f"<summary>{html.escape(row['label'])} "
            f"({'enabled' if row['enabled'] else 'disabled'})</summary>"
            "<ul>"
            f"<li>Name: <code>{html.escape(row['name'])}</code></li>"
            f"<li>Access key: <code>{html.escape(row['key_hint'])}</code></li>"
            f"<li>Secret key: <code>{html.escape(secret_show)}</code></li>"
            f"<li>Priority: {row['priority']}</li>"
            f"<li>Regions ({row['region_count']}): <code>{html.escape(regions)}</code></li>"
            "</ul>"
            "</details>"
        )
        plain_items.append(
            f"<b>{html.escape(row['label'])}</b> "
            f"({'enabled' if row['enabled'] else 'disabled'})\n"
            f"Name: {row['name']}\n"
            f"Access key: <code>{row['key_hint']}</code>\n"
            f"Secret key: <code>{secret_show}</code>\n"
            f"Priority: {row['priority']}\n"
            f"Regions ({row['region_count']}): {regions}"
        )

    mode = "revealed" if show_secrets else "masked"
    rich = (
        "<h1>🔑 AWS Accounts</h1>"
        f"<p><b>{shown_count}</b> account(s) · {len(total_regions)} extra region(s) · "
        f"secrets {mode}.</p>"
        "<h3>Accounts</h3>"
        + "".join(rich_items)
        + "<aside>Regions are tried in account priority order. "
        "If the first account fails capacity for a region, the next account that "
        "serves it is used automatically.</aside>"
    )
    plain = (
        f"<b>🔑 AWS Accounts</b>\n\n"
        f"{shown_count} account(s) · {len(total_regions)} extra region(s) · secrets {mode}\n\n"
        + "\n\n".join(plain_items)
        + "\n\nRegions are tried in account priority order."
    )
    return rich, plain


def accounts_keyboard(show_secrets: bool = False) -> InlineKeyboardMarkup:
    from . import accounts as accounts_store

    base_state = "Disable" if accounts_store.base_account_enabled() else "Enable"
    rows = [
        [
            InlineKeyboardButton(
                "Hide Keys" if show_secrets else "Reveal Keys",
                callback_data="vpsacc:secret:0" if show_secrets else "vpsacc:secret:1",
            )
        ],
        [
            InlineKeyboardButton(
                f"{base_state} Default Profile",
                callback_data=f"vpsacc:toggle:{accounts_store.DEFAULT_ACCOUNT_NAME}",
            )
        ],
        [InlineKeyboardButton("Delete Account", callback_data="vpsacc:delete_menu")],
        [InlineKeyboardButton("Back", callback_data=owner_cb("menu"))],
        [InlineKeyboardButton("Close", callback_data=owner_cb("close"))],
    ]
    return InlineKeyboardMarkup(rows)


def account_detail_content(name: str, show_secrets: bool = False) -> tuple[str, str]:
    from . import accounts as accounts_store

    cfg = accounts_store.get_account(name)
    if not cfg:
        return "<h1>Account Not Found</h1>", "<b>Account Not Found</b>"

    secret = cfg.get("secret_access_key") or ""
    secret_show = secret if show_secrets else accounts_store.mask_key(secret)
    regions = ", ".join(sorted(cfg.get("regions") or [])) or "none"
    rich = (
        f"<h1>🔑 {html.escape(cfg.get('label') or name)}</h1>"
        "<ul>"
        f"<li>Name: <code>{html.escape(name)}</code></li>"
        f"<li>Access key: <code>{html.escape(cfg.get('access_key_id') or '-')}</code></li>"
        f"<li>Secret key: <code>{html.escape(secret_show)}</code></li>"
        f"<li>Priority: {cfg.get('priority', 100)}</li>"
        f"<li>Enabled: {'yes' if cfg.get('enabled', True) else 'no'}</li>"
        f"<li>Regions: <code>{html.escape(regions)}</code></li>"
        "</ul>"
    )
    plain = (
        f"<b>🔑 {cfg.get('label') or name}</b>\n\n"
        f"Name: {name}\n"
        f"Access key: <code>{cfg.get('access_key_id') or '-'}</code>\n"
        f"Secret key: <code>{secret_show}</code>\n"
        f"Priority: {cfg.get('priority', 100)}\n"
        f"Enabled: {'yes' if cfg.get('enabled', True) else 'no'}\n"
        f"Regions: {regions}"
    )
    return rich, plain


def account_detail_keyboard(name: str, show_secrets: bool = False) -> InlineKeyboardMarkup:
    from . import accounts as accounts_store

    cfg = accounts_store.get_account(name) or {}
    enabled = bool(cfg.get("enabled", True))
    rows = [
        [
            InlineKeyboardButton(
                "Hide Key" if show_secrets else "Reveal Key",
                callback_data=owner_cb("account_secret", name, "0" if show_secrets else "1"),
            )
        ],
        [
            InlineKeyboardButton(
                "Disable" if enabled else "Enable",
                callback_data=owner_cb("account_toggle", name),
            )
        ],
        [InlineKeyboardButton("Back", callback_data=owner_cb("accounts"))],
        [InlineKeyboardButton("Close", callback_data=owner_cb("close"))],
    ]
    return InlineKeyboardMarkup(rows)


PRESET_PORTS = [
    ("HTTP", 80),
    ("HTTPS", 443),
    ("MySQL", 3306),
    ("PostgreSQL", 5432),
    ("Redis", 6379),
    ("MongoDB", 27017),
    ("RDP", 3389),
]


def _rule_text(rule: dict) -> str:
    proto = str(rule.get("protocol") or rule.get("proto") or "tcp").lower()
    fr = rule.get("from_port")
    to = rule.get("to_port")
    cidr = rule.get("cidr") or "0.0.0.0/0"
    if proto in ("all", "-1"):
        return f"All traffic · {cidr}"
    if proto == "icmp":
        return f"ICMP (ping) · {cidr}"
    if fr is None:
        return f"{proto} · {cidr}"
    if fr == to:
        return f"{proto}/{fr} · {cidr}"
    return f"{proto}/{fr}-{to} · {cidr}"


def firewall_content(inst: dict, rules: dict | None = None) -> tuple[str, str]:
    inst = inst or {}
    rules = rules or {"in": [], "out": []}
    inbound = rules.get("in") or []
    outbound = rules.get("out") or []

    in_html = "".join(f"<li>{html.escape(_rule_text(r))}</li>" for r in inbound)
    out_html = "".join(f"<li>{html.escape(_rule_text(r))}</li>" for r in outbound)
    in_plain = "\n".join(f"• {_rule_text(r)}" for r in inbound) or "• none"
    out_plain = "\n".join(f"• {_rule_text(r)}" for r in outbound) or "• none"

    rich = (
        f"<h1>🛡️ Firewall — {html.escape(str(inst.get('vps_id')))}</h1>"
        f"<p>IP <code>{html.escape(str(inst.get('ip_address') or '-'))}</code> · "
        f"{html.escape(region_display(inst.get('region') or '-'))}</p>"
        "<h3>Inbound</h3><ul>" + (in_html or "<li>none</li>") + "</ul>"
        "<h3>Outbound</h3><ul>" + (out_html or "<li>none</li>") + "</ul>"
        "<aside>SSH port 22 is added automatically and cannot be removed. "
        "Changes apply immediately, no restart needed.</aside>"
    )
    plain = (
        f"<b>🛡️ Firewall — {inst.get('vps_id')}</b>\n\n"
        f"IP: <code>{inst.get('ip_address') or '-'}</code>\n"
        f"Region: {region_display(inst.get('region') or '-')}\n\n"
        f"<b>Inbound</b>\n{in_plain}\n\n"
        f"<b>Outbound</b>\n{out_plain}\n\n"
        "SSH port 22 is added automatically and cannot be removed."
    )
    return rich, plain


def firewall_keyboard(vps_id: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Add Inbound Rule", callback_data=f"fwr:{vps_id}:add:in"),
            InlineKeyboardButton("Add Outbound Rule", callback_data=f"fwr:{vps_id}:add:out"),
        ],
        [InlineKeyboardButton("Remove a Rule", callback_data=f"fwr:{vps_id}:del:menu")],
        [InlineKeyboardButton("Back", callback_data=user_cb("detail", vps_id))],
        [InlineKeyboardButton("Close", callback_data=user_cb("close"))],
    ]
    return InlineKeyboardMarkup(rows)


def firewall_pick_content(inst: dict, direction: str) -> tuple[str, str]:
    label = "Inbound" if direction == "in" else "Outbound"
    rich = (
        f"<h1>🛡️ Add {label} Rule</h1>"
        f"<p>Server <b>{html.escape(str(inst.get('vps_id')))}</b>. "
        "Pick a common port or choose Custom port.</p>"
    )
    plain = (
        f"<b>🛡️ Add {label} Rule</b>\n\n"
        f"Server: {inst.get('vps_id')}\n"
        "Pick a common port or choose Custom port."
    )
    return rich, plain


def firewall_pick_keyboard(vps_id: str, direction: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for name, port in PRESET_PORTS:
        row.append(
            InlineKeyboardButton(
                f"{name} {port}",
                callback_data=f"fwr:{vps_id}:preset:{direction}:{port}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("Custom port", callback_data=f"fwr:{vps_id}:custom:{direction}")]
    )
    rows.append(
        [InlineKeyboardButton("Custom range", callback_data=f"fwr:{vps_id}:range:{direction}")]
    )
    rows.append([InlineKeyboardButton("Back", callback_data=f"fwr:{vps_id}:menu")])
    return InlineKeyboardMarkup(rows)


def firewall_delete_keyboard(vps_id: str, rules: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for idx, r in enumerate(rules):
        direction = "IN" if r.get("direction") == "in" else "OUT"
        rows.append([
            InlineKeyboardButton(
                f"Remove {direction} {r.get('_text')}",
                callback_data=f"fwr:{vps_id}:rm:{r.get('id')}",
            )
        ])
    rows.append([InlineKeyboardButton("Back", callback_data=f"fwr:{vps_id}:menu")])
    return InlineKeyboardMarkup(rows)
