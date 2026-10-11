"""Konstanta handler command `/anichin` (Anichin — fansub Donghua Sub Indo).

FLOW KONSTANTA
--------------
1. `LABEL`/`PREFIX`/`BASE_URL`/`UA` untuk semua request curl_cffi.
2. Batas UI: `PER_PAGE` (hasil listing), `GENRES_PER_PAGE`, `AZ_PER_PAGE`,
   `EPISODES_PER_PAGE`.
3. `MAX_RESULTS`: batas keras hasil search/kategori/genre/A-Z.
4. Cache: `CACHE_TTL`/`MAX_CACHE_ENTRIES` (sesi), `INDEX_TTL` (genre/A-Z).
5. `BROWSE` + `CATEGORY_SLUGS`: peta tombol listing statis.
6. `SCHEDULE_DAYS`: urutan hari di halaman `/schedule/`.
"""
LABEL = "Anichin"
PREFIX = "ac"
BASE_URL = "https://anichin.moe"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Jumlah item per halaman Telegram.
PER_PAGE = 5
GENRES_PER_PAGE = 10
AZ_PER_PAGE = 10
EPISODES_PER_PAGE = 15
# Batas keras hasil search / genre / A-Z.
MAX_RESULTS = 100
# Umur cache sesi (detik) + batas entri.
CACHE_TTL = 3600
MAX_CACHE_ENTRIES = 120
# Umur cache daftar genre & A-Z.
INDEX_TTL = 3600
# Timeout HTTP halaman listing.
HTTP_TIMEOUT = 25

# Tombol listing statis: key -> (judul, path, apakah ada paginasi).
BROWSE = {
    "list": ("Donghua List", "/anime", True),
    "ongoing": ("Ongoing", "/ongoing", True),
    "completed": ("Completed", "/completed", True),
    "upcoming": ("Upcoming", "/upcoming-donghua", False),
    "drop": ("Drop", "/drop", False),
}

# Urutan hari di `/schedule/` (label persis seperti di halaman Anichin).
SCHEDULE_DAYS = ("Senin", "Selasa", "Rabu", "Kamis", "Jum'at", "Sabtu", "Minggu", "Acak")

DAY_EN_MAP = {
    "senin": "Monday",
    "selasa": "Tuesday",
    "rabu": "Wednesday",
    "kamis": "Thursday",
    "jum'at": "Friday",
    "jumat": "Friday",
    "sabtu": "Saturday",
    "minggu": "Sunday",
    "acak": "Random",
}
