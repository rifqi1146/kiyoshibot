
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _JAKARTA = ZoneInfo("Asia/Jakarta")
except Exception:
    _JAKARTA = None

WEEKDAYS_ID = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
MONTHS_ID = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)


def now_jakarta() -> datetime:
    if _JAKARTA is not None:
        return datetime.now(_JAKARTA)
    return datetime.now()


def time_context() -> str:
    now = now_jakarta()
    hari = WEEKDAYS_ID[now.weekday()]
    bulan = MONTHS_ID[now.month - 1]
    return (
        f"Hari ini {hari}, {now.day} {bulan} {now.year}, pukul {now.strftime('%H:%M')} WIB "
        f"(zona Asia/Jakarta, UTC+7)."
    )


def time_context_short() -> str:
    now = now_jakarta()
    hari = WEEKDAYS_ID[now.weekday()]
    bulan = MONTHS_ID[now.month - 1]
    return f"{hari}, {now.day} {bulan} {now.year} — {now.strftime('%H:%M')} WIB"


def current_year() -> int:
    return now_jakarta().year
