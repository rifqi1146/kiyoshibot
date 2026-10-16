LABEL = "Anichin"
PREFIX = "ac"
BASE_URL = "https://anichin.moe"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PER_PAGE = 5
GENRES_PER_PAGE = 10
AZ_PER_PAGE = 10
EPISODES_PER_PAGE = 15
MAX_RESULTS = 100
CACHE_TTL = 3600
MAX_CACHE_ENTRIES = 120
INDEX_TTL = 3600
HTTP_TIMEOUT = 25

BROWSE = {
    "list": ("Donghua List", "/anime", True),
    "ongoing": ("Ongoing", "/ongoing", True),
    "completed": ("Completed", "/completed", True),
    "upcoming": ("Upcoming", "/upcoming-donghua", False),
    "drop": ("Drop", "/drop", False),
}

SCHEDULE_DAYS = ("Senin", "Selasa", "Rabu", "Kamis", "Jum'at", "Sabtu", "Minggu", "Acak")

DAY_EN_MAP = {
    "senin": "Monday",
    "selasa": "Tuesday",
    "rabu": "Wednesday",
    "kamis": "Thursday",
    "jum'at": "Friday",
    "jumat": "Friday",
    "sabtu": "Saturday",
    "minggu": "Sunday",
    "acak": "Random",
}
