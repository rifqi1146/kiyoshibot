"""Konstanta command `/drakorid` (Drakor.id / drakorid.co)."""
LABEL = "Drakor.id"
PREFIX = "dk"
BASE_URL = "https://drakorid.co"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Jumlah hasil per halaman Telegram.
PER_PAGE = 5
# Jumlah kategori per halaman picker.
CATS_PER_PAGE = 10
# Batas keras hasil search / latest / kategori.
MAX_RESULTS = 100
# Umur cache sesi (detik) dan batas jumlah entri.
CACHE_TTL = 3600
MAX_CACHE_ENTRIES = 120
