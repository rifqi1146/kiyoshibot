"""FLOW DOWNLOADER
1. Scrape post metadata in a thread; reject MP3 for photo/video gallery posts.
2. Download photos concurrently, convert to JPEG, keep gallery order.
3. If Cossora embed exists: decrypt HLS URL, download TS segments in parallel,
   concat/remux to MP4 faststart.
4. Return one shared album payload: photos first, video last.
"""
import asyncio
import os
import subprocess
import time
import uuid

import aiohttp
from PIL import Image, ImageOps

from handlers.dl.constants import MAX_TG_SIZE, TMP_DIR
from handlers.dl.progress import PROGRESS_LOG_INTERVAL, TransferStats, edit_status, render_progress_text
from handlers.dl.utils import FileSizeLimitExceeded, sanitize_filename
from . import cossora, extractor
from .constants import (
    COSSORA_FFMPEG_TIMEOUT,
    COSSORA_SEG_CONCURRENCY,
    COSSORA_SEG_RETRIES,
    COSPLAYTELE_ALBUM_CONCURRENCY,
    UA,
    _HTTP_TIMEOUT,
)

is_cosplaytele_url = extractor.is_cosplaytele_url


def _jpeg(source, destination):
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((3840, 3840))
        image.convert('RGB').save(destination, 'JPEG', quality=90)


def _concat_segments(seg_files: list[str], out_path: str, work_dir: str) -> str:
    concat = os.path.join(work_dir, 'concat.txt')
    with open(concat, 'w', encoding='utf-8') as fh:
        for seg in seg_files:
            safe = os.path.abspath(seg).replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")
    cmd = [
        'ffmpeg', '-y', '-loglevel', 'error',
        '-f', 'concat', '-safe', '0', '-i', concat,
        '-c', 'copy', '-movflags', '+faststart', out_path,
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=COSSORA_FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'ffmpeg timeout after {COSSORA_FFMPEG_TIMEOUT}s') from exc
    if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"ffmpeg failed: {(result.stderr or '').strip()[-400:]}")
    return out_path


async def _download_photos(raw_url: str, photos: list[str], title: str, bot, chat_id, status_msg_id,
                           tag: str) -> list[dict]:
    if not photos:
        return []
    paths = [os.path.join(TMP_DIR, f'{tag}_cosplaytele_{i:03d}.jpg') for i in range(len(photos))]
    stats = TransferStats()
    done = 0
    sem = asyncio.Semaphore(max(1, COSPLAYTELE_ALBUM_CONCURRENCY))

    async def progress(force=False):
        if stats.should_log():
            stats.log('Cosplaytele photos', label=title)
        if force or stats.should_edit():
            await edit_status(bot, chat_id, status_msg_id, render_progress_text(
                title, downloaded=stats.downloaded, total=stats.total,
                speed_bps=stats.speed_bps or stats.avg_bps,
                eta_seconds=stats.eta_seconds,
                pct=done * 100 / len(photos), extra=f'{done}/{len(photos)} photos'))

    async def one(session, index, url):
        nonlocal done
        path = paths[index]
        size = 0
        async with sem:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError(f'Cosplaytele image HTTP {response.status}')
                if (response.content_length or 0) > MAX_TG_SIZE:
                    raise FileSizeLimitExceeded('Photo exceeds download size limit.')
                with open(path + '.part', 'wb') as output:
                    async for chunk in response.content.iter_chunked(512 * 1024):
                        size += len(chunk)
                        if size > MAX_TG_SIZE:
                            raise FileSizeLimitExceeded('Photo exceeds download size limit.')
                        output.write(chunk)
                        stats.sample(stats.downloaded + len(chunk))
                        await progress()
        if not size:
            raise RuntimeError('Cosplaytele returned an empty photo.')
        conversion = asyncio.create_task(asyncio.to_thread(_jpeg, path + '.part', path))
        try:
            await asyncio.shield(conversion)
        except asyncio.CancelledError:
            await conversion
            raise
        os.remove(path + '.part')
        done += 1

    tasks = []
    try:
        await progress(force=True)
        async with aiohttp.ClientSession(
            headers={'User-Agent': UA, 'Referer': raw_url},
            timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT),
        ) as session:
            tasks = [asyncio.create_task(one(session, i, url)) for i, url in enumerate(photos)]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        stats.total = stats.downloaded
        await progress(force=True)
        stats.log_done('Cosplaytele photos', label=title)
        return [{'path': path, 'type': 'photo'} for path in paths]
    except BaseException:
        for path in paths:
            for candidate in (path, path + '.part'):
                try:
                    os.remove(candidate)
                except FileNotFoundError:
                    pass
        raise


async def _download_cossora_video(embed_url: str, title: str, bot, chat_id, status_msg_id, tag: str) -> dict:
    resolved = await asyncio.to_thread(cossora.resolve_embed, embed_url)
    stream = await asyncio.to_thread(cossora.probe_stream, resolved['playlist_url'], resolved['referer'])
    segments = stream['segments']
    work_dir = os.path.join(TMP_DIR, f'cossora_{tag[:10]}')
    os.makedirs(work_dir, exist_ok=True)

    sem = asyncio.Semaphore(max(1, COSSORA_SEG_CONCURRENCY))
    stats = TransferStats()
    done = 0
    total_bytes = 0
    lock = asyncio.Lock()
    emit_lock = asyncio.Lock()
    last_edit = -10.0

    async def emit(force=False):
        nonlocal last_edit
        now = time.monotonic()
        stats.sample(total_bytes, now=now)
        if done:
            stats.total = max(int(total_bytes / done * len(segments)), total_bytes, 1)
        if stats.should_log(PROGRESS_LOG_INTERVAL, now=now):
            stats.log('Cosplaytele video', label=title)
        if not status_msg_id:
            return
        interval = stats.adaptive_edit_interval()
        if not force and now - last_edit < interval:
            return
        last_edit = now
        pct = 100.0 if done >= len(segments) else done * 100.0 / len(segments)
        await edit_status(bot, chat_id, status_msg_id, render_progress_text(
            title, downloaded=stats.downloaded, total=stats.total,
            speed_bps=stats.speed_bps or stats.avg_bps,
            eta_seconds=stats.eta_seconds, pct=pct),
            label='Cosplaytele video')

    async def worker(index: int, url: str) -> str:
        nonlocal done, total_bytes
        path = os.path.join(work_dir, f'seg_{index:05d}.ts')
        async with sem:
            size = await asyncio.to_thread(
                cossora.download_segment, url, resolved['referer'], path, COSSORA_SEG_RETRIES,
            )
        async with lock:
            done += 1
            total_bytes += size
            if total_bytes > MAX_TG_SIZE:
                raise FileSizeLimitExceeded('Video exceeds download size limit.')
        async with emit_lock:
            await emit()
        return path

    try:
        async with emit_lock:
            await emit(force=True)
        seg_files = await asyncio.gather(*(worker(i, url) for i, url in enumerate(segments)))
        async with emit_lock:
            await emit(force=True)
        stats.log_done('Cosplaytele video', label=title, size=total_bytes)
        out_path = os.path.join(TMP_DIR, f'{tag}_cosplaytele_video.mp4')
        await asyncio.to_thread(_concat_segments, seg_files, out_path, work_dir)
        if os.path.getsize(out_path) > MAX_TG_SIZE:
            raise FileSizeLimitExceeded('Video exceeds download size limit.')
        return {
            'path': out_path,
            'type': 'video',
            'remux_done': True,
            'meta': {'duration': int(stream.get('duration') or 0)},
        }
    finally:
        for name in os.listdir(work_dir) if os.path.isdir(work_dir) else []:
            try:
                os.remove(os.path.join(work_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(work_dir)
        except OSError:
            pass


async def cosplaytele_download(raw_url, fmt_key, bot, chat_id, status_msg_id,
                              metadata_ready=False, **kwargs):
    if fmt_key == 'mp3':
        raise RuntimeError('This Cosplaytele downloader supports photo/video albums, not MP3.')
    if not metadata_ready:
        await edit_status(bot, chat_id, status_msg_id, '<b>Scraping Cosplaytele metadata...</b>')
    post = await asyncio.to_thread(extractor.scrape_post, raw_url)
    title = sanitize_filename(post['title'], 100)
    photos = post.get('images') or []
    video_embed = post.get('video_embed') or ''
    tag = uuid.uuid4().hex
    os.makedirs(TMP_DIR, exist_ok=True)

    items = await _download_photos(raw_url, photos, title, bot, chat_id, status_msg_id, tag)
    if video_embed:
        await edit_status(bot, chat_id, status_msg_id, '<b>Resolving Cosplaytele video...</b>', label='Cosplaytele video')
        items.append(await _download_cossora_video(video_embed, title, bot, chat_id, status_msg_id, tag))

    if not items:
        raise RuntimeError('No supported media found in this Cosplaytele post.')
    return {'items': items, 'title': title, 'source': 'cosplaytele'}
