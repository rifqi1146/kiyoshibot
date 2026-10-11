"""Konstanta explorer RetroTube (`/becekku`, `/lendirqu`).

FLOW KONSTANTA
--------------
1. `SITES`          : konfigurasi per situs (label, prefix callback, base URL,
   gate URL, contoh command). Keduanya memakai tema WP-Script `retrotube`
   sehingga selector HTML-nya identik.
2. `PER_PAGE`       : jumlah kartu per halaman Telegram (hasil listing).
3. `CATS_PER_PAGE`  : jumlah kategori per halaman picker.
4. `TAGS_PER_PAGE`  : jumlah tag per halaman picker (becekku 416, lendirqu ~400).
5. `MAX_RESULTS`    : batas keras hasil search/kategori/tag/filter.
6. `CACHE_TTL`/`MAX_CACHE_ENTRIES`: umur & batas cache sesi.
7. `INDEX_TTL`      : umur cache kategori & tag cloud (1 jam).
"""

SITES = {
    "becekku": {
        "label": "Becekku",
        "prefix": "bq",
        "base": "https://becekku.club",
        "gate_url": "https://becekku.club/",
        "example": "/becekku bocil",
        "fallback_categories": (
            {"slug": "bokep-bocil", "name": "Bokep Bocil"},
            {"slug": "indo-viral", "name": "Indo Viral"},
            {"slug": "chindo-viral", "name": "Chindo Viral"},
            {"slug": "bokep-hijab", "name": "Bokep Hijab"},
        ),
    },
    "lendirqu": {
        "label": "LendirQu",
        "prefix": "lq",
        "base": "https://lendirqu.stream",
        "gate_url": "https://lendirqu.stream/",
        "example": "/lendirqu bokep sma",
        "fallback_categories": (
            {"slug": "bokep-bocil", "name": "Bokep Bocil"},
            {"slug": "bokep-chindo", "name": "Bokep Chindo"},
            {"slug": "bokep-abg", "name": "Bokep ABG"},
            {"slug": "bokep-indo", "name": "Bokep Indo"},
            {"slug": "bokep-viral", "name": "Bokep Viral"},
            {"slug": "bokep-colmek", "name": "Bokep Colmek"},
            {"slug": "bokep-hijab", "name": "Bokep Hijab"},
            {"slug": "bokep-malay", "name": "Bokep Malay"},
        ),
    },
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Hasil listing per halaman Telegram.
PER_PAGE = 5
# Picker kategori & tag cloud.
CATS_PER_PAGE = 10
TAGS_PER_PAGE = 20
# Batas keras hasil search / kategori / tag / filter.
MAX_RESULTS = 100
# Umur cache sesi (detik) + batas entri.
CACHE_TTL = 3600
MAX_CACHE_ENTRIES = 120
# Umur cache daftar kategori & tag cloud.
INDEX_TTL = 3600
# Timeout HTTP halaman listing.
HTTP_TIMEOUT = 25
