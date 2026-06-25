import aiohttp
from telegram import Update
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from utils.http import get_http_session

ECB_SOURCE_URL = "https://data.ecb.europa.eu/currency-converter"

def _clean_amount(text: str) -> float:
    text = text.replace(",", "")
    if text.count(".") > 1:
        text = text.replace(".", "")
    elif text.count(".") == 1:
        parts = text.split(".")
        if len(parts[1]) == 3:
            text = text.replace(".", "")
            
    return float(text)

def _fmt_num(val: float) -> str:
    if val == int(val):
        return f"{int(val):,}".replace(",", ".")
    s = f"{val:,.2f}"
    main, dec = s.split(".")
    return main.replace(",", ".") + "," + dec

async def kurs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update, context):
        return
    msg = update.message
    if not msg:
        return
    args = context.args
    if args and args[0].lower() == "list":
        try:
            session = await get_http_session()
            async with session.get(
                "https://api.frankfurter.app/currencies",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return await msg.reply_text("Failed to fetch currency list.")
                data = await r.json()
            lines = ["💱 <b>Currency List</b>\n"]
            for code, name in sorted(data.items()):
                lines.append(f"• <b>{code}</b> — {name}")
            lines.append(
                "\n🌐 Data source: "
                f"<a href=\"{ECB_SOURCE_URL}\">European Central Bank</a>"
            )
            return await msg.reply_text(
                "\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            return await msg.reply_text(f"Error: {e}")
            
    if len(args) < 2:
        return await msg.reply_text(
            "💱 <b>Currency Exchange</b>\n\n"
            "Format:\n"
            "<code>/kurs [amount] FROM TO</code>\n\n"
            "Example:\n"
            "<code>/kurs USD IDR</code>\n"
            "<code>/kurs 10 USD IDR</code>\n"
            "<code>/kurs list</code>",
            parse_mode="HTML"
        )
        
    try:
        if len(args) == 2:
            amount = 1.0
            from_cur = args[0].upper()
            to_cur = args[1].upper()
        else:
            amount = _clean_amount(args[0])
            from_cur = args[1].upper()
            to_cur = args[2].upper()
    except Exception:
        return await msg.reply_text("Invalid format.")
        
    try:
        session = await get_http_session()
        async with session.get(
            "https://api.frankfurter.app/latest",
            params={
                "from": from_cur,
                "to": to_cur,
                "amount": amount
            },
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return await msg.reply_text("Failed to fetch exchange rate data.")
            data = await r.json()
            
        rate = data["rates"].get(to_cur)
        date = data.get("date")
        if rate is None:
            return await msg.reply_text("Invalid currency code.")
            
        await msg.reply_text(
            "💱 <b>Currency Exchange</b>\n\n"
            f"{_fmt_num(amount)} <b>{from_cur}</b> ≈ <b>{_fmt_num(rate)} {to_cur}</b>\n\n"
            f"📅 Date: <code>{date}</code>\n"
            "🌐 Source: "
            f"<a href=\"{ECB_SOURCE_URL}\">European Central Bank</a>",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        await msg.reply_text(f"Error: {e}")
