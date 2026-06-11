[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-22.5-blue?logo=telegram)](https://github.com/python-telegram-bot/python-telegram-bot)
[![License](https://img.shields.io/badge/License-GPLv3-green)](https://www.gnu.org/licenses/gpl-3.0.html)

# Telegram Multi-Function Bot

A multi-function Telegram bot built with Python and `python-telegram-bot`, providing AI features, downloader utilities, moderation tools, networking commands, and additional group management features.

## Features

- AI chat and assistant commands
- Media downloader for multiple platforms
- Google search integration
- Networking and utility tools
- Moderation and administration features
- Group and verification features
- Entertainment and miscellaneous commands

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
cd groupbot
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
```
source .env
```
### Run Bot
```
python main.py
```

## Credits

This project uses and depends on the following tools and services:

- [Groq Cloud](https://console.groq.com/home)
- [Google Gemini](https://ai.google.dev/)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [godv bot](https://github.com/govdbot/govd)
- [gallery-dl](https://github.com/mikf/gallery-dl)
- [Sonzai Api](http://api.sonzaix.indevs.in)
- [TikWm](https://www.tikwm.com/)
- [Pyrofork](https://github.com/Mayuri-Chan/pyrofork)
