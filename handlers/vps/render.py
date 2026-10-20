import html
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import vps_db

from .constants import (
    DEFAULT_REGION,
    OS_IMAGES,
    OWNER_PREFIX,
    REGIONS_PER_PAGE,
    TIERS,
    USER_PREFIX,
    WARRANTY_DAYS,
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
                callback_data=user_cb("checkout", tier_id, str(days), region, o["id"]),
            )
        ])
    rows.append([InlineKeyboardButton("Back", callback_data=user_cb("region", tier_id, str(days), "0"))])
    rows.append([InlineKeyboardButton("Close", callback_data=user_cb("close"))])
    return InlineKeyboardMarkup(rows)


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


def instance_detail_keyboard(vps_id: str, status: str, warranty_active: bool) -> InlineKeyboardMarkup:
    rows = []
    if status == "active":
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
        tag = "" if is_managed else "[manual] "
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


def invoice_content(inv: dict, *, status: str, remaining: int) -> tuple[str, str]:
    t = get_tier(inv["tier"])
    o = get_os(inv["os_image"])
    rem_m, rem_s = divmod(max(0, remaining), 60)
    tier_label = t["label"] if t else normalize_tier(inv["tier"])
    os_label = o["label"] if o else inv["os_image"]
    region = inv.get("region") or "-"
    rich = (
        "<h1>🧾 VPS Invoice — QRIS</h1>"
        f"<p><b>Status:</b> {html.escape(status)}<br/>"
        f"<b>Plan:</b> {html.escape(tier_label)} / {inv['days']} days<br/>"
        f"<b>Region:</b> {html.escape(region_display(region))}<br/>"
        f"<b>Operating System:</b> {html.escape(os_label)}<br/>"
        f"<b>Total:</b> {fmt_rp(inv['gross_amount'])}<br/>"
        f"<b>Reference:</b> <code>{html.escape(inv['trx_id'])}</code></p>"
        "<hr/>"
        f"<p><b>Time Remaining:</b> {rem_m:02d}:{rem_s:02d}</p>"
        "<p>Scan the QR code above with any QRIS-compatible app (GoPay, OVO, DANA, "
        "ShopeePay, or mobile banking). Payment status is polled automatically every 15 seconds "
        "and provisioning starts the moment payment is confirmed.</p>"
    )
    plain = (
        "<b>🧾 VPS Invoice — QRIS</b>\n\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Plan:</b> {tier_label} / {inv['days']} days\n"
        f"<b>Region:</b> {region_display(region)}\n"
        f"<b>Operating System:</b> {os_label}\n"
        f"<b>Total:</b> {fmt_rp(inv['gross_amount'])}\n"
        f"<b>Reference:</b> <code>{html.escape(inv['trx_id'])}</code>\n\n"
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
        if live_match is None and not inst.get("account"):
            for k, v in live_by_id.items():
                if k[0] == iid:
                    live_match = v
                    row["account"] = v.get("account")
                    break
        if live_match is not None:
            row["aws_state"] = live_match.get("state")
            row["live"] = True
            if live_match.get("public_ip"):
                row["ip_address"] = live_match["public_ip"]
            seen_ids.add(key)
        else:
            row["aws_state"] = None
            row["live"] = False
        merged.append(row)

    for live_row in live:
        iid = str(live_row.get("instance_id") or "")
        if not iid:
            continue
        key = _match_key(iid, live_row.get("account"))
        if key in seen_ids:
            continue
        seen_ids.add(key)
        merged.append({
            "vps_id": None,
            "instance_id": iid,
            "instance_type": live_row.get("instance_type"),
            "region": live_row.get("region"),
            "account": live_row.get("account"),
            "status": "unmanaged",
            "aws_state": live_row.get("state"),
            "ip_address": live_row.get("public_ip") or "",
            "user_id": None,
            "name": live_row.get("name"),
            "live": True,
            "managed": False,
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


def owner_info_content(inst: dict) -> tuple[str, str]:
    t = get_tier(inst["tier"])
    o = get_os(inst["os_image"])
    tier_label = t["label"] if t else normalize_tier(inst["tier"])
    os_label = o["label"] if o else inst["os_image"]
    region = inst.get("region") or "-"
    rich = (
        f"<h1>🖥 {html.escape(inst['vps_id'].upper())}</h1>"
        "<h3>Instance</h3>"
        "<ul>"
        f"<li>AWS ID: <code>{html.escape(inst['instance_id'])}</code></li>"
        f"<li>Type: {html.escape(inst['instance_type'])}</li>"
        f"<li>Region: {html.escape(region_display(region))}</li>"
        f"<li>Status: <b>{html.escape(inst['status'])}</b></li>"
        "</ul>"
        "<h3>Owner</h3>"
        "<ul>"
        f"<li>User ID: <code>{inst['user_id']}</code></li>"
        f"<li>Plan: {html.escape(tier_label)} / {inst['days']} days</li>"
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
        f"Region: {region_display(region)}\n"
        f"Status: {inst['status']}\n"
        f"User: <code>{inst['user_id']}</code>\n"
        f"Plan: {tier_label} / {inst['days']} days\n"
        f"Price: {fmt_rp(inst['price'])}\n"
        f"IP: <code>{inst['ip_address'] or '-'}</code>\n"
        f"Password: <code>{inst['root_password'] or '-'}</code>\n"
        f"OS: {os_label}\n"
        f"Created: {_fmt_dt(inst['created_at'])}\n"
        f"Expires: {_fmt_dt(inst['expires_at'])}\n"
        f"Warranty: {_fmt_dt(inst['warranty_until'])}"
    )
    return rich, plain


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
    acct_line_rich = (
        f"<li>Account: <code>{html.escape(str(account))}</code></li>" if account else ""
    )
    acct_line_plain = f"Account: {account}\n" if account else ""
    rich = (
        f"<h1>🖥 {html.escape(instance_id)}</h1>"
        "<p>Unmanaged instance. This server is running in your AWS account but was "
        "not provisioned by the bot, so it has no customer, plan, or warranty attached.</p>"
        "<h3>Instance</h3>"
        "<ul>"
        f"<li>AWS ID: <code>{html.escape(instance_id)}</code></li>"
        f"<li>Region: {html.escape(region_display(region))}</li>"
        f"{acct_line_rich}"
        f"<li>State: <b>{html.escape(str(state))}</b></li>"
        f"<li>Type: {html.escape(str(info.get('instance_type') or 'unknown'))}</li>"
        f"<li>Launched: {html.escape(str(info.get('launch_time') or '-'))}</li>"
        "</ul>"
        "<h3>Network</h3>"
        "<ul>"
        f"<li>Public IP: <code>{html.escape(str(ip))}</code></li>"
        f"<li>Private IP: <code>{html.escape(str(private_ip))}</code></li>"
        "</ul>"
    )
    plain = (
        f"<b>🖥 {instance_id}</b>\n\n"
        "Unmanaged instance. Not provisioned by the bot.\n\n"
        f"AWS ID: <code>{instance_id}</code>\n"
        f"Region: {region_display(region)}\n"
        f"{acct_line_plain}"
        f"State: {state}\n"
        f"Type: {info.get('instance_type') or 'unknown'}\n"
        f"Public IP: <code>{ip}</code>\n"
        f"Private IP: <code>{private_ip}</code>"
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
