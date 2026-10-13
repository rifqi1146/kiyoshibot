"""uvloop-safe async subprocess spawner.

PENTING (uvloop fork() SIGSEGV -11):
`asyncio.create_subprocess_exec` di bawah uvloop memanggil `fork()` lewat
`libuv.uv_spawn`. Ketika bot memiliki background C-threads aktif (seperti
curl_cffi, Playwright/scrapling, atau sqlite3), `fork()` pada thread lain merusak
state internal `libc.so.6` sehingga anak proses mati SIGSEGV (`exit code -11`)
sebelum/saat `execve` (terkonfirmasi di kernel dmesg: segfault in libc.so.6).
Selain itu, uvloop child watcher bisa rusak sehingga SEMUA pemanggilan spawn
berikutnya gagal sampai bot di-restart.

`subprocess.Popen` yang dipanggil via `asyncio.to_thread` aman karena
menggunakan implementasi CPython (`posix_spawnp` / `CLONE_VM | CLONE_VFORK`)
dan tidak menyentuh uvloop child watcher.

Modul ini menyediakan `AsyncProcess` dan `spawn_process()` sebagai drop-in
replacement yang aman untuk `asyncio.create_subprocess_exec`.
"""
import os
import sys
import shutil
import asyncio
import subprocess
from typing import Any

PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL
STDOUT = subprocess.STDOUT

_RESOLVED_ARIA2: str | None = None


def resolve_aria2() -> str | None:
    """Temukan binary `aria2c`. Cek PATH sistem, lalu `venv/bin/aria2c` dan site-packages."""
    global _RESOLVED_ARIA2
    if _RESOLVED_ARIA2 is not None:
        return _RESOLVED_ARIA2 or None
    found = shutil.which("aria2c")
    if not found:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cand = os.path.join(exe_dir, "aria2c")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            found = cand
    if not found:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        lib_dir = os.path.normpath(os.path.join(exe_dir, "..", "lib"))
        if os.path.isdir(lib_dir):
            for root, _dirs, files in os.walk(lib_dir):
                if "aria2c" in files and os.path.basename(os.path.dirname(root)) == "aria2c":
                    c = os.path.join(root, "aria2c")
                    if os.path.isfile(c) and os.access(c, os.X_OK):
                        found = c
                        break
    _RESOLVED_ARIA2 = found or ""
    return found or None


class AsyncProcess:
    """Wrapper `subprocess.Popen` dengan interface async ala `asyncio.subprocess.Process`."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    @property
    def stdin(self):
        return self._proc.stdin

    @property
    def stdout(self):
        return self._proc.stdout

    @property
    def stderr(self):
        return self._proc.stderr

    def poll(self) -> int | None:
        return self._proc.poll()

    def kill(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass

    def terminate(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            pass

    async def wait(self) -> int:
        return await asyncio.to_thread(self._proc.wait)

    async def communicate(self, input: bytes | str | None = None) -> tuple[bytes, bytes]:
        if input is not None:
            return await asyncio.to_thread(self._proc.communicate, input)
        return await asyncio.to_thread(self._proc.communicate)


async def spawn_process(
    *cmd: Any,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    cwd: str | None = None,
    env: dict | None = None,
    **kwargs: Any,
) -> AsyncProcess:
    """Spawn child process via `subprocess.Popen` di worker thread (posix_spawn).

    Aman dijalankan di bawah uvloop dan thread C aktif.
    """
    flat_cmd: list[str] = []
    for arg in cmd:
        if isinstance(arg, (list, tuple)):
            flat_cmd.extend(str(a) for a in arg)
        else:
            flat_cmd.append(str(arg))

    proc = await asyncio.to_thread(
        subprocess.Popen,
        flat_cmd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
        env=env,
        **kwargs,
    )
    return AsyncProcess(proc)
