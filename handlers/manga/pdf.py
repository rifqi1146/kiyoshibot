import asyncio
import html
import logging
import os
import time
from io import BytesIO

from telegram.error import RetryAfter

from handlers.dl.progress import TransferStats, render_progress_text
from handlers.dl.utils import progress_bar
from utils.http import get_http_session

from .constants import _PDF_SEM, NH_HEADERS, USER_AGENT
from .http import _escape

try:
    from PIL import Image
except ImportError:
    Image = None

log = logging.getLogger(__name__)

PDF_EDIT_INTERVAL = float(os.getenv("MANGA_PDF_EDIT_INTERVAL", "4.0"))
PDF_DL_CONCURRENCY = max(1, int(os.getenv("MANGA_PDF_DL_CONCURRENCY", "8")))
PDF_NH_CONCURRENCY = max(1, int(os.getenv("MANGA_PDF_NH_CONCURRENCY", "6")))


async def _call_with_flood_retry(coro_factory, *, max_attempts: int = 3):
    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except RetryAfter as e:
            if attempt >= max_attempts - 1:
                raise
            wait = max(int(getattr(e, "retry_after", 1)), 1) + 1
            log.warning("Manga PDF RetryAfter | wait=%ss attempt=%d/%d", wait, attempt + 1, max_attempts)
            await asyncio.sleep(wait)


async def send_pdf_task(chat_id, message_thread_id, context, title, urls, referer, source="", reply_to_message_id=None):
    async with _PDF_SEM:
        try:
            await _send_pdf_task(chat_id, message_thread_id, context, title, urls, referer, source, reply_to_message_id)
        except Exception:
            log.exception("Unhandled error in send_pdf_task | title=%r chat_id=%s", title, chat_id)


async def _send_pdf_task(chat_id, message_thread_id, context, title, urls, referer, source="", reply_to_message_id=None):
    total_pages = len(urls or [])
    log.info(f"Starting PDF generation for '{title}' ({total_pages} pages) from '{source}'")
    if not total_pages:
        await _call_with_flood_retry(
            lambda: context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text="❌ No pages found for PDF.",
                reply_to_message_id=reply_to_message_id,
            )
        )
        return

    initial_text = render_progress_text(
        title,
        downloaded=0,
        total=0,
        extra=f"0/{total_pages} pages",
        pct=0.0,
    )
    status_msg = await _call_with_flood_retry(
        lambda: context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text=initial_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_to_message_id=reply_to_message_id,
        )
    )

    last_edit_ts = time.monotonic()
    last_text = initial_text
    flood_until = 0.0
    edit_lock = asyncio.Lock()

    async def _edit_status(text: str, *, force: bool = False, wait_on_flood: bool = False) -> None:
        nonlocal last_edit_ts, last_text, flood_until
        async with edit_lock:
            if text == last_text:
                return
            now = time.monotonic()
            if now < flood_until:
                if not wait_on_flood:
                    return
                await asyncio.sleep(flood_until - now)
                now = time.monotonic()
            if not force and (now - last_edit_ts) < PDF_EDIT_INTERVAL:
                return
            attempts = 3 if wait_on_flood else 1
            for attempt in range(attempts):
                try:
                    await status_msg.edit_text(
                        text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    last_edit_ts = time.monotonic()
                    last_text = text
                    return
                except RetryAfter as e:
                    wait = max(int(getattr(e, "retry_after", 1)), 1) + 1
                    flood_until = time.monotonic() + wait
                    log.warning("Manga PDF edit RetryAfter | chat_id=%s wait=%ss", chat_id, wait)
                    if not wait_on_flood or attempt >= attempts - 1:
                        return
                    await asyncio.sleep(wait)
                except Exception as e:
                    if "message is not modified" in str(e).lower():
                        last_text = text
                        return
                    log.debug("Manga PDF status edit failed | chat_id=%s err=%r", chat_id, e)
                    return

    session = await get_http_session()
    concurrency = PDF_NH_CONCURRENCY if source == "nh" else PDF_DL_CONCURRENCY
    sem = asyncio.Semaphore(concurrency)
    results: list[bytes | None] = [None] * total_pages
    done_pages = 0
    stats = TransferStats()
    progress_lock = asyncio.Lock()

    async def fetch_one(idx: int, url: str) -> None:
        nonlocal done_pages
        headers = {"User-Agent": USER_AGENT, "Referer": referer}
        if source == "nh" and NH_HEADERS:
            headers.update(NH_HEADERS)
        data = None
        async with sem:
            for attempt in range(2):
                try:
                    async with session.get(url, headers=headers, timeout=15) as response:
                        if response.status == 200:
                            data = await response.read()
                            break
                except Exception as e:
                    if attempt == 1:
                        log.error(f"Error fetching image for PDF '{title}': {e}")
                if attempt == 0:
                    await asyncio.sleep(0.3)
        results[idx] = data
        should_emit = False
        text_to_send = ""
        async with progress_lock:
            done_pages += 1
            new_downloaded = stats.downloaded + (len(data) if data else 0)
            if done_pages > 0 and new_downloaded > 0:
                stats.total = max(int(new_downloaded / done_pages * total_pages), new_downloaded, 1)
            now = time.monotonic()
            stats.sample(new_downloaded, now=now)
            if done_pages < total_pages and now >= flood_until and (now - last_edit_ts) >= PDF_EDIT_INTERVAL:
                pct = done_pages * 100.0 / total_pages
                text_to_send = render_progress_text(
                    title,
                    downloaded=stats.downloaded,
                    total=stats.total,
                    speed_bps=stats.speed_bps or stats.avg_bps,
                    eta_seconds=stats.eta_seconds,
                    extra=f"{done_pages}/{total_pages} pages",
                    pct=pct,
                )
                should_emit = True
        if should_emit:
            await _edit_status(text_to_send)

    await asyncio.gather(*(fetch_one(idx, u) for idx, u in enumerate(urls)))
    images_bytes = [r for r in results if r]

    log.info(f"Downloaded {len(images_bytes)}/{total_pages} pages for '{title}'")

    if not images_bytes:
        log.error(f"Failed to download any images for PDF '{title}'")
        await _edit_status("❌ Failed to download images for PDF.", force=True, wait_on_flood=True)
        return

    safe_title_html = html.escape(str(title))
    await _edit_status(
        f"<b>{safe_title_html}</b>\n"
        f"<code>{len(images_bytes)}/{total_pages} pages</code>\n\n"
        f"<code>{progress_bar(100.0)}</code>\n"
        f"<code>⏳ Generating PDF...</code>",
        force=True,
        wait_on_flood=True,
    )
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
                    resized = img.resize((int(w * scale), int(h * scale)), resample_method)
                    img.close()
                    img = resized
                temp = BytesIO()
                img.save(temp, format="JPEG", quality=75)
                img.close()
                temp.seek(0)
                img_compressed = Image.open(temp)
                imgs.append(img_compressed)
            except Exception as e:
                log.warning(f"Failed to process a page for PDF: {e}")
        if not imgs:
            return None
        out = BytesIO()
        try:
            imgs[0].save(out, format="PDF", save_all=True, append_images=imgs[1:])
            return out.getvalue()
        finally:
            for im in imgs:
                try:
                    im.close()
                except Exception:
                    pass

    pdf_bytes = await asyncio.to_thread(build_pdf, images_bytes)
    if not pdf_bytes:
        log.error(f"Failed to generate PDF for '{title}' (Pillow error or empty images)")
        await _edit_status("❌ Failed to generate PDF. Make sure Pillow is installed.", force=True, wait_on_flood=True)
        return

    pdf_size_mb = len(pdf_bytes) / 1024 / 1024
    log.info(f"PDF generated successfully for '{title}' (Size: {pdf_size_mb:.2f} MB)")
    await _edit_status(
        f"<b>{safe_title_html}</b>\n"
        f"<code>{len(images_bytes)} pages • {pdf_size_mb:.2f} MB</code>\n\n"
        f"<code>⏳ Uploading PDF to Telegram...</code>",
        force=True,
        wait_on_flood=True,
    )

    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).rstrip()
    if not safe_title:
        safe_title = "Manga_Chapter"

    try:
        async def _send_doc():
            pdf_io = BytesIO(pdf_bytes)
            pdf_io.name = f"{safe_title}.pdf"
            return await context.bot.send_document(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                document=pdf_io,
                caption=f"📚 <b>{_escape(title)}</b>",
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
            )

        await _call_with_flood_retry(_send_doc)
        log.info(f"Successfully sent PDF '{title}' to chat_id={chat_id}")
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        log.error(f"Failed to send PDF '{title}': {e}")
        await _edit_status("❌ Failed to send PDF. It might be too large for Telegram API.", force=True, wait_on_flood=True)
