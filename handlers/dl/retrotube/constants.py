import os

DEBUG_RETROTUBE = os.getenv("RETROTUBE_DEBUG", "0").strip().lower() in ("1", "true", "on", "yes")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

RETROTUBE_DOMAINS = (
    "bokepcrot.gives",
    "bokepcrot.land",
    "bokepcrot.quest",
    "bokepcrot.com",
    "bokepcrot.net",
    "bokepcrot.xyz",
    "lendirqu.stream",
    "lendirqu.wtf",
    "lendirqu.com",
    "bokepindoh.design",
    "bokepindoh.xxx",
    "bokepinfo.today",
    "bokepinfo.info",
    "indobocil.com",
    "lendirqu.surf",
    "bocilterbaru.surf",
    "rajabocil.surf",
    "abgindoterbaru.com",
    "kangencoli.com",
    "ksatriabokep.com",
    "pemburubokep.com",
    "becekku.live",
    "lordbokep.com",
    "bokepnoz.co",
    "bokepbrut.co",
    "bokepcluk.com",
    "bokepjret.net",
    "bokeplik.com",
    "bokeplot.com",
    "bokepmun.com",
    "bokeprit.in",
    "bokepsut.in",
    "bokeptod.pro",
    "bokepud.in",
    "growbokep.co",
    # Domain baru
    "videobokep.vip",
    "bokepindonesia.me",
    "viralbocil.lol",
)

# Host yang embed-nya didukung khusus. Diprioritaskan.
_PREFERRED_HOSTS = (
    "lulust.com",
    "lulustream.com",
    "luluvdo.com",
    "luluvid.com",
    "mumu.watch",
    "voe.sx",
    "jeremyparticipantanything.com",
    "miaw.lol",
    "lordfile.site",
    "nozstream.site",
    "colistream.site",
    "jretfile.site",
    "likstream.site",
    "filendung.site",
    "munfile.site",
    "ritfile.site",
    "sutfile.site",
    "ngicstream.site",
    "domfile.site",
    "growfile.site",
    "fbplay.vip",
    "nontonvideo.xyz",
)

# Host/file yang jelas-jelas bukan video asli (decoy), di-skip.
_DECOY_HOSTS = ("test-videos.co.uk",)

# Grup domain mirror: situs yang sama di beberapa domain (isi post & path identik)
_MIRROR_GROUPS = (
    ("lendirqu.stream", "lendirqu.wtf", "lendirqu.com"),
    (
        "bokepcrot.gives",
        "bokepcrot.land",
        "bokepcrot.quest",
        "bokepcrot.com",
        "bokepcrot.net",
        "bokepcrot.xyz",
    ),
    ("bokepindoh.design", "bokepindoh.xxx"),
    ("bokepinfo.today", "bokepinfo.info"),
    (
        "indobocil.com",
        "lendirqu.surf",
        "bocilterbaru.surf",
        "rajabocil.surf",
        "abgindoterbaru.com",
        "kangencoli.com",
        "ksatriabokep.com",
        "pemburubokep.com",
    ),
)

_SEG_CONCURRENCY = int(os.getenv("RETROTUBE_SEG_CONCURRENCY", "5"))
_SEG_RETRIES = int(os.getenv("RETROTUBE_SEG_RETRIES", "3"))
_HTTP_TIMEOUT = int(os.getenv("RETROTUBE_HTTP_TIMEOUT", "30"))
_FFMPEG_TIMEOUT = int(os.getenv("RETROTUBE_FFMPEG_TIMEOUT", "300"))

_FAST_INTERVAL = float(os.getenv("RETROTUBE_FAST_INTERVAL", "5"))
_SLOW_INTERVAL = float(os.getenv("RETROTUBE_SLOW_INTERVAL", "10"))
_FAST_SPEED_BPS = float(os.getenv("RETROTUBE_FAST_SPEED_BPS", "1000000"))  # 1 MB/s
