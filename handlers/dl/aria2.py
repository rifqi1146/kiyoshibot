"""Helper unduh paralel multi-koneksi lewat aria2c.

Dipakai scraper yang CDN-nya men-throttle koneksi tunggal (mis. FemdomVC
~250 KB/s per socket). Dengan 16 koneksi paralel, throughput naik 10-20x.
Selalu ada fallback ke streaming `curl_cffi` bila aria2c tidak ada / gagal.
"""
import os
import sys
import shutil
import logging
import asyncio

from .constants import MAX_TG_SIZE
from .progress import TransferStats

log = logging.getLogger(__name__)

# Jumlah koneksi paralel per unduhan. Bisa dioverride per-situs via env
# `ARIA2_CONNS=<n>` (global) atau argumen `conns=`.
DEFAULT_CONNS = int(os.getenv("ARIA2_CONNS", "16"))

_ARIA2_PATH: str | None = None


def _resolve_aria2() -> str | None:
    """Cari `aria2c`. PATH bot sering tidak memuat `venv/bin`, jadi cek
    direktori bin milik interpreter yang sedang jalan sebagai fallback."""
    global _ARIA2_PATH
    if _ARIA2_PATH is not None:
        return _ARIA2_PATH or None
    found = shutil.which("aria2c")
    if not found:
        # venv/bin/aria2c relatif ke interpreter (…/venv/bin/python).
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cand = os.path.join(exe_dir, "aria2c")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            found = cand
    if not found:
        # site-packages/aria2c/bin/aria2c (paket pip `aria2`).
        lib_dir = os.path.normpath(os.path.join(exe_dir, "..", "lib"))
        if os.path.isdir(lib_dir):
            for root, _dirs, files in os.walk(lib_dir):
                if "aria2c" in files and os.path.basename(os.path.dirname(root)) == "aria2c":
                    c = os.path.join(root, "aria2c")
                    if os.path.isfile(c) and os.access(c, os.X_OK):
                        found = c
                        break
    _ARIA2_PATH = found or ""
    if found:
        log.info("aria2c resolved | path=%s", found)
    else:
        log.warning("aria2c not found; download will use streaming")
    return found or None


def aria2_available() -> bool:
    return _resolve_aria2() is not None


async def download_aria2(
    url: str,
    out_path: str,
    *,
    headers: dict | None = None,
    total_size: int = 0,
    conns: int = 0,
    kind: str = "Download",
    label: str = "",
    title: str = "",
    bot=None,
    chat_id=None,
    status_msg_id=None,
    notify: bool = True,
    log_interval: float = 2.5,
    edit_interval: float = 2.5,
    timeout: float = 0,
) -> bool:
    """Unduh `url` -> `out_path` via aria2c multi-koneksi.

    Return True kalau sukses (file ada & tidak kosong). Return False kalau
    aria2c tidak tersedia atau gagal supaya pemanggil bisa fallback.

    Menghormati batas `MAX_TG_SIZE` (2GB) dan memancarkan progress ke Telegram
    + terminal lewat `TransferStats` selama unduhan berjalan.
    """
    aria2 = _resolve_aria2()
    if not aria2:
        return False

    n = int(conns or DEFAULT_CONNS)
    n = max(1, min(n, 16))
    out_dir = os.path.dirname(os.path.abspath(out_path))
    out_name = os.path.basename(out_path)

    cmd = [
        aria2,
        "--dir", out_dir,
        "--out", out_name,
        "--file-allocation=none",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--continue=true",
        f"--max-connection-per-server={n}",
        f"--split={n}",
        "--min-split-size=1M",
        "--summary-interval=0",
        "--download-result=hide",
        "--console-log-level=warn",
    ]
    for k, v in (headers or {}).items():
        if v:
            cmd.extend(["--header", f"{k}: {v}"])
    cmd.append(url)

    async def _run_once(conns: int) -> tuple[int, bytes]:
        c = list(cmd)
        # ganti jumlah koneksi pada percobaan retry (lebih konservatif)
        for i, a in enumerate(c):
            if a.startswith("--max-connection-per-server="):
                c[i] = f"--max-connection-per-server={conns}"
            elif a.startswith("--split="):
                c[i] = f"--split={conns}"
        proc = await asyncio.create_subprocess_exec(
            *c,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        loop = asyncio.get_event_loop()
        started = loop.time()

        async def _poll():
            while proc.returncode is None:
                if timeout and (loop.time() - started) > timeout:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
                if os.path.exists(out_path):
                    cur = os.path.getsize(out_path)
                    stats.sample(cur)
                    if cur > MAX_TG_SIZE:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return
                    if notify and status_msg_id:
                        await stats.emit(
                            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
                            title=title or label,
                            kind=kind, label=label,
                            log_interval=log_interval, edit_interval=edit_interval,
                        )
                    # File sudah penuh: aria2c tinggal menutup koneksi / exit.
                    # Stop polling supaya tidak terus sample+emit (progress 100%
                    # dan speed sisa dikit yang spam edit di log).
                    if total_size > 0 and cur >= total_size:
                        return
                await asyncio.sleep(0.5)

        poll = asyncio.create_task(_poll())
        _, stderr_b = await proc.communicate()
        poll.cancel()
        try:
            await poll
        except asyncio.CancelledError:
            pass
        return (proc.returncode if proc.returncode is not None else -1), (stderr_b or b"")

    stats = TransferStats(total_size)

    # aria2c sesekali crash (SIGSEGV / exit code negatif) pada unduhan 16-koneksi.
    # Coba sekali lagi dengan koneksi lebih sedikit sebelum menyerah ke streaming.
    rc, stderr_b = await _run_once(n)
    if rc != 0 and rc < 0 and n > 4:
        log.info("aria2c crash (signal %s), retry koneksi=8 | url=%s", -rc, url[:80])
        for _p in (out_path, out_path + ".aria2"):
            try:
                if os.path.exists(_p):
                    os.remove(_p)
            except OSError:
                pass
        stats = TransferStats(total_size)
        rc, stderr_b = await _run_once(min(8, n))

    if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        err = stderr_b.decode("utf-8", errors="ignore").strip()
        log.warning("aria2c failed, falling back to streaming | code=%s err=%s", rc, err[:200])
        # Bersihkan file parsial + control file (.aria2) supaya fallback streaming
        # mulai dari awal dengan file kosong (bukan menimpa sisa parsial).
        for p in (out_path, out_path + ".aria2"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        return False

    final = os.path.getsize(out_path)
    if final > MAX_TG_SIZE:
        from .utils import FileSizeLimitExceeded
        raise FileSizeLimitExceeded("File exceeds 2GB limit. Download canceled.")
    stats.sample(final)
    if notify and status_msg_id:
        await stats.emit(
            bot=bot, chat_id=chat_id, status_msg_id=status_msg_id,
            title=title or label, kind=kind, label=label,
            log_interval=log_interval, edit_interval=edit_interval,
        )
    stats.log_done(kind, label=label, size=final)
    return True
