import html
import time
import asyncio
import logging
import re
import os

from telegram.error import RetryAfter

from .utils import progress_bar, format_size, format_speed, format_eta

log = logging.getLogger(__name__)

PROGRESS_EDIT_INTERVAL = float(os.getenv("DL_PROGRESS_EDIT_INTERVAL", "5.0"))
PROGRESS_LOG_INTERVAL = float(os.getenv("DL_PROGRESS_LOG_INTERVAL", "2.5"))

_SPEED_WINDOW = float(os.getenv("DL_SPEED_WINDOW", "1.0"))

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
    text = str(value or "").strip()
    if text.lower() in _NULLISH:
        return 0.0
    if text.lower().endswith("/s"):
        text = text[:-2]
    return float(parse_size_str(text))


def parse_eta_str(value) -> float | None:
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


_EDIT_CACHE_ATTR = "_dl_status_edit_cache"
_EDIT_CACHE_MAX = int(os.getenv("DL_EDIT_CACHE_MAX", "2048"))
_EDIT_CACHE_TTL = float(os.getenv("DL_EDIT_CACHE_TTL", "1800"))


def prune_edit_cache(
    cache: dict,
    *,
    now: float | None = None,
    max_entries: int | None = None,
    ttl: float | None = None,
) -> int:
    if not cache:
        return 0
    if max_entries is None:
        max_entries = _EDIT_CACHE_MAX
    if ttl is None:
        ttl = _EDIT_CACHE_TTL
    dropped = 0
    if ttl > 0 and len(cache) > 64:
        now = time.monotonic() if now is None else now
        for k in [k for k, v in cache.items() if now - float(v.get("ts", 0.0)) > ttl]:
            cache.pop(k, None)
            dropped += 1
    if max_entries > 0 and len(cache) > max_entries:
        excess = len(cache) - max_entries
        for k in list(cache)[:excess]:
            cache.pop(k, None)
            dropped += 1
    return dropped


async def edit_status(
    bot,
    chat_id,
    message_id,
    text: str,
    *,
    min_interval: float = 5.0,
    label: str = "Progress",
    force: bool = False,
) -> None:
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
    effective_min = 0.0 if force else max(5.0, float(min_interval if min_interval is not None else 5.0))
    if effective_min > 0 and (now - prev.get("ts", 0.0)) < effective_min:
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
        if len(cache) > _EDIT_CACHE_MAX:
            prune_edit_cache(cache)
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

    __slots__ = (
        "total",
        "started",
        "_win_ts",
        "_win_bytes",
        "_last_log_ts",
        "_done_logged",
        "_last_edit_ts",
        "_done_edited",
        "downloaded",
        "speed_bps",
        "avg_bps",
        "eta_seconds",
    )

    def __init__(self, total: int = 0, *, started: float | None = None) -> None:
        self.total = max(int(total or 0), 0)
        self.started = started if started is not None else time.monotonic()
        self._win_ts = self.started
        self._win_bytes = 0
        self._last_log_ts = 0.0
        self._done_logged = False
        self._last_edit_ts = -10.0
        self._done_edited = False
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

        first_byte = done > 0 and self.downloaded <= 0
        if first_byte:
            self.started = now
            self._win_ts = now
            self._win_bytes = 0
        self.downloaded = done

        if first_byte:
            self.avg_bps = 0.0
            self.speed_bps = 0.0
            self.eta_seconds = None
            return self

        span = now - self._win_ts
        elapsed = max(now - self.started, 0.001)
        self.avg_bps = done / elapsed

        if span >= _SPEED_WINDOW:
            gained = done - self._win_bytes
            self.speed_bps = max(gained, 0) / span
            self._win_ts = now
            self._win_bytes = done

        if elapsed < 5.0 and self.speed_bps > 0:
            self.avg_bps = min(self.avg_bps, self.speed_bps * 2.0)

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
        iv = float(interval if interval is not None else 2.5)
        now = now if now is not None else time.monotonic()
        if self.total > 0 and self.downloaded >= self.total:
            if self._done_logged:
                return False
            self._done_logged = True
            self._last_log_ts = now
            return True
        if (
            self._last_log_ts <= 0
            or (now - self._last_log_ts) >= iv
        ):
            self._last_log_ts = now
            return True
        return False

    def adaptive_edit_interval(self) -> float:
        now = time.monotonic()
        if self.speed_bps > 0:
            speed = self.speed_bps
        elif (now - self.started) >= 5.0 and self.avg_bps > 0:
            speed = self.avg_bps
        else:
            speed = 0.0

        MB = 1024 * 1024
        if speed <= 0:
            iv = 10.0
        elif speed < 1.0 * MB:
            iv = 10.0
        elif speed < 3.0 * MB:
            iv = 7.0
        else:
            iv = 5.0

        if self.total > 0 and speed > 0 and self.downloaded < self.total:
            eta = (self.total - self.downloaded) / speed
            if eta < iv * 2.0:
                iv = max(5.0, eta)
        return max(5.0, iv)

    def should_edit(self, interval: float = 5.0, *, now: float | None = None) -> bool:
        iv = float(interval if interval is not None else 5.0)
        now = now if now is not None else time.monotonic()
        if iv <= 0:
            self._last_edit_ts = now
            return True

        if self.total > 0 and self.downloaded >= self.total:
            if self._done_edited:
                return False
            self._done_edited = True
            self._last_edit_ts = now
            return True

        effective = max(5.0, iv, self.adaptive_edit_interval())

        if self._last_edit_ts < 0 or (now - self._last_edit_ts) >= effective:
            self._last_edit_ts = now
            return True
        return False

    def telegram_text(self, title: str, *, extra: str = "") -> str:
        display_speed = self.speed_bps if self.speed_bps > 0 else self.avg_bps
        display_eta = self.eta_seconds
        if display_eta is None and display_speed > 0 and self.total > 0 and self.downloaded <= self.total:
            display_eta = (self.total - self.downloaded) / display_speed
        return render_progress_text(
            title,
            downloaded=self.downloaded,
            total=self.total,
            speed_bps=display_speed,
            eta_seconds=display_eta,
            extra=extra,
        )

    def log(self, kind: str, *, label: str = "") -> None:
        display_speed = self.speed_bps if self.speed_bps > 0 else self.avg_bps
        display_eta = self.eta_seconds
        if display_eta is None and display_speed > 0 and self.total > 0 and self.downloaded <= self.total:
            display_eta = (self.total - self.downloaded) / display_speed
        log_progress(
            kind,
            label=label,
            downloaded=self.downloaded,
            total=self.total,
            speed_bps=display_speed,
            avg_bps=self.avg_bps,
            eta_seconds=display_eta,
        )

    def log_done(self, kind: str, *, label: str = "", size: int | None = None) -> None:
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
        edit_interval: float = 5.0,
    ) -> None:
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
                min_interval=max(5.0, edit_interval),
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
        edit_interval: float = 5.0,
    ) -> "TransferStats":
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
            edit_interval=max(5.0, edit_interval),
        )
        return self
