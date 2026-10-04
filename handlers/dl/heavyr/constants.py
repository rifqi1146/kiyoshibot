"""Konstanta scraper Heavy-R (heavy-r.com)

FLOW KONSTANTA
--------------
1. UA & timeout untuk request curl_cffi (impersonate Chrome).
2. Host Heavy-R untuk deteksi URL dan routing download.
3. Sumber video:
   - Direct MP4 di `a-cdn` / `b-cdn.heavy-r.com` (Referer wajib).
   - HLS single-file di `nl.object-storage.io/hr-vids/hls/...` / `hr2-vids/hls/...`
     (playlist media langsung tanpa STREAM-INF; semua segmen = SATU file `.m4s`).
4. Concurrency aria2c (+ interval progress edit).
5. Alur pos embed berbahaya:
   - `embed.heavy-r.com/embed/<id>` untuk ID yang sudah mati TIDAK 404 —
     ia merender video random lain (HTTP 200). Validasi wajib: anchor
     `flow-title` harus menunjuk `/video/<id>/` dengan id yang SAMA.
     Kalau mismatch -> ID mati -> tolak (FileNotFoundError).
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("HEAVYR_HTTP_TIMEOUT", "30"))
HTTP_ATTEMPTS = int(os.getenv("HEAVYR_HTTP_ATTEMPTS", "3"))

# Host yang dianggap milik heavy-r (host halaman, bukan CDN a-cdn/b-cdn).
HEAVYR_HOSTS = ("heavy-r.com", "www.heavy-r.com", "embed.heavy-r.com")

# Origin embed untuk resolve MP4 dari post yang menyajikan HLS.
HEAVYR_EMBED_ORIGIN = "https://embed.heavy-r.com"

# Referer yang diterima CDN a-cdn/b-cdn saat download MP4.
HEAVYR_REFERER = "https://www.heavy-r.com/"

HEAVYR_ARIA2_CONNS = int(os.getenv("HEAVYR_ARIA2_CONNS", "16"))

HEAVYR_PROGRESS_INTERVAL = float(os.getenv("HEAVYR_PROGRESS_INTERVAL", "2.5"))
