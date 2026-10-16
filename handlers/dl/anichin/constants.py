import os

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = int(os.getenv("ANICHIN_HTTP_TIMEOUT", "30"))

ANICHIN_HOSTS = ("anichin.moe", "www.anichin.moe")

BASE_URL = "https://anichin.moe"

CAPTCHA_SHORTENER_HOSTS = (
    "clk.asia",
    "dekhe.click",
    "clicksfly.com",
)

SHORTENER_HOSTS = (
    "bit.ly",
    "inshorturl.in",
    "lifehei.my.id",
    "www.lifehei.my.id",
) + CAPTCHA_SHORTENER_HOSTS

VIDHIDE_TIMEOUT = float(os.getenv("ANICHIN_VIDHIDE_TIMEOUT", "15"))

MIRRORED_HOSTS = ("mirrored.to", "www.mirrored.to")

VIDHIDE_HOSTS = (
    "vidhide", "vidhidepre", "vidhidepro", "vidhidevip", "streamwish", "strwish",
    "streamhg", "morencius", "callistanise", "minochinos", "ryderjet", "rubystm",
    "rubyvidhub", "filelions", "filemoon", "vidspeeder", "cloudyup", "dood.",
    "playmogo", "dsvplay", "earnvids", "embedrise", "luluvid", "doodstream",
    "ahvsh", "streamhide", "sbrapid", "streamsb", "turbovidhls", "playerwish",
    "myvidplay", "dhtpre", "rubyvid", "abyssplayer", "listeamed",
)

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

MIRROR_MAX_ATTEMPTS = int(os.getenv("ANICHIN_MIRROR_ATTEMPTS", "5"))

MIRSTATS_MAX_POLLS = int(os.getenv("ANICHIN_MIRSTATS_POLLS", "8"))
MIRSTATS_POLL_DELAY = float(os.getenv("ANICHIN_MIRSTATS_DELAY", "3.0"))
