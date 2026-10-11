"""Konstanta command `/nekopoi` (Nekopoi explorer: browse, genre, A-Z, jadwal).

FLOW KONSTANTA
--------------
1. `BASE_URL`/`UA` dipakai semua request curl_cffi (impersonate Chrome).
2. `PER_PAGE`     : jumlah kartu per halaman Telegram (hasil listing).
3. `CATS_PER_PAGE`: jumlah kategori per halaman picker.
4. `GENRES_PER_PAGE`: jumlah genre per halaman picker.
5. `MAX_RESULTS`  : batas keras hasil search/kategori/genre.
6. `CACHE_TTL`/`MAX_CACHE_ENTRIES`: umur & batas cache sesi.
7. `AZ_TTL`       : umur cache daftar A-Z (hentai-list/jav-list, ~1MB sekali fetch).
8. `EPISODES_PER_PAGE`: jumlah tombol episode per halaman di episode picker.
"""
LABEL = "Nekopoi"
PREFIX = "nk"
BASE_URL = "https://nekopoi.care"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Hasil listing per halaman Telegram.
PER_PAGE = 5
# Picker kategori (5 kategori saja: hentai, 2d-animation, 3d-hentai, jav, jav-cosplay).
CATS_PER_PAGE = 10
# Picker genre (78 genre).
GENRES_PER_PAGE = 10
# Tombol episode per halaman.
EPISODES_PER_PAGE = 15
# Batas keras hasil search / kategori / genre.
MAX_RESULTS = 100
# Umur cache sesi (detik) + batas entri.
CACHE_TTL = 3600
MAX_CACHE_ENTRIES = 120
# Umur cache daftar A-Z & jadwal.
AZ_TTL = 3600
# Timeout HTTP default untuk halaman listing (bukan segmen HLS).
HTTP_TIMEOUT = 25

# Kategori statis dari `ul.menu` (halaman kategori Nekopoi).
CATEGORIES = (
    {"slug": "hentai", "name": "Hentai"},
    {"slug": "2d-animation", "name": "2D Animation"},
    {"slug": "3d-hentai", "name": "3D Hentai"},
    {"slug": "jav", "name": "JAV"},
    {"slug": "jav-cosplay", "name": "JAV Cosplay"},
)
