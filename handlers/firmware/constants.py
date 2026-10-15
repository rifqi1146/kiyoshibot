"""Constants for Xiaomi Firmware & ROM Explorer (/firmware).

Sumber data: `https://xmfirmwareupdater.com` (XM Firmware Updater).
Mencakup Official ROM lengkap (HyperOS + MIUI):
- Format Recovery (.zip) & Fastboot (.tgz)
- Seluruh Region resmi: Indonesia (ID), Global (MI), EEA Europe (EU),
  China (CN), India (IN), Russia (RU), Turkey (TR), Taiwan (TW), Japan (JP).
- Caching & mirror CDN resmi (BigOTA, CDN Org, Aliyun SG, HugeOTA, BN).
"""

LABEL = "Xiaomi Firmware & ROM"
PREFIX = "fw"

BASE_URL = "https://xmfirmwareupdater.com"
DEVICES_URL = f"{BASE_URL}/data/firmware_devices.yml"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 25
# Jumlah firmware/ROM per halaman Telegram.
PER_PAGE = 20
# Batas maksimum ROM yang dikumpulkan per device.
MAX_RESULTS = 200
# Maksimum tombol device yang ditawarkan saat pencarian ambigu.
MAX_DEVICE_MATCHES = 20
CACHE_TTL = 3600
DEVICES_TTL = 24 * 3600
DETAIL_TTL = 3600

# ---------------------------------------------------------------------------
# Filter region resmi Xiaomi (teks bersih, tanpa emote di tombol)
# ---------------------------------------------------------------------------
REGIONS: dict[str, tuple[str, str]] = {
    "indonesia": ("Indonesia", "indonesia"),
    "global":    ("Global", "global"),
    "europe":    ("Europe (EEA)", "europe"),
    "china":     ("China", "china"),
    "india":     ("India", "india"),
    "russia":    ("Russia", "russia"),
    "turkey":    ("Turkey", "turkey"),
    "taiwan":    ("Taiwan", "taiwan"),
    "japan":     ("Japan", "japan"),
}

# Device populer sebagai tombol pintas di menu (teks bersih).
POPULAR_DEVICES: tuple[tuple[str, str], ...] = (
    ("munch", "POCO F4"),
    ("alioth", "POCO F3"),
    ("ingres", "POCO F4 GT"),
    ("duchamp", "POCO X6 Pro"),
    ("vayu", "POCO X3 Pro"),
    ("surya", "POCO X3 NFC"),
    ("rosemary", "Redmi Note 10S"),
    ("sweet", "Redmi Note 10 Pro"),
    ("topaz", "Redmi Note 12"),
    ("garnet", "Redmi Note 13 Pro"),
)
