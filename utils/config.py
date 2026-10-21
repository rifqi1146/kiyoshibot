import os
from dotenv import load_dotenv

load_dotenv()

def require_env(name: str, cast=str):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    try:
        return cast(value)
    except Exception:
        raise RuntimeError(f"Environment variable {name} must be {cast.__name__}")
        
def require_env_list(key: str) -> set[int]:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing env: {key}")
    return {
        int(x)
        for x in val.split(",")
        if x.strip().isdigit()
    }
    
BOT_TOKEN = require_env("BOT_TOKEN")

OWNER_ID = require_env_list("BOT_OWNER_ID")

LOG_CHAT_ID = require_env("LOG_CHAT_ID", int)

SUPPORT_CHANNEL_ID = os.getenv("SUPPORT_CH_ID")
SUPPORT_CHANNEL_LINK = os.getenv("SUPPORT_CH_LINK")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")
JINA_API_KEY = os.getenv("JINA_API_KEY", "").strip()
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "").strip()
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "firecrawl").strip().lower()
SEARCH_DEPTH = os.getenv("SEARCH_DEPTH", "fast").strip().lower()

GROQ_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_TIMEOUT = int(os.getenv("GROQ_TIMEOUT", "30"))
COOLDOWN = int(os.getenv("GROQ_COOLDOWN", "2"))


DONATE_URL = os.getenv("DONATE_URL")

NASIONALPAY_BASE_URL = os.getenv("NASIONALPAY_BASE_URL", "https://merchant.nasionalpay.com").strip().rstrip("/")
NASIONALPAY_STORE_KEY = os.getenv("NASIONALPAY_STORE_KEY", "").strip()

PREMIUM_PRICE = int(os.getenv("PREMIUM_PRICE", os.getenv("AXIOM_PREMIUM_AMOUNT", "20000")))

FONT_DIR = os.getenv("FONT_DIR")

CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_AUTH_TOKEN = os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
CLOUDFLARE_MODEL = "@cf/nvidia/nemotron-3-120b-a12b"

NEOXR_API_KEY = os.getenv("NEOXR_API_KEY")