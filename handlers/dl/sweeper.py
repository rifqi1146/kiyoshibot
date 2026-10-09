import os
import time
import asyncio
import logging
import shutil

from .constants import TMP_DIR

log = logging.getLogger(__name__)

DOWNLOADS_TTL_SEC = int(os.getenv("DOWNLOADS_TTL_SEC", "3600"))
DOWNLOADS_SWEEP_INTERVAL_SEC = int(os.getenv("DOWNLOADS_SWEEP_INTERVAL_SEC", "600"))
# Grace period: jangan sapu file yang baru saja tersentuh walau TTL di-set rendah,
# supaya download yang masih jalan tidak dihapus. mtime file progresif berubah,
# jadi ini hanya pengaman ekstra di atas fd-guard.
DOWNLOADS_GRACE_SEC = int(os.getenv("DOWNLOADS_GRACE_SEC", "300"))

# Profil browser sementara milik scrapling/camoufox ikut menumpuk di /tmp
# (folder besar, di luar TMP_DIR). Jatuhnya kalau browser crash / tidak ditutup
# rapi, folder profilnya tertinggal. Pembersihannya harus KONSERVATIF:
#   - hanya prefix profil browser automation (whitelist), TIDAK PERNAH prefix
#     kerja manual — sekarang runtime bot TIDAK membuat workdir di /tmp sistem
#     (semua pakai TMP_DIR = folder downloads), jadi membereskan /tmp sengaja
#     dibatasi ke sampah library saja supaya riset manual Manuel di /tmp aman.
#   - harus lebih tua dari SAFE_TTL (4 jam) supaya browser yang masih hidup aman
#   - hanya direktori, tidak pernah file, tidak pernah symlink
TMP_BROWSER_TTL_SEC = int(os.getenv("TMP_BROWSER_TTL_SEC", str(4 * 3600)))
# Prefix ini disengaja pendek: `firefox_` DIBUANG — profil Firefox desktop juga
# memakai nama serupa, jadi bisa menghapus sesi browser aktif milik user.
_TMP_BROWSER_PREFIXES = tuple(
    p for p in os.getenv(
        "TMP_BROWSER_PREFIXES",
        "playwright_,playwright-,camoufox_",
    ).split(",") if p.strip()
)
_TMP_DIR = os.getenv("TMP_DIR_FOR_PROFILES", "/tmp")


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
            if os.path.islink(path):
                continue
            age = now - os.path.getmtime(path)
            if age < ttl:
                continue
            # Guard: jangan hapus bila masih dipakai proses hidup (mirror
            # _in_use untuk profil /tmp). Worker download bisa menahan file
            # lebih lama dari TTL pada transfer besar.
            if _in_use(path):
                continue
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except OSError as e:
            log.warning("Downloads sweep failed to remove | path=%s err=%r", path, e)
    if removed:
        log.info("Downloads sweep removed %s stale item(s) | dir=%s ttl=%ss", removed, TMP_DIR, ttl)
    return removed


def _in_use(path: str) -> bool:
    """True kalau ada proses hidup yang memakai `path` (cwd atau fd terbuka).

    Profil browser bisa telihat "lama" dari sisi mtime padahal masih dipakai:
    dir mtime hanya berubah kalau entri baru dibuat di root profil, sedangkan
    browser yang idle menulis ke file yang sudah ada (prefs.js) sehingga tidak
    menyentuh mtime dir. Guard ini memindai /proc khusus untuk kandidat yang
    SUDAH cocok whitelist + TTL — jumlahnya kecil, jadi ongkosnya murah.
    """
    try:
        base = os.path.realpath(path)
    except OSError:
        return True
    prefix = base + os.sep
    try:
        pids = os.listdir("/proc")
    except OSError:
        return True
    for pid in pids:
        if not pid.isdigit():
            continue
        root = f"/proc/{pid}"
        for link in (f"{root}/cwd", f"{root}/exe"):
            try:
                if os.path.realpath(link) == base:
                    return True
            except OSError:
                continue
        try:
            for fd in os.listdir(f"{root}/fd"):
                try:
                    if os.path.realpath(f"{root}/fd/{fd}").startswith(prefix):
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


def sweep_tmp_profiles_once(ttl_sec: int | None = None) -> int:
    """Sapu folder sementara lama di luar TMP_DIR (khususnya /tmp).

    Hanya membuang entri yang namanya cocok whitelist prefix DAN sudah lebih tua
    dari `ttl_sec`. Whitelist membuat operasi ini aman dijalankan di server yang
    dipakai proses lain.
    """
    ttl = TMP_BROWSER_TTL_SEC if ttl_sec is None else ttl_sec
    prefixes = _TMP_BROWSER_PREFIXES
    if not prefixes or ttl <= 0:
        return 0
    now = time.time()
    removed = 0
    try:
        names = os.listdir(_TMP_DIR)
    except OSError as e:
        log.warning("Tmp sweep failed to list dir | dir=%s err=%r", _TMP_DIR, e)
        return 0
    for name in names:
        if not name.startswith(prefixes):
            continue
        path = os.path.join(_TMP_DIR, name)
        try:
            # HANYA direktori yang disapu: sisa browser profil & workdir selalu
            # direktori, sedangkan file biasa (mis. script .py di /tmp) tidak
            # pernah disentuh walau namanya kebetulan berprefix sama.
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) < ttl:
                continue
            # Jangan hapus yang masih dipakai proses hidup (browser proyek lain
            # / sesi idle yang mtime dir-nya tak tersentuh).
            if _in_use(path):
                continue
            shutil.rmtree(path)
            removed += 1
        except OSError as e:
            log.debug("Tmp sweep skipped | path=%s err=%r", path, e)
    if removed:
        log.info("Tmp sweep removed %s stale item(s) | dir=%s ttl=%ss", removed, _TMP_DIR, ttl)
    return removed


async def start_downloads_sweeper():
    log.info(
        "Downloads sweeper started | dir=%s ttl=%ss interval=%ss",
        TMP_DIR, DOWNLOADS_TTL_SEC, DOWNLOADS_SWEEP_INTERVAL_SEC,
    )
    while True:
        try:
            await asyncio.to_thread(sweep_downloads_once)
            await asyncio.to_thread(sweep_tmp_profiles_once)
            await asyncio.sleep(DOWNLOADS_SWEEP_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Downloads sweeper error | err=%r", e)
            await asyncio.sleep(DOWNLOADS_SWEEP_INTERVAL_SEC)
