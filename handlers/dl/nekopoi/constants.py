"""Konstanta scraper Nekopoi (nekopoi.care)

FLOW KONSTANTA
--------------
1. UA/_HTTP_TIMEOUT dipakai semua request curl_cffi (impersonate Chrome).
2. NEKOPOI_DOMAINS: host halaman post (auto-detect link).
3. EMBED_HOSTS: host iframe pemutar yang didukung (streampoi dulu, playmogo belum).
4. HLS tuning: jumlah worker segmen, retry, timeout, ambang paralel.
5. Subtitle tag untuk sufix varian HLS (_l/_n/_h/_o).
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 45

# Streampoi membangkitkan token `i=<ip>` dari alamat IP yang MELIHAT embed.
# CDN streamruby.net hanya punya record A (IPv4) di beberapa node -> kalau
# request embed keluar via IPv6 tetapi request playlist/CDN dipaksa IPv4,
# token `i=` tidak cocok dengan IP koneksi -> 403 Forbidden.
# Karena itu SEMUA request (post, embed, playlist, segmen) dikunci ke IPv4
# supaya token & koneksi selalu satu alamat IP.
FORCE_IPV4 = os.getenv("NEKOPOI_FORCE_IPV4", "1").strip().lower() in ("1", "true", "on", "yes")

DEBUG_NEKOPOI = os.getenv("NEKOPOI_DEBUG", "0").strip().lower() in ("1", "true", "on", "yes")

# Host halaman post Nekopoi (juga dipakai AUTO_DOWNLOAD_DOMAINS).
NEKOPOI_HOSTS = ("nekopoi.care", "nekopoi.best", "nekopoi.care")

# Host embed yang bisa di-unpack jadi master.m3u8 tanpa browser.
EMBED_HOST_STREAMPOI = ("streampoi.com", "streamruby.com", "streamruby.net")
# playmogo/DoodStream butuh Turnstile interaktif -> BELUM didukung (dibiarkan sebagai
# kandidat supaya mudah dinyalakan kalau solver-nya sudah ada).
EMBED_HOST_DOOD = ("playmogo.com", "doodstream.com", "dood.", "ds2play.com")

# Progress HLS.
SEG_CONCURRENCY = int(os.getenv("NEKOPOI_SEG_CONCURRENCY", "6"))
SEG_RETRIES = int(os.getenv("NEKOPOI_SEG_RETRIES", "3"))
SEG_TIMEOUT = float(os.getenv("NEKOPOI_SEG_TIMEOUT", "60"))
# Progress HLS: interval edit pesan ADAPTIF (hindari 429 flood Telegram).
# interval = clamp(estimasi_durasi_file / PROGRESS_TARGET_EDITS, min, max)
# -> makin besar ukuran file & makin cepat speed, edit makin jarang;
#    total edit seluruh unduhan dibatasi sekitar PROGRESS_TARGET_EDITS.
PROGRESS_POLL = float(os.getenv("NEKOPOI_PROGRESS_POLL", "0.7"))
PROGRESS_MIN_INTERVAL = float(os.getenv("NEKOPOI_PROGRESS_MIN_INTERVAL", "5"))
PROGRESS_MAX_INTERVAL = float(os.getenv("NEKOPOI_PROGRESS_MAX_INTERVAL", "30"))
PROGRESS_TARGET_EDITS = float(os.getenv("NEKOPOI_PROGRESS_TARGET_EDITS", "15"))
FFMPEG_TIMEOUT = float(os.getenv("NEKOPOI_FFMPEG_TIMEOUT", "1800"))

# Sufix varian HLS -> label default (dipakai saat RESOLUTION= tidak ada di playlist).
VARIANT_LABEL = {
    "l": 360,
    "n": 480,
    "h": 720,
    "o": 720,
    "v": 1080,
}
