[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-22.5-blue?logo=telegram)](https://github.com/python-telegram-bot/python-telegram-bot)
[![License](https://img.shields.io/badge/License-GPLv3-green)](https://www.gnu.org/licenses/gpl-3.0.html)

# Telegram Multi-Function Bot

A multi-function Telegram bot built with Python and `python-telegram-bot`, providing AI features, downloader utilities, moderation tools, networking commands, and additional group management features.

## Features

- AI chat and assistant commands
- Media downloader for multiple platforms
- QRIS payment gateway integration (`/buypremium` via Axiom Payment)
- Google search integration
- Networking and utility tools
- Moderation and administration features
- Group and verification features
- Entertainment and miscellaneous commands

## Project Architecture & Directory Structure

The project follows a modular, decoupled architecture where download engines remain strictly headless and isolated from the user-facing bot interfaces:

```
kiyoshibot/
├── main.py                     # Supervisor launcher (manages local telegram-bot-api & bot.py)
├── bot.py                      # Telegram application lifecycle, uvloop init & handler groups
│
├── database/                   # SQLite persistence layer (WAL mode, connection pooling)
│   ├── db.py                   # Thread-safe pooled connection helper with busy_timeout=30s
│   ├── download_db.py          # Download counters, quota checks & premium status store
│   ├── payment_db.py           # Axiom QRIS invoices, payment status & transaction history
│   ├── moderation_db.py        # Group moderation configs, warned users & mute states
│   ├── caca_db.py              # Persona state, conversation context & user AI memory
│   └── user_settings_db.py     # Per-user preferences (silent downloads, format picks, etc.)
│
├── handlers/                   # Command handlers, callback dispatchers & interactive UIs
│   ├── commands.py             # Command dispatcher (supports list/string command aliases)
│   ├── callbacks.py            # Central callback query regex router
│   ├── messages.py             # Text message recorder, AI reply router & quotly buffer
│   │
│   ├── dl/                     # Headless download engines, stream scrapers & decrypters
│   │   ├── router.py           # Link detector, rate-limits, format pickers & worker pool
│   │   ├── service.py          # Download coordinator, ffmpeg remuxer & media dispatch
│   │   ├── progress.py         # Unified transfer stats, adaptive ETA & status edit throttling
│   │   ├── stages.py           # Staged status messages (metadata -> picker -> progress)
│   │   ├── keyboards.py        # Format, resolution, engine & slideshow inline keyboards
│   │   ├── state.py            # Shared DL_CACHE / DL_LIMIT_CACHE runtime registry
│   │   ├── constants.py        # Premium-only / auto-download domain lists & tuning
│   │   ├── utils.py            # URL normalization, ffprobe validation & URL extraction
│   │   ├── remux.py            # ffmpeg faststart remux & MP3 audio extraction pipeline
│   │   ├── ytdlp.py            # posix_spawn yt-dlp runner (immune to uvloop fork() SIGSEGV)
│   │   ├── aria2.py            # Multi-socket 16-connection aria2c download engine
│   │   ├── probe.py            # Per-site resolution prober & picker capability registry
│   │   ├── sweeper.py          # TTL-based disk cleanup & orphan browser-profile reaper
│   │   ├── mtproto_uploader.py # Telethon bot client for 2GB MTProto uploads
│   │   ├── pyrogram_uploader.py # Pyrofork uploader with multi-connection streaming
│   │   ├── moviebox/           # MovieBox direct stream resolver & signed CDN fetcher
│   │   ├── anichin/            # Donghua shortener resolver & multi-server stream fallback
│   │   ├── nekopoi/            # Streampoi Dean Edwards unpacker, ouo bypass & CDN direct
│   │   ├── drakorid/           # Drakor.id multi-resolution CDN scraper & auth session
│   │   ├── tiktok/             # Watermark stripper, slideshow renderer & direct scraper
│   │   ├── bunkr/              # Multi-tier Bunkr album scraper & parallel CDN downloader
│   │   ├── instagram/          # Instagram carousel, reels & story parallel downloader
│   │   ├── retrotube/          # VOE.sx DDoS-Guard resolver & WP-Script stream extractor
│   │   ├── simontok/           # AES-256-GCM player decryptor & HLS segment downloader
│   │   ├── cosxplay/           # CosXplay direct MP4 scraper
│   │   ├── darknessporn/       # DarknessPorn direct MP4 scraper
│   │   ├── femdomvc/           # FemdomVC aria2c scraper
│   │   ├── asiangirl/          # AsianGirl.porn AES-128 HLS parallel downloader
│   │   ├── bdsmlust/           # BDSMLust embed resolver (bdsmstreak / bdsmx)
│   │   ├── heavyr/             # Heavy-R HLS/direct MP4 resolver
│   │   ├── cosplaytele/        # Cosplaytele photo gallery & Cossora HLS extractor
│   │   ├── pawchive/           # Pawchive Kemono-style Patreon archive scraper
│   │   ├── punishworld/        # PunishWorld stream extractor
│   │   ├── youtube/            # YouTube video, Shorts & post downloader
│   │   ├── twitter/            # Twitter/X media downloader
│   │   ├── reddit/             # Reddit video & gallery downloader
│   │   ├── facebook/           # Facebook video downloader
│   │   ├── threads/            # Threads media downloader
│   │   └── pinterest/          # Pinterest video & image downloader
│   │
│   ├── moviebox/               # MovieBox Explorer UI (Bot API 10.3 Rich Message, 59 sections)
│   ├── anichin/                # Anichin Explorer UI (Schedule, Donghua list, Genres, A–Z)
│   ├── nekopoi/                # Nekopoi Explorer UI (Browse, JAV/Hentai, Genres, Episode grid)
│   ├── drakor/                 # Drakor.id Explorer UI (K-Drama/C-Drama browse, auto-login)
│   ├── retrotubeexplorer/      # BecekKu & LendirQu Explorer UI (Browse, categories, tags)
│   ├── simontok/               # Simontok Explorer UI (Bot API 10.3 Rich Message, browse, genres)
│   ├── asupan/                 # TikTok feed source (vendored web API scraper & signer)
│   ├── caca/                   # Multi-persona conversational AI engine (7 personas)
│   ├── proxy/                  # Local OpenAI-compatible proxy server & web grounding tools
│   ├── moderation/             # Group admin moderation, sudo privileges & temp kick/ban
│   ├── payment/                # /buypremium QRIS flow, invoice polling & premium activation
│   ├── stats/                  # System telemetry, memory diagnostics & hardware metrics
│   │
│   ├── quotly.py               # Multi-message sticker/image quote renderer
│   ├── manga.py                # Multi-source manga reader & in-memory PDF compiler
│   ├── music.py                # High-speed music search & multi-source audio downloader
│   ├── punishworldsearch.py    # /punish search command & download dispatcher
│   ├── gsearch.py              # Google Programmable Search with pagination
│   ├── welcome.py              # New member welcome, verification & CAPTCHA gate
│   ├── join.py                 # Force-join channel gate & realtime leave detection
│   ├── nsfw.py                 # Per-group NSFW toggle management
│   ├── premium.py              # Premium grant/revoke & cascade user reset
│   ├── premiumbenefit.py       # /premiumbenefit feature & domain benefit tables
│   ├── setting.py              # Per-user settings menu (silent download, format defaults)
│   ├── reload.py               # Hot module reload with dependency-ordered reimport
│   ├── restart.py              # In-place process restart via os.execv
│   ├── update.py               # Git pull & hot reload updater
│   ├── backup.py               # Database backup / restore / auto-backup scheduler
│   ├── broadcast.py            # Mass broadcast to users & groups
│   ├── blacklist.py            # Global blacklist gate (message + callback groups)
│   ├── cookies.py              # Owner-only Netscape cookies.txt uploader
│   ├── collector.py            # Group & user metadata collector (throttled)
│   ├── prefix_dollar.py        # `$` prefix custom command handler
│   ├── asahotak.py             # Quiz game (reply-based answer matching)
│   ├── susunkata.py            # Word-scramble quiz game
│   ├── quiz.py                 # Multiple-choice inline quiz
│   ├── ship.py                 # Fun "shipping" compatibility generator
│   ├── waifu.py                # Waifu image fetch & preference picker
│   ├── kang.py                 # Sticker/emoji kang (save) command
│   ├── getsticker.py           # Convert media/message to sticker
│   ├── spoiler.py              # Toggle spoiler mark on downloads
│   ├── translate.py            # Multi-language text translation
│   ├── networking.py           # /ip /domain /net /whoisdomain lookups
│   ├── weather.py              # Weather forecast lookup
│   ├── kurs.py                 # Currency exchange rates
│   ├── speedtest.py            # Ookla speedtest runner
│   ├── resi.py                 # Package tracking (courier receipt)
│   ├── nobg.py                 # Background remover
│   ├── upscale.py              # AI image upscaler
│   ├── aitext.py               # AI text utilities
│   ├── aiimagedetector.py      # AI-generated image detector
│   ├── igstalk.py              # Instagram profile stalker
│   ├── igstory.py              # Instagram story downloader
│   ├── anime.py                # Anime search & info (NeoXR API)
│   ├── quoteanime.py           # Anime quote generator
│   ├── gemini.py               # Google Gemini AI chat handler
│   ├── groq.py                 # Groq AI chat handler
│   ├── donate.py               # Donation/support info
│   ├── groups.py               # Group listing & management
│   ├── reminder.py             # Timed reminder scheduler
│   ├── share.py                # Shared-media gallery & re-send
│   ├── delete.py               # Message deletion utilities
│   ├── ping.py                 # Latency check
│   ├── start.py                # /start, deep-links & verification entry
│   ├── help.py                 # Public help menu
│   ├── helpowner.py            # Owner-only command reference (OWNER_ID locked)
│   ├── fasttelethon.py         # FastTelethon upload engine toggle
│   ├── uploadengine.py         # Upload engine selector (MTProto / Pyrofork)
│   └── __init__.py             # Handler package initializer
│
├── rag/                        # Retrieval-Augmented Generation subsystem
│   ├── loader.py               # Document & context loader
│   ├── chunker.py              # Semantic text chunking engine
│   ├── retriever.py            # Local vector retrieval & relevance ranker
│   └── prompt.py               # Grounded prompt synthesis
│
└── utils/                      # Cross-cutting utility modules & shared infrastructure
    ├── menu_lock.py            # Fail-closed 4-tier inline button ownership guard
    ├── rich_common.py          # Shared rich-message helpers (not-modified guard)
    ├── rich_msg.py             # Bot API 10.3 Rich Message (HTML blocks) builder & fallback
    ├── rich_search.py          # Unified compact search table layout with pagination
    ├── rich_stream.py          # Streaming rich-message updater for AI drafts
    ├── scrapling_browser.py    # Singleton stealth Playwright session for Cloudflare bypass
    ├── axiom.py                # Axiom Payment QRIS gateway client (curl_cffi Chrome JA3)
    ├── http.py                 # Shared aiohttp session with tuned timeouts & pool
    ├── firecrawl.py            # Firecrawl web search + page scrape (grounding backend)
    ├── jina.py                 # Jina AI reader & SERP fallback (grounding backend)
    ├── recent_messages.py      # In-memory rolling message buffer for grand-reply context
    ├── caca_memory.py          # Persona conversation memory store & cleanup
    ├── gemini_memory.py        # Gemini chat memory store
    ├── groq_memory.py          # Groq chat memory store
    ├── nsfw.py                 # NSFW state helper
    ├── config.py               # Central env/config accessor
    ├── fonts.py                # Font asset loader for quote/image rendering
    ├── text.py                 # Text sanitizing & formatting helpers
    ├── logger.py               # Message/event logger (forwards to LOG_CHAT_ID)
    ├── startup.py              # Startup warmup (browser, caches, PTB warnings)
    ├── stop_draft.py           # Real-time message streaming draft cancellation listener
    └── user_collector.py       # User metadata collector
```

### Architectural Highlights

- **Headless Downloader Isolation:** Download scrapers and resolvers live strictly under `handlers/dl/<site>/`. They can be imported and executed in headless CLI/worker contexts without pulling any Telegram UI dependencies.
- **Fail-Closed Ownership Guard:** All interactive menus and inline pickers are bound to the command caller via `utils/menu_lock.py`, preventing unauthorized group members from hijacking search sessions.
- **UVLoop & Subprocess Safety:** Heavy CLI subprocesses (`yt-dlp`, `aria2c`, `ffmpeg`) use `subprocess.Popen` via `asyncio.to_thread` (`posix_spawn`) to guarantee immunity against `fork()` SIGSEGV (-11) crashes under `uvloop`.
- **Bot API 10.3 Rich Messages:** Structured explorer menus leverage Telegram Bot API 10.3 rich formatting (`<h1>`, `<table compact>`, `<aside>`, collapsible sections) with automatic fallback to standard HTML.

## Quick Installation (Recommended)

The recommended installation method is to use the provided installer script:

```
git clone https://github.com/rifqi1146/kiyoshibot.git
cd kiyoshibot
sudo bash install.sh
```
## Manual Installation
```
apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  git \
  ffmpeg \
  curl \
  unzip \
  build-essential \
  libjpeg-dev \
  zlib1g-dev \
  cmake \
  libssl-dev \
  gperf
```

```
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt install -y nodejs
```

```
curl -fsSL https://deno.land/install.sh | sh
sudo ln -sf /root/.deno/bin/deno /usr/local/bin/deno
```

```
curl -L https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz \
| tar zx

sudo mv speedtest /usr/bin/speedtest
sudo chmod +x /usr/bin/speedtest
```

### Clone repository
```
git clone https://github.com/rifqi1146/kiyoshibot.git
```
```
cd kiyoshibot
```
```
python3 -m venv venv
```
```
source venv/bin/activate
```
```
pip install --upgrade pip
```
```
pip install -r requirements.txt
```
## Environment Setup

```
# Required - Telegram Bot Token
# Get it from @BotFather.
BOT_TOKEN=

# Required - Bot Owner IDs
# Telegram user IDs allowed as bot owners.
# Use comma-separated numeric IDs.
# Example: BOT_OWNER_ID=123456789,987654321
BOT_OWNER_ID=ids1,ids2,ids3


# Required - Log Chat ID
# Chat ID for logs, startup messages, debug files, and errors.
# Use a private log group/channel. Usually starts with -100.
LOG_CHAT_ID=

# Required - Telegram API
# Get API_ID and API_HASH from:
# https://my.telegram.org
API_ID=
API_HASH=

# Optional - Gemini AI
# Required only if you use Gemini / /ask feature.
# Get GEMINI_API_KEY from Google AI Studio:
# https://aistudio.google.com/app/apikey
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
GEMINI_API_URL="https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${GEMINI_API_KEY}"


# Optional - Groq AI
# Required only if you use Groq / /groq feature.
# Get GROQ_API_KEY from Groq Console:
# https://console.groq.com/keys
GROQ_API_KEY=


# Optional - OpenCode AI Proxy (/ask)
# Bot starts a local OpenAI-compatible proxy for free opencode.ai models.
# No API key needed. Owner-only toggles: /getmodel /setmodel /thinking /setsearch
OPENCODE_PROXY=1
OPENCODE_PROXY_HOST=127.0.0.1
OPENCODE_PROXY_PORT=20130
OPENCODE_MODEL=
OPENCODE_THINKING=off


# Optional - Web Grounding for /ask (Jina AI + Firecrawl)
JINA_API_KEY=
FIRECRAWL_API_KEY=
SEARCH_ENGINE=firecrawl
SEARCH_DEPTH=fast


# Optional - Google Search
# Required only if you use Google Search feature.
# Get GOOGLE_API_KEY from Google Cloud Console.
# Get GOOGLE_CSE_ID from Programmable Search Engine.
GOOGLE_API_KEY=
GOOGLE_CSE_ID=

# Optional - Force Join / Support Channel
# SUPPORT_CH_ID is your channel ID, usually starts with -100.
# SUPPORT_CH_LINK is your public invite/channel link.
SUPPORT_CH_ID=
SUPPORT_CH_LINK="https://t.me/"


# Optional - Donation Link
# Used by donate/support menu or command.
# Fill with Telegram link, Saweria, Trakteer, GitHub Sponsor, etc.
DONATE_URL="https://t.me/"


# Optional - Axiom Payment (QRIS gateway for /buypremium)
# Docs: https://portal.axiomcreative.xyz/docs
# AXIOM_BASE_URL is the gateway origin. AXIOM_API_KEY comes from the Axiom
# dashboard (Kunci API). Keep the key server-side only: never expose it to
# users, browsers, URLs, or logs.
# AXIOM_PREMIUM_AMOUNT is the premium price in rupiah (integer 1..10000000).
AXIOM_BASE_URL=https://portal.axiomcreative.xyz
AXIOM_API_KEY=
AXIOM_PREMIUM_AMOUNT=20000


# Optional - Quote API
# Required only if you use quote/sticker quote renderer.
# Default is local quote API server.
QUOTE_API_URI="http://127.0.0.1:3000"


# Optional - NeoXR API
# Required only if you use features that depend on NeoXR API.
# Get it from your NeoXR API provider/dashboard.
NEOXR_API_KEY=


# Optional - NH API / nhentai API Key
# Required only if you use features that depend on NH_API_KEY.
NH_API_KEY=


# Optional - Cloudflare Workers AI
# Required only if you use Caca / Cloudflare AI persona.
# Get account ID from Cloudflare Dashboard.
# Get auth token from Cloudflare API Tokens page.
#
# You can add multiple Cloudflare account/token pairs as fallback.
# Maximum supported: up to 10 accounts.
# Pattern:

CLOUDFLARE_ACCOUNT_ID=
CLOUDFLARE_AUTH_TOKEN=
CLOUDFLARE_ACCOUNT_ID_2=
CLOUDFLARE_AUTH_TOKEN_2=

# Optional - Cloudflare Turnstile Captcha
# Required for frontend validation on the web verification page.
# Get keys from Cloudflare Dashboard -> Turnstile.
TURNSTILE_SITE_KEY=
TURNSTILE_SECRET_KEY=


# Optional - hCaptcha Alternative
# Get keys from dashboard.hcaptcha.com.
HCAPTCHA_SITE_KEY=
HCAPTCHA_SECRET_KEY=

# Optional Web Verification Port
# Internal port where the local captcha verification service listens.
WEB_PORT=5050


# Optional - Public Domain URL
# External root address used to route captcha requests.
PUBLIC_URL="https://hirohitokiyoshi.site"
```

### Run Bot
```
python main.py
```

## Credits

This project uses and depends on the following tools and services:

- [Groq Cloud](https://console.groq.com/home)
- [Google Gemini](https://ai.google.dev/)
- [OpenCode](https://opencode.ai)
- [Jina AI](https://jina.ai)
- [Firecrawl](https://www.firecrawl.dev)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [godv bot](https://github.com/govdbot/govd)
- [gallery-dl](https://github.com/mikf/gallery-dl)
- [TikWm](https://www.tikwm.com/)
- [Pyrofork](https://github.com/Mayuri-Chan/pyrofork)
- [Axiom Payment](https://portal.axiomcreative.xyz)
- [omkarcloud/tiktok-scraper](https://github.com/omkarcloud/tiktok-scraper) — MIT License,
  Copyright (c) 2026 Chetan Jain. Vendored TikTok web-API client under
  `handlers/asupan/tiktokapi/` (used by the Asupan feature for keyword search).
- [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
  — Apache-2.0. The pure-Python `webmssdk` signer (`X-Dynosaur` / `X-Gnarly`)
  in `handlers/asupan/tiktokapi/signer.py` is ported/vendored from here.
