import os
import uuid
import shutil
import logging
import asyncio

from handlers.dl.constants import TMP_DIR
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded
from .constants import RETROTUBE_DOMAINS, _PREFERRED_HOSTS, DEBUG_RETROTUBE, is_retrotube_domain
from .extractor import _scrape_post, _mirror_urls, _resolve_embed, _host
from .download import (
    _fetch_segments,
    _download_segments,
    _concat,
    _extract_audio,
    _download_direct,
    _download_gdrive,
    _download_thumb,
    _safe_edit_status,
)

log = logging.getLogger(__name__)


def _dbg(msg, *args):
    if DEBUG_RETROTUBE:
        log.warning("RTDBG | " + msg, *args)


def is_retrotube_url(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    return is_retrotube_domain(host)


async def retrotube_download(
    raw_url,
    fmt_key,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
):
    del format_id, has_audio, known_size
    work_dir = os.path.join(TMP_DIR, f"rt_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    final_path = None
    try:
        if not metadata_ready:
            await _safe_edit_status(bot, chat_id, status_msg_id, "<b>Scraping website...</b>")

        title = None
        page_thumb = None
        candidate_pairs = []
        seen_cands = set()

        def _has_preferred(pairs):
            return any(
                any(_host(p[0]) == h or _host(p[0]).endswith("." + h) for h in _PREFERRED_HOSTS)
                for p in pairs
            )

        for idx, page_url in enumerate([raw_url] + _mirror_urls(raw_url)):
            if idx > 0 and _has_preferred(candidate_pairs):
                break
            try:
                t, thumb, cands = await asyncio.to_thread(_scrape_post, page_url)
            except Exception as e:
                _dbg("scrape gagal | %s %r", page_url, e)
                continue
            if idx == 0 or title is None:
                title = t
            if thumb and not page_thumb:
                page_thumb = thumb
            for c in cands:
                if c not in seen_cands:
                    seen_cands.add(c)
                    candidate_pairs.append((c, page_url))

        if not candidate_pairs:
            raise RuntimeError("URL video (embed) tidak ditemukan di halaman")

        pref = [p for p in candidate_pairs if any(_host(p[0]) == h or _host(p[0]).endswith("." + h) for h in _PREFERRED_HOSTS)]
        rest = [p for p in candidate_pairs if p not in pref]
        candidate_pairs = pref + rest

        title = sanitize_filename(title or "Video", 100)

        # Loop setiap kandidat embed sampai berhasil dapat stream valid (non-decoy)
        downloaded = False
        last_error = None
        
        # Tambahkan kandidat fallback untuk mencoba mode tanpa gdrive bypass jika gdrive gagal limit kuota
        expanded_candidates = []
        for cand, refers in candidate_pairs:
            expanded_candidates.append((cand, refers, True))  # coba bypass gdrive
            if "db.fbplay.vip" in cand:
                expanded_candidates.append((cand, refers, False))  # fallback ke hls tiktokcdn
                
        for cand, refers, prefer_gdrive in expanded_candidates:
            try:
                k, mu, th = await asyncio.to_thread(_resolve_embed, cand, refers, prefer_gdrive)
                if not mu:
                    continue

                if k == "gdrive":
                    raw_file = os.path.join(work_dir, "direct.mp4")
                    await asyncio.to_thread(_download_gdrive, mu, raw_file)
                    if fmt_key == "mp3":
                        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp3")
                        await asyncio.to_thread(_extract_audio, raw_file, final_path)
                    else:
                        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp4")
                        shutil.move(raw_file, final_path)
                elif k == "hls":
                    segments = await asyncio.to_thread(_fetch_segments, mu, cand)
                    if segments and segments[0] == "__AES_HLS__":
                        # HLS AES fallback path
                        from handlers.dl.remux import download_hls_aes_ffmpeg
                        from handlers.dl.retrotube.constants import UA
                        variant_url = segments[1]
                        if fmt_key == "mp3":
                            final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp3")
                            await download_hls_aes_ffmpeg(variant_url, cand, final_path, user_agent=UA, audio_only=True)
                        else:
                            final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp4")
                            await download_hls_aes_ffmpeg(variant_url, cand, final_path, user_agent=UA, audio_only=False)
                    else:
                        seg_files = await _download_segments(segments, cand, work_dir, bot, chat_id, status_msg_id, title)
                        if fmt_key == "mp3":
                            final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp3")
                            await asyncio.to_thread(_concat, seg_files, final_path, work_dir, True)
                        else:
                            final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp4")
                            await asyncio.to_thread(_concat, seg_files, final_path, work_dir, False)
                else:
                    raw_file = os.path.join(work_dir, "direct.mp4")
                    await asyncio.to_thread(_download_direct, mu, cand, raw_file)
                    if fmt_key == "mp3":
                        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp3")
                        await asyncio.to_thread(_extract_audio, raw_file, final_path)
                    else:
                        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retrotube.mp4")
                        shutil.move(raw_file, final_path)

                downloaded = True
                kind = k
                embed_thumb = th
                break
            except FileSizeLimitExceeded:
                raise
            except Exception as e:
                _dbg("kandidat %s gagal (prefer_gdrive=%s): %r", cand, prefer_gdrive, e)
                last_error = e
                # Bersihkan file kerja sebelum mencoba kandidat berikutnya
                for item in os.listdir(work_dir):
                    item_path = os.path.join(work_dir, item)
                    try:
                        if os.path.isfile(item_path):
                            os.remove(item_path)
                    except OSError:
                        pass
                continue

        if not downloaded:
            raise RuntimeError(f"Tidak ada sumber video yang bisa diunduh ({last_error or 'semua kandidat gagal'})")

        result = {"path": final_path, "title": title}

        if fmt_key == "mp3":
            thumb_path = await asyncio.to_thread(
                _download_thumb, page_thumb or embed_thumb, os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_thumb.jpg")
            )
            if thumb_path:
                result["thumb"] = thumb_path
                result["artist"] = title

        log.info(
            "RetroTube sukses | title=%r size=%.2fMB kind=%s fmt=%s",
            title, os.path.getsize(final_path) / 1024 / 1024, kind, fmt_key,
        )
        return result
    except FileSizeLimitExceeded:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    except Exception:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
