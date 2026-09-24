import os
import time
import asyncio
import logging

from .constants import TMP_DIR

log = logging.getLogger(__name__)

DOWNLOADS_TTL_SEC = int(os.getenv("DOWNLOADS_TTL_SEC", "7200"))
DOWNLOADS_SWEEP_INTERVAL_SEC = int(os.getenv("DOWNLOADS_SWEEP_INTERVAL_SEC", "1800"))


def sweep_downloads_once(ttl_sec: int | None = None) -> int:
    ttl = DOWNLOADS_TTL_SEC if ttl_sec is None else ttl_sec
    now = time.time()
    removed = 0
    try:
        names = os.listdir(TMP_DIR)
    except OSError as e:
        log.warning("Downloads sweep failed to list dir | dir=%s err=%r", TMP_DIR, e)
        return 0
    for name in names:
        path = os.path.join(TMP_DIR, name)
        try:
            if not os.path.isfile(path):
                continue
            if now - os.path.getmtime(path) >= ttl:
                os.remove(path)
                removed += 1
        except OSError as e:
            log.warning("Downloads sweep failed to remove | path=%s err=%r", path, e)
    if removed:
        log.info("Downloads sweep removed %s stale file(s) | dir=%s ttl=%ss", removed, TMP_DIR, ttl)
    return removed


async def start_downloads_sweeper():
    log.info("Downloads sweeper started | dir=%s ttl=%ss interval=%ss", TMP_DIR, DOWNLOADS_TTL_SEC, DOWNLOADS_SWEEP_INTERVAL_SEC)
    while True:
        try:
            await asyncio.to_thread(sweep_downloads_once)
            await asyncio.sleep(DOWNLOADS_SWEEP_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Downloads sweeper error | err=%r", e)
            await asyncio.sleep(DOWNLOADS_SWEEP_INTERVAL_SEC)
