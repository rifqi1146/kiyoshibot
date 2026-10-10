"""Konstanta scraper Drakor.id (drakorid.co)

FLOW KONSTANTA
--------------
1. UA & timeout untuk request curl_cffi (impersonate Chrome).
2. Host Drakor.id untuk deteksi URL dan routing download.
3. Endpoint situs:
   - Detail drama : `GET /nonton/<slug>/` (judul, poster, episode list).
   - Halaman unduh: `GET /download-streaming/<slug>/<episode>` (direct MP4 360p/480p/720p).
   - Halaman tonton: `GET /watch-streaming/<slug>/<episode>` (player HLS).
   - API episode  : `POST /myapi/episode_detail.php` (token + id + episode -> id stream).
4. Resolver CDN: `http://admin.drakor.la/go/files/<stream_id>` -> 302 ke
   `http://<node>.drakor.cc/files/<hash>.mp4`. Node yang sama juga melayani
   varian `/360p/` dan `/480p/` dari nama file yang sama.
5. Concurrency aria2c (+ interval progress edit).
"""
import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("DRAKORID_HTTP_TIMEOUT", "30"))

DRAKORID_HOSTS = ("drakorid.co", "www.drakorid.co")

BASE_URL = "https://drakorid.co"
CDN_RESOLVER = "http://admin.drakor.la/go/files"

DRAKORID_ARIA2_CONNS = int(os.getenv("DRAKORID_ARIA2_CONNS", "16"))

DRAKORID_PROGRESS_INTERVAL = float(os.getenv("DRAKORID_PROGRESS_INTERVAL", "2.5"))

# Resolusi yang dicoba dari node CDN (nama file sama, folder berbeda).
# `files` = master 720p (terverifikasi 1280x720). `720p` folder dicoba duluan kalau ada.
CDN_QUALITY_DIRS = ("files", "720p", "480p", "360p")
