
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
PER_PAGE = 20
MAX_RESULTS = 200
MAX_DEVICE_MATCHES = 20
CACHE_TTL = 3600
DEVICES_TTL = 24 * 3600
DETAIL_TTL = 3600

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
