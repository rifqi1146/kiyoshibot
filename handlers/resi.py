import html
import json
import aiohttp
import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from handlers.join import require_join_or_block
from utils.http import get_http_session
from utils.config import NEOXR_API_KEY

BASE_URL = "https://api.neoxr.eu/api"
MAX_TELEGRAM_TEXT = 3900

EXPEDISI_FALLBACK = [
    {"label": "JNE", "value": "jne"},
    {"label": "J&T", "value": "jnt"},
    {"label": "J&T Cargo", "value": "jtcargo"},
    {"label": "Sicepat", "value": "sicepat"},
    {"label": "Lion Parcel", "value": "lion"},
    {"label": "Ninja", "value": "ninja"},
    {"label": "Pos Indonesia", "value": "pos"},
    {"label": "TIKI", "value": "tiki"},
    {"label": "ID Express", "value": "idexpress"},
    {"label": "Anteraja", "value": "anteraja"},
    {"label": "Wahana", "value": "wahana"},
    {"label": "Indah Cargo", "value": "indah"},
    {"label": "Lazada Express", "value": "lex"},
    {"label": "Shopee Express", "value": "spx"},
    {"label": "Sentral Cargo", "value": "sentralcargo"},
    {"label": "Dakota Cargo", "value": "dakota"},
    {"label": "Rex Express", "value": "rex"},
    {"label": "Paxel", "value": "paxel"},
    {"label": "Kurir Rekomendasi", "value": "rekomendasi"},
    {"label": "SAP Express", "value": "sap"},
    {"label": "JDL Express", "value": "jdl"},
    {"label": "NCS Cargo", "value": "ncs"},
]

def esc(text) -> str:
    return html.escape(str(text or "-"))

def usage_text() -> str:
    return (
        "📦 <b>Tracking Check</b>\n\n"
        "Format:\n"
        "<code>/resi ekspedisi nomor_resi</code>\n\n"
        "Example:\n"
        "<code>/resi spx SPXID065715424</code>\n"
        "<code>/resi jne 0123456789</code>\n\n"
        "See supported couriers:\n"
        "<code>/resi list</code>"
    )

def split_text(text: str, limit: int = MAX_TELEGRAM_TEXT) -> list[str]:
    chunks = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            if len(line) > limit:
                for i in range(0, len(line), limit):
                    chunks.append(line[i:i + limit].rstrip())
            else:
                current = line
        else:
            current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [text[:limit]]

async def send_or_edit_long(msg, text: str):
    chunks = split_text(text)
    await msg.edit_text(chunks[0], parse_mode="HTML", disable_web_page_preview=True)
    for chunk in chunks[1:]:
        await msg.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)

async def neoxr_get(endpoint: str, params: dict, timeout: int = 20):
    if not NEOXR_API_KEY:
        return False, "NEOXR_API_KEY is not set in .env"
    params = dict(params)
    params["apikey"] = NEOXR_API_KEY
    session = await get_http_session()
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            raw = await resp.text()
            if resp.status != 200:
                return False, f"HTTP {resp.status}: {raw[:500]}"
            try:
                return True, json.loads(raw)
            except Exception:
                return False, f"Invalid JSON: {raw[:500]}"
    except asyncio.TimeoutError:
        return False, "Request timeout, API is slow / busy."
    except Exception as e:
        return False, str(e)

async def fetch_expedisi():
    ok, data = await neoxr_get("cekresi-expedisi", {})
    if not ok:
        return False, EXPEDISI_FALLBACK, data
    if not isinstance(data, dict) or not data.get("status"):
        return False, EXPEDISI_FALLBACK, data.get("message") or data.get("msg") or "Failed to fetch courier list."
    items = data.get("data")
    if not isinstance(items, list) or not items:
        return False, EXPEDISI_FALLBACK, "Courier data is empty from API."
    return True, items, None

def format_expedisi_list(items: list[dict], fallback_reason=None) -> str:
    lines = ["📦 <b>Courier Tracking List</b>\n"]
    for item in items:
        label = item.get("label")
        value = item.get("value")
        if label and value:
            lines.append(f"• <b>{esc(label)}</b> — <code>{esc(value)}</code>")
    lines.append("\nExample:")
    lines.append("<code>/resi spx SPXID06575424</code>")
    if fallback_reason:
        lines.append(f"\n⚠️ API list failed.\n<code>{esc(fallback_reason)}</code>")
    return "\n".join(lines)

def format_resi(data: dict) -> str:
    state = data.get("state")
    courier = data.get("courier")
    awb = data.get("awb")
    shipment_at = data.get("shipment_at")
    history = data.get("history") or []

    lines = [
        "📦 <b>Tracking Result</b>",
        "",
        f"🚚 Courier: <b>{esc(courier)}</b>",
        f"🧾 AWB: <code>{esc(awb)}</code>",
        f"📌 Status: <b>{esc(state)}</b>",
    ]

    if shipment_at:
        lines.append(f"🕒 Shipment At: <code>{esc(shipment_at)}</code>")

    lines.append("")
    lines.append("📍 <b>Shipping History</b>")

    if not history:
        lines.append("No shipping history yet.")
        return "\n".join(lines)

    for i, item in enumerate(history, start=1):
        time = item.get("time")
        position = item.get("position")
        desc = item.get("description")
        lines.append("")
        lines.append(f"<b>{i}.</b> <code>{esc(time)}</code>")
        lines.append(f"   <b>{esc(position)}</b>")
        lines.append(f"   {esc(desc)}")

    return "\n".join(lines)

async def resi_list_cmd(msg):
    status = await msg.reply_text("📦 Fetching courier list...")
    ok, items, reason = await fetch_expedisi()
    text = format_expedisi_list(items, None if ok else reason)
    await send_or_edit_long(status, text)

async def resi_check_cmd(msg, ekspedisi: str, nomor_resi: str):
    status = await msg.reply_text(
        f"🔎 Checking <code>{esc(nomor_resi)}</code> via <b>{esc(ekspedisi)}</b>...",
        parse_mode="HTML"
    )

    ok, payload = await neoxr_get("cekresi", {
        "resi": nomor_resi,
        "ekspedisi": ekspedisi
    })

    if not ok:
        return await status.edit_text(
            f"Failed to check tracking number.\n\n<code>{esc(payload)}</code>",
            parse_mode="HTML"
        )

    if not isinstance(payload, dict):
        return await status.edit_text("Invalid API response.")

    if not payload.get("status"):
        err = payload.get("message") or payload.get("msg") or payload.get("data") or "Tracking number not found / wrong courier."
        return await status.edit_text(
            f"Failed to check tracking number.\n\n<code>{esc(err)}</code>",
            parse_mode="HTML"
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        return await status.edit_text("Tracking data is empty.")

    text = format_resi(data)
    await send_or_edit_long(status, text)

async def resi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return

    msg = update.message
    if not msg:
        return

    args = context.args or []

    if not args:
        return await msg.reply_text(usage_text(), parse_mode="HTML")

    sub = args[0].lower().strip()

    if sub in ("help", "-h", "--help"):
        return await msg.reply_text(usage_text(), parse_mode="HTML")

    if sub == "list":
        return await resi_list_cmd(msg)

    if len(args) < 2:
        return await msg.reply_text(usage_text(), parse_mode="HTML")

    ekspedisi = sub
    nomor_resi = "".join(args[1:]).strip()

    if not nomor_resi:
        return await msg.reply_text(usage_text(), parse_mode="HTML")

    await resi_check_cmd(msg, ekspedisi, nomor_resi)