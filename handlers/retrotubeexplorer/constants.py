
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

PER_PAGE = 5
CATS_PER_PAGE = 10
TAGS_PER_PAGE = 20
MAX_RESULTS = 100
CACHE_TTL = 3600
MAX_CACHE_ENTRIES = 120
INDEX_TTL = 3600
HTTP_TIMEOUT = 25
