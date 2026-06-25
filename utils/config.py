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
    
# Bot token
BOT_TOKEN = require_env("BOT_TOKEN")

# owner id
OWNER_ID = require_env_list("BOT_OWNER_ID")

# log & asupan startup
LOG_CHAT_ID = require_env("LOG_CHAT_ID", int)

# force join
SUPPORT_CHANNEL_ID = os.getenv("SUPPORT_CH_ID")
SUPPORT_CHANNEL_LINK = os.getenv("SUPPORT_CH_LINK")

# gsearch & gemini
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# groq & caca
GROQ_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_TIMEOUT = int(os.getenv("GROQ_TIMEOUT", "30"))
COOLDOWN = int(os.getenv("GROQ_COOLDOWN", "2"))


# donate
DONATE_URL = os.getenv("DONATE_URL")

# fonts
FONT_DIR = os.getenv("FONT_DIR")

#Cloudflare
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_AUTH_TOKEN = os.getenv("CLOUDFLARE_AUTH_TOKEN", "").strip()
CLOUDFLARE_MODEL = "@cf/moonshotai/kimi-k2.6"

#Neoxr api
NEOXR_API_KEY = os.getenv("NEOXR_API_KEY")