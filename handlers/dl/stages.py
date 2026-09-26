"""Lightweight monotonic stage timing for downloader end-to-end latency.

Debug-only. Enable with DL_STAGE_LOG=1 (default off in production).
One line per job so we can find the real bottleneck from code path + timing
instead of guessing.

Usage:
    from .stages import stage, timed
    with timed("download"):
        ...
    stage("total", t0)
"""
import os
import time
import logging

log = logging.getLogger(__name__)
_ENABLED = os.getenv("DL_STAGE_LOG", "0").strip().lower() in ("1", "true", "on", "yes")


def stage(name: str, started: float, job: str = "") -> None:
    if not _ENABLED:
        return
    elapsed_ms = (time.monotonic() - started) * 1000.0
    log.info("STAGE | job=%s | %s | %.1f ms", job or "-", name, elapsed_ms)


class timed:
    """Context manager: measure a stage with monotonic clock, single log line."""

    __slots__ = ("name", "job", "_t0", "_enabled")

    def __init__(self, name: str, job: str = "") -> None:
        self.name = name
        self.job = job
        self._t0 = 0.0
        self._enabled = _ENABLED

    def __enter__(self):
        if self._enabled:
            self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._enabled:
            elapsed_ms = (time.monotonic() - self._t0) * 1000.0
            level = logging.INFO if exc_type is None else logging.WARNING
            log.log(
                level,
                "STAGE | job=%s | %s | %.1f ms%s",
                self.job or "-",
                self.name,
                elapsed_ms,
                "" if exc_type is None else f" | err={exc!r}",
            )
        return False
