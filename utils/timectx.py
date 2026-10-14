"""Konteks waktu Asia/Jakarta untuk LLM.

Dipakai bersama oleh `/ask` (`handlers/proxy/ask.py`) dan Caca
(`handlers/caca/caca.py`). Model tidak tahu tanggal hari ini dari data
latihannya, jadi blok ini disuntik ke prompt supaya jawaban soal "sekarang jam
berapa", "hari apa", "tanggal berapa" tidak dikarang.

Format balasan: kalimat Bahasa Indonesia, mis.
    "Hari ini Selasa, 3 Juni 2026, pukul 21:14 WIB (zona Asia/Jakarta, UTC+7)."
"""

from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _JAKARTA = ZoneInfo("Asia/Jakarta")
except Exception:  # tzdata tidak tersedia -> fallback ke waktu lokal server
    _JAKARTA = None

WEEKDAYS_ID = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
MONTHS_ID = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)


def now_jakarta() -> datetime:
    """Waktu sekarang zona Asia/Jakarta (UTC+7)."""
    if _JAKARTA is not None:
        return datetime.now(_JAKARTA)
    return datetime.now()


def time_context() -> str:
    """Kalimat konteks waktu lengkap (hari, tanggal, jam, zona)."""
    now = now_jakarta()
    hari = WEEKDAYS_ID[now.weekday()]
    bulan = MONTHS_ID[now.month - 1]
    return (
        f"Hari ini {hari}, {now.day} {bulan} {now.year}, pukul {now.strftime('%H:%M')} WIB "
        f"(zona Asia/Jakarta, UTC+7)."
    )


def time_context_short() -> str:
    """Versi ringkas untuk diselipkan di prompt yang hemat token."""
    now = now_jakarta()
    hari = WEEKDAYS_ID[now.weekday()]
    bulan = MONTHS_ID[now.month - 1]
    return f"{hari}, {now.day} {bulan} {now.year} — {now.strftime('%H:%M')} WIB"


def current_year() -> int:
    return now_jakarta().year
