import os

DEBUG_RETROTUBE = os.getenv("RETROTUBE_DEBUG", "0").strip().lower() in ("1", "true", "on", "yes")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

RETROTUBE_DOMAINS = (
    "bokepcrot.*",
    "lendirqu.*",
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
    "becekku.*",
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

def _host_match(host: str, domain: str) -> bool:
    host = (host or "").lower().split(":", 1)[0]
    domain = (domain or "").lower()
    if not host or not domain:
        return False
    if host.startswith("www."):
        host = host[4:]
    # Wildcard TLD: "lendirqu.*" cocok untuk lendirqu.pics, lendirqu.hair, dst.
    if domain.endswith(".*"):
        base = domain[:-1]  # "lendirqu."
        return host.startswith(base) and "." not in host[len(base):]
    return host == domain or host.endswith("." + domain)


def is_retrotube_domain(host: str) -> bool:
    return any(_host_match(host, d) for d in RETROTUBE_DOMAINS)


def mirror_domains_for(host: str) -> tuple:
    """Kembalikan daftar domain dalam grup mirror yang sama (termasuk domain asal)."""
    host = (host or "").lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    for group in _MIRROR_GROUPS:
        if any(_host_match(host, g) for g in group):
            return group
    return ()


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
    ("lendirqu.*",),
    ("bokepcrot.*",),
    ("becekku.*",),
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

_MIRROR_EXTRA_HOSTS = {
    "lendirqu.*": ("lendirqu.stream", "lendirqu.wtf", "lendirqu.com", "lendirqu.pics", "lendirqu.hair"),
    "bokepcrot.*": ("bokepcrot.gives", "bokepcrot.land", "bokepcrot.quest", "bokepcrot.com", "bokepcrot.net", "bokepcrot.xyz"),
    "becekku.*": ("becekku.live", "becekku.club"),
}

_SEG_CONCURRENCY = int(os.getenv("RETROTUBE_SEG_CONCURRENCY", "10"))
_SEG_RETRIES = int(os.getenv("RETROTUBE_SEG_RETRIES", "3"))
_HTTP_TIMEOUT = int(os.getenv("RETROTUBE_HTTP_TIMEOUT", "30"))
_FFMPEG_TIMEOUT = int(os.getenv("RETROTUBE_FFMPEG_TIMEOUT", "300"))

_FAST_INTERVAL = float(os.getenv("RETROTUBE_FAST_INTERVAL", "5"))
_SLOW_INTERVAL = float(os.getenv("RETROTUBE_SLOW_INTERVAL", "10"))
_FAST_SPEED_BPS = float(os.getenv("RETROTUBE_FAST_SPEED_BPS", "1000000"))  # 1 MB/s
