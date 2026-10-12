"""Worker pembuatan dan pengiriman PDF chapter manga ke Telegram."""
import asyncio
import logging
from io import BytesIO

from utils.http import get_http_session

from .constants import _PDF_SEM, NH_HEADERS, USER_AGENT
from .http import _escape

try:
    from PIL import Image
except ImportError:
    Image = None

log = logging.getLogger(__name__)


async def send_pdf_task(chat_id, message_thread_id, context, title, urls, referer, source="", reply_to_message_id=None):
    async with _PDF_SEM:
        await _send_pdf_task(chat_id, message_thread_id, context, title, urls, referer, source, reply_to_message_id)


async def _send_pdf_task(chat_id, message_thread_id, context, title, urls, referer, source="", reply_to_message_id=None):
    log.info(f"Starting PDF generation for '{title}' ({len(urls)} pages) from '{source}'")
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        text=f"⏳ Downloading 0/{len(urls)} pages for PDF...",
        reply_to_message_id=reply_to_message_id,
    )

    async def fetch_one(session, url):
        headers = {"User-Agent": USER_AGENT, "Referer": referer}
        if source == "nh" and NH_HEADERS:
            headers.update(NH_HEADERS)
        try:
            async with session.get(url, headers=headers, timeout=15) as response:
                if response.status == 200:
                    return await response.read()
        except Exception as e:
            log.error(f"Error fetching image for PDF '{title}': {e}")
        return None

    session = await get_http_session()
    images_bytes = []
    chunk_size = 5
    for i in range(0, len(urls), chunk_size):
        chunk_urls = urls[i:i + chunk_size]
        tasks = [fetch_one(session, u) for u in chunk_urls]
        results = await asyncio.gather(*tasks)
        images_bytes.extend([r for r in results if r])
        await asyncio.sleep(0.5)
        try:
            await status_msg.edit_text(f"⏳ Downloading {min(i + chunk_size, len(urls))}/{len(urls)} pages for PDF...")
        except Exception:
            pass

    log.info(f"Downloaded {len(images_bytes)}/{len(urls)} pages for '{title}'")

    if not images_bytes:
        log.error(f"Failed to download any images for PDF '{title}'")
        return await status_msg.edit_text("❌ Failed to download images for PDF.")

    await status_msg.edit_text(f"⏳ Generating PDF for {len(images_bytes)} pages...")
    log.info(f"Building PDF in memory for '{title}'...")

    def build_pdf(img_bytes_list):
        if not Image:
            return None
        imgs = []
        for b in img_bytes_list:
            try:
                img = Image.open(BytesIO(b)).convert("RGB")
                w, h = img.size
                if w > 1200:
                    scale = 1200.0 / w
                    resample_method = getattr(Image, "Resampling", Image).LANCZOS
                    img = img.resize((int(w * scale), int(h * scale)), resample_method)
                temp = BytesIO()
                img.save(temp, format="JPEG", quality=75)
                temp.seek(0)
                img_compressed = Image.open(temp)
                imgs.append(img_compressed)
            except Exception as e:
                log.warning(f"Failed to process a page for PDF: {e}")
        if not imgs:
            return None
        out = BytesIO()
        imgs[0].save(out, format="PDF", save_all=True, append_images=imgs[1:])
        return out.getvalue()

    pdf_bytes = await asyncio.to_thread(build_pdf, images_bytes)
    if not pdf_bytes:
        log.error(f"Failed to generate PDF for '{title}' (Pillow error or empty images)")
        return await status_msg.edit_text("❌ Failed to generate PDF. Make sure Pillow is installed.")

    pdf_size_mb = len(pdf_bytes) / 1024 / 1024
    log.info(f"PDF generated successfully for '{title}' (Size: {pdf_size_mb:.2f} MB)")
    await status_msg.edit_text(f"⏳ Uploading PDF to Telegram (Size: {pdf_size_mb:.2f} MB)...")

    pdf_io = BytesIO(pdf_bytes)
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).rstrip()
    if not safe_title:
        safe_title = "Manga_Chapter"
    pdf_io.name = f"{safe_title}.pdf"

    try:
        # PDF final WAJIB membalas ke pesan command user (reply_to_message_id),
        # bukan dikirim polos. Pesan status dihapus supaya tidak menyisakan
        # pesan ganda di chat.
        await context.bot.send_document(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            document=pdf_io,
            caption=f"📚 <b>{_escape(title)}</b>",
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )
        log.info(f"Successfully sent PDF '{title}' to chat_id={chat_id}")
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        log.error(f"Failed to send PDF '{title}': {e}")
        await status_msg.edit_text("❌ Failed to send PDF. It might be too large for Telegram API (Limit is 50MB for bots).")
