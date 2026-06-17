import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_PATH = os.path.join(BASE_DIR, "..", "..", "data", "cookies.txt")

TMP_DIR = "downloads"
AUTO_DL_DB = "data/auto_dl.sqlite3"

MAX_TG_SIZE = 1999 * 1024 * 1024

DL_FORMATS = {
    "video": {"label": "Video"},
    "mp3": {"label": "MP3"},
}

PREMIUM_ONLY_DOMAINS = {
    "pornhub.com",
    "xnxx.com",
    "redtube.com",
    "bdsmstreak.com",
    "xvideos.com",
    "vjav.com",
    "japanhdv.com",
    "youporn.com",
    "eporner.com",
    "xhamster.com",
    "xhamster.com",
    "japaneseporn.xxx",
    "xhsocial.com",
}

AUTO_DOWNLOAD_DOMAINS = {
    "music.youtube.com",
    "tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "instagram.com",
    "capcut.com"
    "instagr.am",
    "facebook.com",
    "fb.watch",
    "fb.com",
    "m.facebook.com",
    "twitter.com",
    "x.com",
    "reddit.com",
    "redd.it",
    "pornhub.com",
    "xnxx.com",
    "pixiv.net",
    "bdsmstreak.com",
    "xhamster.com",
    "threads.net",
    "pinterest.com",
    "pin.it",
    "threads.com",
    "redtube.com",
    "xvideos.com",
    "vjav.com",
    "japanhdv.com",
    "youporn.com",
    "eporner.com",
    "japaneseporn.xxx",
    "xhsocial.com",
    "youtube.com",
    "youtu.be",
}