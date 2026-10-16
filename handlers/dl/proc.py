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
