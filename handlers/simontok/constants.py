"""Konstanta explorer Simontok (`/simontok`).

FLOW KONSTANTA
--------------
1. Label, prefix callback `sm`, base URL dan gate URL.
2. `SECTIONS`: seksi kurasi (Latest, Trending, VIP, Indo, JAV, Semi, Hentai).
3. `FALLBACK_CATEGORIES`: 16 kategori unggulan beranda simontok.study.
4. Paginasi: `PER_PAGE=5`, `CATS_PER_PAGE=10`, `TAGS_PER_PAGE=20`.
5. Cache TTL & limits.
"""

LABEL = "Simontok"
PREFIX = "sm"
BASE_URL = "https://simontok.study"
GATE_URL = "https://simontok.study/"
EXAMPLE = "/simontok hijab"

SECTIONS = {
    "terbaru": ("Latest Videos", f"{BASE_URL}/terbaru/"),
    "trending": ("Trending Videos", f"{BASE_URL}/trending/"),
    "vip": ("VIP 18+", f"{BASE_URL}/vip/"),
    "indo": ("Indo Viral", f"{BASE_URL}/indo/"),
    "jav": ("JAV Sub Indo", f"{BASE_URL}/jav/"),
    "semi": ("Film Semi", f"{BASE_URL}/semi/"),
    "hentai": ("Hentai", f"{BASE_URL}/hentai/"),
}

FALLBACK_CATEGORIES = (
    {"slug": "bokep-indo", "name": "Bokep Indo", "count": "58k"},
    {"slug": "indo-viral", "name": "Indo Viral", "count": "45k"},
    {"slug": "bokep-jilbab", "name": "Bokep Jilbab", "count": "24k"},
    {"slug": "bokep-hotel", "name": "Bokep Hotel", "count": "22k"},
    {"slug": "bokep-viral", "name": "Bokep Viral", "count": "18k"},
    {"slug": "video-bugil", "name": "Video Bugil", "count": "18k"},
    {"slug": "bokep-semok", "name": "Bokep Semok", "count": "18k"},
    {"slug": "bokep-skandal", "name": "Bokep Skandal", "count": "18k"},
    {"slug": "bokep-pov", "name": "Bokep POV", "count": "18k"},
    {"slug": "video-colmek", "name": "Video Colmek", "count": "17k"},
    {"slug": "bokep-onlyfans", "name": "Bokep Onlyfans", "count": "17k"},
    {"slug": "bokep-sedarah", "name": "Bokep Sedarah", "count": "17k"},
    {"slug": "bokep-selingkuh", "name": "Bokep Selingkuh", "count": "17k"},
    {"slug": "bokep-mahasiswi", "name": "Bokep Mahasiswi", "count": "16k"},
    {"slug": "bokep-cosplay", "name": "Bokep Cosplay", "count": "16k"},
    {"slug": "bokep-cindo", "name": "Bokep Cindo", "count": "16k"},
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PER_PAGE = 5
CATS_PER_PAGE = 10
TAGS_PER_PAGE = 20

MAX_RESULTS = 100
PER_SERVER_PAGE = 24

CACHE_TTL = 3600
MAX_CACHE_ENTRIES = 120
INDEX_TTL = 3600
HTTP_TIMEOUT = 25
EMPTY_NOTICE = "Tidak ada hasil"
