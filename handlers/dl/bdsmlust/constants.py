"""Konstanta scraper BDSMLust (bdsmlust.com)

FLOW KONSTANTA
--------------
1. UA & timeout untuk request curl_cffi (impersonate Chrome).
2. Host BDSMLust untuk deteksi URL dan routing download.
3. Dua tipe embed yang dipakai halaman post:
   - `bdsmstreak.com/embed/<id>` -> `<source src>` langsung (CDN mespeaks).
   - `vid-bx.com/embed/<id>` -> redirect ke `bdsmx.tube/embed/<id>` (SPA KVS)
     -> API `/api/videofile.php` -> path `/get_file/...` di `bdsmx.tube`
     -> redirect berantai ke CDN ahcdn.
4. Concurrency aria2c + interval progress edit.
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("BDSMLUST_HTTP_TIMEOUT", "30"))
HTTP_ATTEMPTS = int(os.getenv("BDSMLUST_HTTP_ATTEMPTS", "3"))

BDSMLUST_HOSTS = ("bdsmlust.com", "www.bdsmlust.com")

# Path statis yang BUKAN post video (harus ditolak oleh is_bdsmlust_url)
STATIC_PATH_PREFIXES = (
    "search", "categories", "bdsm-pornstars", "bdsm-studios",
    "kink-channels", "bdsm-tags", "bdsm-guides", "bdsm-glossary",
    "bdsm-test", "what-is-bdsm", "category", "tag", "page",
    "partners", "contact", "2257-2",
)

# Host embed -> keterangan jalur unduh
EMBED_HOST_BDSMSTREAK = "bdsmstreak.com"      # <source src> langsung
EMBED_HOST_VIDBX = "vid-bx.com"               # -> bdsmx.tube (KVS)
EMBED_HOST_BDSMX = "bdsmx.tube"

# Origin yang dipakai sebagai Referer pada request API / download KVS
BDSMX_ORIGIN = "https://bdsmx.tube"

BDSMLUST_ARIA2_CONNS = int(os.getenv("BDSMLUST_ARIA2_CONNS", "16"))

BDSMLUST_PROGRESS_INTERVAL = float(os.getenv("BDSMLUST_PROGRESS_INTERVAL", "2.5"))
