"""Downloader TikTok (tiktok.com / vt.tiktok.com / douyin.com).

FLOW DOWNLOADER TIKTOK
----------------------
1. URL Matching: `is_tiktok(url)` (dari extractor).

2. Metadata (`_fetch_tiktok_metadata`, via extractor):
   - Jalur cepat `_fetch_tiktok_govd_fast` (1 request) dulu bila TIKTOK_GOVD_FAST.
   - Gagal -> `_fetch_tiktok_direct` (5x retry aiohttp + fallback Scrapling).
   - PoW challenge TikTok diselesaikan `utils._solve_tt_challenge`.

3. Download (downloader.py):
   - engine TIKTOK_DOWNLOAD_ENGINE (aria2c/aiohttp, saling fallback),
     progress TransferStats, batas 2GB.
   - Video: coba semua kandidat `video_urls` berurutan.
   - Album: paralel per gambar.
   - Slideshow: pilihan gambar vs video (ffmpeg xfade) vs audio.

4. Slideshow multi-gambar + fmt mp4 -> kirim `choice_required`
   ("tiktok_slideshow") supaya router menampilkan picker.

5. Gagal total -> fallback tikwm (`fallback._tikwm_result`).
"""
import logging

from handlers.dl.utils import FileSizeLimitExceeded

from .constants import USER_AGENT, USE_GOVD_FAST, TIKTOK_DOWNLOAD_ENGINE, TIKTOK_PROGRESS
from .utils import _write_debug_file, _send_debug_file, _safe_edit_status
from . import extractor as _ext
from . import downloader as _dl
from .fallback import _tikwm_result

log = logging.getLogger(__name__)

# Re-export: dipakai router (`from .tiktok.main import is_tiktok,tiktok_download`)
# dan fallback (`from .main import _download_with_best_engine, ...`).
is_tiktok = _ext.is_tiktok
_download_with_best_engine = _dl._download_with_best_engine
_download_album_images = _dl._download_album_images

__all__ = [
    "USER_AGENT",
    "is_tiktok",
    "tiktok_download",
    "tiktok_scrape_download",
    "douyin_download",
    "_download_with_best_engine",
    "_download_album_images",
]



async def tiktok_scrape_download(url,bot,chat_id,status_msg_id,fmt_key="mp4",metadata_ready:bool=False):
    media=await _ext._fetch_tiktok_metadata(url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,metadata_ready=metadata_ready)
    log.info("TikTok scraping metadata ready | url=%s kind=%s title=%r target=%s source=%s",url,media.get("kind"),media.get("title"),media.get("target_url"),media.get("source"))
    return await _dl._download_tiktok_media(media,bot,chat_id,status_msg_id,fmt_key=fmt_key)

async def douyin_download(url,bot,chat_id,status_msg_id):
    result=await _tikwm_result(url=url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,fmt_key="mp4")
    if result.get("items"):
        raise RuntimeError("SLIDESHOW")
    return result

async def tiktok_download(url,bot,chat_id,status_msg_id,fmt_key="mp4",metadata_ready:bool=False):
    try:
        log.info("TikTok primary start | source=scraping url=%s fmt=%s metadata_ready=%s fast=%s engine=%s progress=%s",url,fmt_key,metadata_ready,USE_GOVD_FAST,TIKTOK_DOWNLOAD_ENGINE,TIKTOK_PROGRESS)
        result=await tiktok_scrape_download(url=url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,fmt_key=fmt_key,metadata_ready=metadata_ready)
        if isinstance(result,dict):
            if result.get("path"):
                log.info("TikTok primary success | source=%s file=%s",result.get("source"),result.get("path"))
            elif result.get("items"):
                log.info("TikTok primary success | source=%s items=%s",result.get("source"),len(result.get("items") or []))
        return result
    except Exception as e:
        if isinstance(e, FileSizeLimitExceeded):
            log.warning("TikTok media exceeds limit, not falling back | url=%s err=%r", url, e)
            raise
        log.warning("TikTok scraping failed, fallback to tikwm | url=%s fmt=%s err=%r",url,fmt_key,e)
        try:
            err_path=_write_debug_file("tiktok_scrape_exception",repr(e),"txt")
            await _send_debug_file(bot,err_path,f"[TTDBG] scrape exception | {url}")
        except Exception as dbg_err:
            log.debug("Failed to send TikTok scrape exception debug | err=%r",dbg_err)
        await _safe_edit_status(bot,chat_id,status_msg_id,"<b>TikTok scraping failed</b>\n\n<i>Fallback to tikwm...</i>")
        result=await _tikwm_result(url=url,bot=bot,chat_id=chat_id,status_msg_id=status_msg_id,fmt_key=fmt_key)
        if isinstance(result,dict):
            if result.get("path"):
                log.info("TikTok fallback success | source=tikwm file=%s",result.get("path"))
            elif result.get("items"):
                log.info("TikTok fallback success | source=tikwm items=%s",len(result.get("items") or []))
        return result
