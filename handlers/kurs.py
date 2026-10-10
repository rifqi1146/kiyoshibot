import aiohttp
import html
from telegram import Update
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from utils.http import get_http_session
from utils.rich_msg import Report, edit_rich, send_rich

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
            # Bangun tabel 2 kolom (kode | nama) agar ringkas.
            items = sorted(data.items())
            rows = []
            for i in range(0, len(items), 2):
                left = items[i]
                right = items[i + 1] if i + 1 < len(items) else None
                l_cell = f"<code>{html.escape(left[0])}</code> — {html.escape(str(left[1]))}"
                r_cell = (
                    f"<code>{html.escape(right[0])}</code> — {html.escape(str(right[1]))}"
                    if right else ""
                )
                rows.append((l_cell, r_cell))
            rich = (
                "<h1>💱 Currency List</h1>"
                f"<p>{len(items)} currencies • European Central Bank</p>"
                "<hr/>"
                "<table bordered compact>"
                "<tr><th>Code</th><th>Code</th></tr>"
                + "".join(
                    f"<tr><td>{l}</td><td>{r}</td></tr>" for l, r in rows
                )
                + "</table>"
                "<hr/>"
                f"<footer>Source: {ECB_SOURCE_URL}</footer>"
            )
            plain = (
                "<b>💱 Currency List</b>\n\n"
                + "\n".join(f"• <b>{html.escape(k)}</b> — {html.escape(str(v))}" for k, v in items)
                + f"\n\n🌐 Source: <a href=\"{ECB_SOURCE_URL}\">European Central Bank</a>"
            )
            if len(plain) > 4096:
                return await msg.reply_text(plain[:4096], parse_mode="HTML", disable_web_page_preview=True)
            return await send_rich(msg.get_bot(), msg.chat_id, rich, plain, reply_to_message_id=msg.message_id)
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

        rep = Report("💱 Currency Exchange", f"{_fmt_num(amount)} {from_cur} ≈ {_fmt_num(rate)} {to_cur}", aside="Data via European Central Bank")
        rep.add("💱 Result", [
            ("From", f"{_fmt_num(amount)} {from_cur}"),
            ("To", f"{_fmt_num(rate)} {to_cur}"),
            ("Rate", f"1 {from_cur} = {_fmt_num(rate / amount if amount else rate)} {to_cur}"),
            ("Date", date),
        ])
        rich_html, plain_html = rep.build()
        await send_rich(msg.get_bot(), msg.chat_id, rich_html, plain_html, reply_to_message_id=msg.message_id)
    except Exception as e:
        await msg.reply_text(f"Error: {e}")
