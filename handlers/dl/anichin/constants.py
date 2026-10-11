"""Konstanta downloader Anichin (anichin.moe)

FLOW KONSTANTA
--------------
1. `ANICHIN_HOSTS`: host halaman post Anichin (deteksi URL & routing).
2. `SHORTENER_HOSTS`: shortener di `div.soraurlx` (bit.ly, inshorturl.in,
   lifehei.my.id) yang harus di-resolve dulu sebelum sampai `mirrored.to`.
3. `MIRRORED_HOSTS`: halaman multilinks / files `mirrored.to`.
4. `HOST_PRIORITY`: urutan preferensi host file setelah `/out_url` di-resolve.
   GoogleDrive didahulukan (paling stabil + sudah ada `_download_gdrive`),
   lalu Buzzheavier (CDN langsung), baru sisanya.
5. `ARIA2_CONNS`/`PROGRESS_INTERVAL`: tuning unduhan.
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("ANICHIN_HTTP_TIMEOUT", "30"))

ANICHIN_HOSTS = ("anichin.moe", "www.anichin.moe")

BASE_URL = "https://anichin.moe"

# Shortener pada `div.soraurlx` (label resolusi -> link pendek).
CAPTCHA_SHORTENER_HOSTS = (
    "clk.asia",
    "dekhe.click",
    "clicksfly.com",
)

# Shortener pada `div.soraurlx` (label resolusi -> link pendek).
SHORTENER_HOSTS = (
    "bit.ly",
    "inshorturl.in",
    "lifehei.my.id",
    "www.lifehei.my.id",
) + CAPTCHA_SHORTENER_HOSTS

# Timeout khusus resolusi embed VidHide/StreamWish. CDN mereka sering tidak
# responsif; tanpa batas ini probe bisa menggantung ~30s saat OK.ru tidak ada.
VIDHIDE_TIMEOUT = float(os.getenv("ANICHIN_VIDHIDE_TIMEOUT", "15"))

# Halaman perantara `mirrored.to`.
MIRRORED_HOSTS = ("mirrored.to", "www.mirrored.to")

# Host embed keluarga VidHide/StreamWish (packer Dean Edwards -> `master.m3u8`,
# segmen `.ts` tanpa enkripsi). Dipakai sebagai fallback KEDUA setelah OK.ru.
VIDHIDE_HOSTS = (
    "vidhide", "vidhidepre", "vidhidepro", "vidhidevip", "streamwish", "strwish",
    "streamhg", "morencius", "callistanise", "minochinos", "ryderjet", "rubystm",
    "rubyvidhub", "filelions", "filemoon", "vidspeeder", "cloudyup", "dood.",
    "playmogo", "dsvplay", "earnvids", "embedrise", "luluvid", "doodstream",
    "ahvsh", "streamhide", "sbrapid", "streamsb", "turbovidhls", "playerwish",
    "myvidplay", "dhtpre", "rubyvid", "abyssplayer", "listeamed",
)

# Preferensi host file setelah POST /out_url. Nama host diambil dari `alt` ikon
# di tabel `/mirstats.php` (DoodStream, GoFileIo, StreamWish, SendNow, TurboBit,
# MegaupNet, Buzzheavier, GoogleDrive, FilesDLi).
HOST_PRIORITY = (
    "GoogleDrive",
    "Buzzheavier",
    "StreamWish",
    "GoFileIo",
    "SendNow",
    "MegaupNet",
    "TurboBit",
    "DoodStream",
)

ARIA2_CONNS = int(os.getenv("ANICHIN_ARIA2_CONNS", "16"))
PROGRESS_INTERVAL = float(os.getenv("ANICHIN_PROGRESS_INTERVAL", "5.0"))

# Batas jumlah host mirror yang dicoba sebelum menyerah dan pindah ke server
# streaming. mirrored.to sering menyiapkan ~14 host tetapi hampir semuanya mati
# (404 / HTML / TLS rusak); mencoba semuanya memakan ~15s dan membanjiri log.
# Host prioritas (HOST_PRIORITY) sudah di urutan depan, jadi cukup beberapa.
MIRROR_MAX_ATTEMPTS = int(os.getenv("ANICHIN_MIRROR_ATTEMPTS", "5"))

# Batas jumlah polling `/mirstats.php` saat mirrored.to menyiapkan link host.
MIRSTATS_MAX_POLLS = int(os.getenv("ANICHIN_MIRSTATS_POLLS", "8"))
MIRSTATS_POLL_DELAY = float(os.getenv("ANICHIN_MIRSTATS_DELAY", "3.0"))
