"""Shared progress tracking, Telegram rendering, and terminal logging.

Every downloader/uploader uses this module so the terminal logs and the
Telegram status message look identical across all platforms.

Terminal progress line:
    <Kind> progress | title=<label> <pct>% <downloaded>/<total> speed=<speed> avg_speed=<avg> eta=<eta>
Terminal completion line:
    <Kind> done | title=<label> size=<size> elapsed=<elapsed>s avg_speed=<avg>

Telegram status text:
    <b>{title}</b>

    <code>[■■■■□□□□□□] 42.7%</code>
    <code>12.5 MB/29.0 MB</code>
    <code>Speed: 3.2 MB/s</code>
    <code>ETA: 8s</code>
"""
import html
import time
import asyncio
import logging
import re
import os

from telegram.error import RetryAfter

from .utils import progress_bar, format_size, format_speed, format_eta

log = logging.getLogger(__name__)

# Shared cadence for every downloader/uploader. Environment overrides remain available.
PROGRESS_EDIT_INTERVAL = float(os.getenv("DL_PROGRESS_EDIT_INTERVAL", "2.5"))
PROGRESS_LOG_INTERVAL = float(os.getenv("DL_PROGRESS_LOG_INTERVAL", "2.5"))

_SIZE_UNITS = {
    "b": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024 ** 2,
    "mib": 1024 ** 2,
    "gb": 1024 ** 3,
    "gib": 1024 ** 3,
    "tb": 1024 ** 4,
    "tib": 1024 ** 4,
}

_NULLISH = {"", "n/a", "na", "unknown", "none", "null", "-", "?", "~"}


def parse_size_str(value) -> int:
    """Parse a human size such as ``12.34MiB`` / ``1.5 GB`` into bytes (0 if unknown)."""
    text = str(value or "").strip()
    if text.lower() in _NULLISH:
        return 0
    m = re.match(r"([\d.,]+)\s*([a-zA-Z]*)", text)
    if not m:
        return 0
    num = m.group(1).replace(",", "")
    try:
        number = float(num)
    except ValueError:
        return 0
    unit = (m.group(2) or "b").lower()
    return int(number * _SIZE_UNITS.get(unit, 1))


def parse_speed_str(value) -> float:
    """Parse a human speed such as ``3.2MiB/s`` into bytes/second (0 if unknown)."""
    text = str(value or "").strip()
    if text.lower() in _NULLISH:
        return 0.0
    if text.lower().endswith("/s"):
        text = text[:-2]
    return float(parse_size_str(text))


def parse_eta_str(value) -> float | None:
    """Parse an ETA such as ``01:02`` / ``1:02:03`` / ``42`` into seconds (None if unknown)."""
    text = str(value or "").strip()
    if text.lower() in _NULLISH:
        return None
    if ":" in text:
        try:
            nums = [int(p) for p in text.split(":")]
        except ValueError:
            return None
        seconds = 0
        for n in nums:
            seconds = seconds * 60 + n
        return float(seconds)
    try:
        return float(text)
    except ValueError:
        return None


# Single shared edit cache stored on the bot instance, keyed by (chat_id, message_id).
_EDIT_CACHE_ATTR = "_dl_status_edit_cache"


async def edit_status(
    bot,
    chat_id,
    message_id,
    text: str,
    *,
    min_interval: float = 0.0,
    label: str = "Progress",
) -> None:
    """Edit a Telegram status message with a shared, flood-safe throttle.

    Used by every downloader so the edit behaviour (dedup + RetryAfter handling)
    is identical across platforms.
    """
    if bot is None or not chat_id or not message_id:
        return
    cache = getattr(bot, _EDIT_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(bot, _EDIT_CACHE_ATTR, cache)
    key = (int(chat_id), int(message_id))
    prev = cache.get(key) or {}
    if prev.get("text") == text:
        return
    now = time.monotonic()
    if min_interval > 0 and (now - prev.get("ts", 0.0)) < min_interval:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        cache[key] = {"text": text, "ts": time.monotonic()}
    except RetryAfter as e:
        wait = max(int(getattr(e, "retry_after", 1)), 1)
        log.warning("%s RetryAfter | chat_id=%s wait=%s", label, chat_id, wait)
        await asyncio.sleep(wait + 1)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            log.debug("%s edit failed | chat_id=%s message_id=%s err=%r", label, chat_id, message_id, e)


def render_progress_text(
    title: str,
    *,
    downloaded: int = 0,
    total: int = 0,
    speed_bps: float = 0.0,
    eta_seconds: float | None = None,
    extra: str = "",
    pct: float | None = None,
) -> str:
    """Build the standard Telegram status body shared by every downloader.

    ``pct`` lets segment-based downloaders (e.g. HLS) drive the progress bar from
    completed segments while the size line still reports real bytes.
    """
    try:
        downloaded = max(int(downloaded or 0), 0)
    except (TypeError, ValueError):
        downloaded = 0
    try:
        total = max(int(total or 0), 0)
    except (TypeError, ValueError):
        total = 0

    lines = [f"<b>{html.escape(str(title))}</b>"]
    if extra:
        lines.append(f"<code>{html.escape(str(extra))}</code>")
    lines.append("")

    if total > 0:
        if pct is None:
            pct = float(downloaded) * 100.0 / float(total)
        pct = min(max(float(pct), 0.0), 100.0)
        lines.append(f"<code>{progress_bar(pct)}</code>")
        lines.append(f"<code>{format_size(downloaded)}/{format_size(total)}</code>")
    elif pct is not None:
        lines.append(f"<code>{progress_bar(min(max(float(pct), 0.0), 100.0))}</code>")
        lines.append(f"<code>{format_size(downloaded)} downloaded</code>")
    else:
        lines.append(f"<code>{format_size(downloaded)} downloaded</code>")

    if speed_bps > 0:
        lines.append(f"<code>Speed: {format_speed(speed_bps)}</code>")
    if eta_seconds is not None and eta_seconds >= 0 and total > 0 and speed_bps > 0:
        lines.append(f"<code>ETA: {format_eta(eta_seconds)}</code>")

    return "\n".join(lines)


def log_progress(
    kind: str,
    *,
    label: str = "",
    downloaded: int = 0,
    total: int = 0,
    speed_bps: float = 0.0,
    avg_bps: float = 0.0,
    eta_seconds: float | None = None,
) -> None:
    """Emit one consistent terminal progress line (percentage, speed, avg_speed, eta)."""
    try:
        downloaded = max(int(downloaded or 0), 0)
    except (TypeError, ValueError):
        downloaded = 0
    try:
        total = max(int(total or 0), 0)
    except (TypeError, ValueError):
        total = 0

    pct = (downloaded * 100.0 / total) if total > 0 else 0.0
    lbl = f"title={label} " if label else ""
    size_part = (
        f"{format_size(downloaded)}/{format_size(total)}"
        if total > 0
        else f"{format_size(downloaded)} downloaded"
    )
    eta_part = (
        f" eta={format_eta(eta_seconds)}"
        if (eta_seconds is not None and eta_seconds >= 0 and total > 0)
        else ""
    )
    log.info(
        "%s progress | %s%.1f%% %s speed=%s avg_speed=%s%s",
        kind,
        lbl,
        pct,
        size_part,
        format_speed(speed_bps),
        format_speed(avg_bps),
        eta_part,
    )


def log_done(
    kind: str,
    *,
    label: str = "",
    size: int = 0,
    elapsed: float = 0.0,
    avg_bps: float = 0.0,
) -> None:
    """Emit one consistent terminal completion line (size, elapsed, avg_speed)."""
    try:
        size = max(int(size or 0), 0)
    except (TypeError, ValueError):
        size = 0
    elapsed = max(float(elapsed or 0.0), 0.0)
    if avg_bps <= 0 and elapsed > 0:
        avg_bps = size / elapsed
    lbl = f"title={label} " if label else ""
    log.info(
        "%s done | %ssize=%s elapsed=%.2fs avg_speed=%s",
        kind,
        lbl,
        format_size(size),
        elapsed,
        format_speed(avg_bps),
    )


class TransferStats:
    """Tracks instantaneous + average transfer speed, ETA and percentage.

    Reused by every download/upload loop so the numbers shown in Telegram and in
    the terminal are computed identically.
    """

    __slots__ = (
        "total",
        "started",
        "_last_ts",
        "_last_bytes",
        "_last_log_ts",
        "_last_edit_ts",
        "downloaded",
        "speed_bps",
        "avg_bps",
        "eta_seconds",
    )

    def __init__(self, total: int = 0, *, started: float | None = None) -> None:
        self.total = max(int(total or 0), 0)
        self.started = started if started is not None else time.monotonic()
        self._last_ts = self.started
        self._last_bytes = 0
        self._last_log_ts = 0.0
        self._last_edit_ts = -10.0
        self.downloaded = 0
        self.speed_bps = 0.0
        self.avg_bps = 0.0
        self.eta_seconds: float | None = None

    def sample(self, done: int, *, now: float | None = None) -> "TransferStats":
        now = now if now is not None else time.monotonic()
        try:
            done = max(int(done or 0), 0)
        except (TypeError, ValueError):
            done = 0
        dt = max(now - self._last_ts, 0.001)
        self.speed_bps = max(done - self._last_bytes, 0) / dt
        elapsed = max(now - self.started, 0.001)
        self.avg_bps = done / elapsed
        self.downloaded = done
        self._last_ts = now
        self._last_bytes = done
        if self.total > 0 and self.speed_bps > 0 and done <= self.total:
            self.eta_seconds = (self.total - done) / self.speed_bps
        else:
            self.eta_seconds = None
        return self

    @property
    def pct(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(self.downloaded * 100.0 / self.total, 100.0)

    @property
    def elapsed(self) -> float:
        return max(time.monotonic() - self.started, 0.0)

    def should_log(self, interval: float = 2.5, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        if (
            self._last_log_ts <= 0
            or (now - self._last_log_ts) >= interval
            or (self.total > 0 and self.downloaded >= self.total)
        ):
            self._last_log_ts = now
            return True
        return False

    def should_edit(self, interval: float = 2.0, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        if (
            self._last_edit_ts < 0
            or (now - self._last_edit_ts) >= interval
            or (self.total > 0 and self.downloaded >= self.total)
        ):
            self._last_edit_ts = now
            return True
        return False

    def telegram_text(self, title: str, *, extra: str = "") -> str:
        return render_progress_text(
            title,
            downloaded=self.downloaded,
            total=self.total,
            speed_bps=self.speed_bps,
            eta_seconds=self.eta_seconds,
            extra=extra,
        )

    def log(self, kind: str, *, label: str = "") -> None:
        log_progress(
            kind,
            label=label,
            downloaded=self.downloaded,
            total=self.total,
            speed_bps=self.speed_bps,
            avg_bps=self.avg_bps,
            eta_seconds=self.eta_seconds,
        )

    def log_done(self, kind: str, *, label: str = "", size: int | None = None) -> None:
        """Emit the canonical completion line using this transfer's average speed."""
        final_size = self.downloaded if size is None else size
        log_done(
            kind,
            label=label,
            size=final_size,
            elapsed=self.elapsed,
            avg_bps=self.avg_bps,
        )

    async def emit(
        self,
        bot=None,
        chat_id=None,
        status_msg_id=None,
        title: str = "",
        *,
        kind: str = "Download",
        label: str = "",
        extra: str = "",
        log_interval: float = 2.5,
        edit_interval: float = 2.0,
    ) -> None:
        """Sample was already called; emit terminal log and edit Telegram if intervals elapsed."""
        now = time.monotonic()
        if self.should_log(log_interval, now=now):
            self.log(kind, label=label)
        if bot and chat_id and status_msg_id and self.should_edit(edit_interval, now=now):
            text = self.telegram_text(title, extra=extra)
            await edit_status(
                bot,
                chat_id,
                status_msg_id,
                text,
                min_interval=edit_interval,
                label=kind,
            )

    async def update(
        self,
        done: int,
        bot=None,
        chat_id=None,
        status_msg_id=None,
        title: str = "",
        *,
        kind: str = "Download",
        label: str = "",
        extra: str = "",
        log_interval: float = 2.5,
        edit_interval: float = 2.0,
    ) -> "TransferStats":
        """Sample bytes, log terminal if interval elapsed, edit Telegram if interval elapsed."""
        self.sample(done)
        await self.emit(
            bot=bot,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            title=title,
            kind=kind,
            label=label,
            extra=extra,
            log_interval=log_interval,
            edit_interval=edit_interval,
        )
        return self
