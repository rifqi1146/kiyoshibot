[![Python](https://img.shields.io/badge/Python-3.12%20%7C%203.13-blue?logo=python)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-22.8-blue?logo=telegram)](https://github.com/python-telegram-bot/python-telegram-bot)
[![License](https://img.shields.io/badge/License-GPLv3-green)](https://www.gnu.org/licenses/gpl-3.0.html)

# KiyoshiBot

A single-tenant, self-hosted Telegram mega-bot written in Python on top of
`python-telegram-bot`. It bundles a multi-platform media downloader, several
Rich-Message content explorers, a persona chat engine with token-level credit
billing, an AWS EC2 reseller storefront with QRIS checkout, a local
RAG/grounded assistant, group moderation, and a large utility command set.

Everything runs in one process tree: a supervisor (`main.py`) that optionally
spawns a local `telegram-bot-api` server, and the bot itself (`bot.py`).

---

## Table of Contents

- [Feature Overview](#feature-overview)
- [Architecture](#architecture)
- [Complete Repository Layout](#complete-repository-layout)
- [Handler Pipeline (PTB Groups)](#handler-pipeline-ptb-groups)
- [Downloader Subsystem](#downloader-subsystem)
- [Explorer Subsystems](#explorer-subsystems)
- [Caca AI Engine & Credit Billing](#caca-ai-engine--credit-billing)
- [RAG & AI Commands](#rag--ai-commands)
- [AWS VPS Reseller](#aws-vps-reseller)
- [Payments](#payments)
- [Moderation & Permissions](#moderation--permissions)
- [Database Layer](#database-layer)
- [Scheduled Jobs](#scheduled-jobs)
- [Installation](#installation)
- [Environment Variables](#environment-variables)
- [Running the Bot](#running-the-bot)
- [Maintenance Commands](#maintenance-commands)
- [Operational Notes & Known Quirks](#operational-notes--known-quirks)
- [Limitations & Things That Will Annoy You](#limitations--things-that-will-annoy-you)
- [Testing](#testing)
- [Credits](#credits)
- [License](#license)

---

## Feature Overview

### AI & Chat

| Command | What it does |
|:--|:--|
| `/ask <question>` | Grounded answer backed by a local OpenAI-compatible proxy that talks to free OpenCode models, with Firecrawl/Jina pre-search grounding. Owner toggles: `/getmodel`, `/setmodel`, `/thinking`, `/setsearch` |
| `/groq <question>` | Groq-hosted chat completion with the same RAG context and a 20-message memory window |
| `/caca` | Persona chat engine ("Caca"): 8 modes (`/mode`), vision for photos and stickers, auto web search, token-billed credits |
| `/aitext` | Detects AI-generated text |
| `/aidetect` | Detects AI-generated images |
| `/cacatopup` / `/buycaca` | Buy Caca credits via QRIS (presets or custom amount) |
| `/cacasaldo` | Show credit balance per lot with expiry dates |
| `/providercaca` | Owner picker for the Caca provider and account token status |
| `/cbacc` | Owner CodeBuddy/WorkBuddy account pool management |
| `/gsearch <query>` | Google Programmable Search with pagination |
| `/anime <query>` | Anime search |
| `/quoteanime` | Random anime quote |
| `/tr <text>` / `/trlist` | Text translation and language list |

### Downloader

Two entry points: `/dl <url> [url ...]` (up to 5 URLs per batch) and passive
link detection on any message. 25 platform engines covering TikTok, Instagram,
YouTube, X/Twitter, Facebook, Threads, Reddit, Pinterest, Bunkr, MovieBox,
Anichin, Nekopoi, PunishWorld, Simontok, CosXplay, DarknessPorn, FemdomVC,
AsianGirl, BDSMLust, Heavy-R, Cosplaytele, Pawchive, RetroTube/VOE, and Drakor.id.
HLS segment pipelines, shortener bypasses, AES-128/AES-256 decryptors, and a
16-connection `aria2c` transport, all behind a premium/NSFW gate and a
3-per-60-second rate limit for non-premium users. See
[Downloader Subsystem](#downloader-subsystem).

### Content Explorers

Ten Rich-Message explorers with browse, search, pagination, and in-place
download:

| Command | Content | Access |
|:--|:--|:--|
| `/moviebox` | Movies and TV series (59 sections, season/episode selection) | Premium |
| `/anichin` | Donghua (schedule, A–Z, genres, 4-layer server fallback) | Premium |
| `/nekopoi` | Hentai and JAV (series → episode grid → resolution) | Premium + NSFW |
| `/punish` | PunishWorld (126 categories, 900+ tags) | Premium + NSFW |
| `/simontok` | Simontok (7 curated sections, 16 genres) | Premium + NSFW |
| `/cosxplay` | CosXplay (176 categories, cosplay models, tags) | Premium + NSFW |
| `/becekku` | Becekku (WP-Script browse, categories, tags) | Premium + NSFW |
| `/lendirqu` | LendirQu (same engine, separate site) | Premium + NSFW |
| `/drakorid` | Drakor.id Asian drama with episode picker | Premium |
| `/firmware` | Xiaomi/Redmi/POCO HyperOS & MIUI ROM archive | Free |

### Manga Reader

Three sources in one command:

| Command | Source |
|:--|:--|
| `/manga mangadex <query or id>` | MangaDex API |
| `/manga maid <query>` | maid.my.id |
| `/manga nh <code>` | nhentai (v2 API, optional `NH_API_KEY`) |

Results open as a Rich Message detail card with cover, synopsis, and chapter
list. `Read Now` streams pages as Telegram photos; `Download PDF` builds a PDF
in memory (5 pages per batch, width capped at 1200 px, JPEG q75) and sends it as
a document. Reader and PDF replies attach to the original command message.
Sessions expire after 2 hours, so an old button answers `Session expired.
Please search again.`

### AWS VPS Store

`/vps` and `/myvps` sell real EC2 instances with dynamic QRIS checkout,
automatic provisioning across every region the account can reach, per-server
firewall security groups, and a full owner panel:

| Command | Purpose |
|:--|:--|
| `/vps` | Buy: plan → duration → region → OS → QRIS |
| `/myvps` | List your servers, open detail, manage firewall, claim warranty |
| `/vpsadmin` | Owner panel: overview, accounts, pricing, scan |
| `/vpslist` | Every instance, tagged `(bot)` or `(manual)`, with per-region summary |
| `/vpsstats` | Aggregate counts and revenue |
| `/vpsinfo <id>` | Full spec sheet for one instance |
| `/vpsterminate <id>` | Terminate |
| `/vpsrenew <id> <days>` | Extend validity |
| `/vpsgrant` | Free-VPS flow with full inline menus, delivered by DM |
| `/vpsprice` | Interactive per-tier price editor |
| `/addawsaccount` / `/delawsaccount` | AWS credential pool management |

In a group, `/vps` and `/myvps` reply with a deep link to the private chat
instead of opening the storefront publicly.

### Caca Credit Billing

`/cacatopup`, `/buycaca`, `/cacasaldo` sell chat credits through QRIS with a
multi-lot FEFO ledger, per-token settlement, and expiry. `/freecaca`,
`/cacaadmin`, `/addcredit` are owner controls.

### Moderation & Group Admin

| Command | Purpose |
|:--|:--|
| `/ban <duration> <user> [reason]` | Ban. Reply or pass an id/`@username`. `/ban 7d @user toxic` |
| `/kick <user>` | Remove and allow immediate rejoin through the invite link |
| `/mute <duration> <user> [reason]` | Restrict until a deadline, then auto-unmute. `/mute 10m @user spam` |
| `/unmute <user>` | Lift a mute |
| `/promote <user> [title]`, `/demote <user>` | Admin rights with optional custom title |
| `/tag [title]`, `/untag` | Set or clear a member's tag text (needs bot *Manage Tags* permission) |
| `/wlc` | Welcome message configuration |
| `/moderation` | Per-group moderation toggle and settings |
| `/autodel` | Auto-delete configuration |
| `/nsfw`, `/spoiler` | Per-group `enable` / `disable` |
| `/settings` | Per-user preferences |

Plus a support-channel force-join gate with realtime leave detection
(120-second cache, immediate invalidation on `CHAT_MEMBER` updates) and
captcha-backed web verification for new members.

### Utilities

| Command | Purpose |
|:--|:--|
| `/q`, `/qi`, `/qs` | Quote sticker / image / 9:16 story from recent messages |
| `/weather <city>` | Forecast, rendered as a Rich report |
| `/ip`, `/net`, `/domain`, `/whoisdomain` | Network and DNS lookups |
| `/kurs` / `/kurs list` | Currency conversion and rate list |
| `/resi <tracking>` | Package tracking |
| `/speedtest` | Ookla CLI speed test |
| `/reminder` | Scheduled reminders |
| `/music <query>` | Music search and audio download |
| `/nobg`, `/upscale` | Background removal, AI upscale |
| `/kang`, `/getsticker` | Sticker pack save, media-to-sticker |
| `/igstalk`, `/igstory` | Instagram profile and story helpers |
| `/share` | Shared-media gallery |
| `/ship`, `/waifu` | Fun pairing and waifu images |
| `/quiz`, `/asahotak`, `/susunkata` | Games |
| `/settings` | Per-user preferences |
| `/help`, `/menu` | Rich help menu |

### Owner Operations

| Command | Purpose |
|:--|:--|
| `/helpowner` | Rich reference split into System / AI & Proxy / Caca AI / Access & Users / Data & Media |
| `/stats` | Live process, hardware, and runtime dashboard |
| `/backup`, `/restore`, `/autobackup` | Database snapshots and a 6-hour automatic job |
| `/broadcast` | Message every known user or group |
| `/reload` | Hot module reload without dropping the process |
| `/restart`, `/reboot` | In-place respawn via `os.execv` |
| `/update` | `git pull` then hot reload |
| `/cookies` | Upload a Netscape `cookies.txt` |
| `/addsudo`, `/rmsudo`, `/sudolist` | Sudo tier management |
| `/premium` | Grant, revoke, and list premium users |
| `/blacklist` | Global user and group blacklist |
| `/freecaca`, `/cacaadmin`, `/addcredit` | Caca access and credit administration |
| `/providercaca`, `/cbacc` | Caca provider and account pool |
| `/getmodel`, `/setmodel`, `/thinking`, `/setsearch` | `/ask` runtime tuning |

These are documented only in `/helpowner` and are intentionally excluded from
`data/rag_docs/`, so the AI never offers commands a regular user cannot run.

---

## Architecture

The design principle throughout is **strict layering**: scrapers and resolvers
are headless Python, the Telegram UI is a thin shell over them, and nothing in
the download layer imports the UI layer.

```
                    ┌────────────────────────────────────────┐
                    │            main.py (supervisor)         │
                    │  reuse-or-spawn telegram-bot-api :8081  │
                    └───────────────────┬────────────────────┘
                                        │ execs
                    ┌───────────────────▼────────────────────┐
                    │               bot.py                    │
                    │  uvloop → Application → handler groups  │
                    │  post_init / post_shutdown lifecycle    │
                    └───┬──────────┬──────────┬──────────┬────┘
                        │          │          │          │
              ┌─────────▼──┐ ┌─────▼─────┐ ┌──▼──────┐ ┌─▼──────────┐
              │ handlers/  │ │   rag/    │ │ utils/  │ │ database/  │
              │ commands   │ │ loader    │ │ rich_*  │ │ SQLite     │
              │ callbacks  │ │ chunker   │ │ menu_   │ │ WAL +      │
              │ messages   │ │ catalog   │ │  lock   │ │ busy_time  │
              │ dl/        │ │ retriever │ │payment  │ │ out 30s    │
              │ vps/       │ │           │ │http     │ │            │
              │ caca/      │ └───────────┘ │timectx  │ └────────────┘
              │ explorers  │               └─────────┘
              └──────┬─────┘
                     │
       ┌─────────────┼──────────────┬─────────────────┐
       │             │              │                 │
 ┌─────▼─────┐ ┌─────▼──────┐ ┌─────▼──────┐ ┌────────▼────────┐
 │ yt-dlp    │ │ aria2c     │ │ ffmpeg     │ │ Scrapling       │
 │ gallery-  │ │ 16 sockets │ │ remux/HLS/ │ │ stealth browser │
 │ dl        │ │            │ │ mp3        │ │ (singleton)     │
 └───────────┘ └────────────┘ └────────────┘ └─────────────────┘
```

### Architectural invariants

- **Headless downloader isolation.** Every scraper lives under
  `handlers/dl/<site>/` with a uniform shape: `constants.py`, `extractor.py`,
  `main.py`, `__init__.py` (some add `downloader.py`, `fallback.py`,
  `mobile_session.py`, `packer.py`, `cossora.py`). These packages must import
  cleanly without pulling `handlers.dl.router` or any Telegram UI package.
  `handlers/moviebox/__init__.py` uses PEP 562 lazy `__getattr__` specifically
  to avoid a circular import through the router.
- **Fail-closed ownership.** Every inline menu is bound to the user who
  triggered the command via `utils/menu_lock.py`. Group members cannot press
  somebody else's buttons. Resolution order: explicit `expected_user_id` →
  session data → `(chat_id, message_id)` registry → replied-to message author →
  private chat allow. Anything else is denied.
- **UVLoop subprocess safety.** `asyncio.create_subprocess_exec` under `uvloop`
  forks the process while C-level threads (curl_cffi, camoufox, sqlite3) are
  alive, which corrupts libc state and crashes the child with `SIGSEGV (-11)`.
  Every external process therefore goes through `handlers/dl/proc.py`
  (`subprocess.Popen` on a worker thread, `posix_spawnp`), which returns an
  `AsyncProcess` wrapper exposing `communicate()`, `wait()`, `kill()`,
  `returncode`.
- **Bot API 10.3 Rich Messages.** Explorers and report screens use
  `sendRichMessage` / `editMessageText` with `rich_message: {html: ...}`.
  Every helper falls back to plain HTML if the server does not support it, and
  treats Telegram's `message is not modified` as a silent no-op rather than
  degrading the rich layout to plain text.
- **Progress editing is throttled at a 5-second floor.** Telegram rate-limits
  message edits at roughly one per 5 seconds per chat. `handlers/dl/progress.py`
  enforces a hard floor of 5.0s and buckets adaptively: under 1 MB/s → 10s,
  under 3 MB/s → 7s, otherwise 5s. Speed is computed over a 1-second sliding
  window, never as `delta_bytes / delta_time` per chunk.
- **Bounded in-memory caches.** Every long-lived dict (`DL_CACHE`,
  `_MANGA_CACHE`, search caches, edit-status caches, asupan video pools) has a
  TTL and a hard size cap with a purge path, so the process does not leak over
  weeks of uptime.
- **Self-healing caches after `/reload`.** `importlib.reload()` resets all
  module-level variables. Premium, persona mode, `/freecaca`, and blacklist
  caches re-read SQLite when they find themselves empty instead of assuming
  "nothing configured".
- **Local Bot API probe.** `bot.py` probes port 8081 with a 1-second TCP
  connect. If it answers, the bot uses `http://127.0.0.1:8081` for both the
  bot API and file downloads, which raises the upload ceiling from 50 MB
  (cloud API) to 2 GB. If it does not answer in time, the bot silently falls
  back to the official cloud API.

---

## Complete Repository Layout

367 Python files across 137 directories. Every directory and module is listed
below with its responsibility.

```
kiyoshibot/
├── main.py                       Supervisor: reuse running telegram-bot-api or spawn one, then exec bot.py
├── bot.py                        Logging setup, token redaction, uvloop, Application build, handler groups
├── install.sh                    Multi-distro installer (apt/dnf/yum/pacman/zypper/apk) + venv + deps
├── requirements.txt              Python dependency lock list
├── Dockerfile                    python:3.13-slim image with ffmpeg/node/deno/speedtest/scrapling browsers
├── docker-compose.yml            Two services: kiyoshi_bot + local telegram-bot-api sharing tg-api-data
├── .env.example                  Documented sample environment (copy to .env)
├── LICENSE                       GPLv3
│
├── database/                     SQLite persistence, one module per feature domain
├── handlers/                     Command handlers, callbacks, and all user-facing surfaces
│   ├── dl/                       Headless download engines (25 platform subpackages)
│   ├── vps/                      AWS EC2 reseller: checkout, provisioning, firewall, owner panel
│   ├── caca/                     Persona engine, provider clients, credit billing
│   ├── proxy/                    /ask OpenCode-compatible proxy + web grounding
│   ├── manga/                    Multi-source manga reader and PDF export
│   ├── moderation/               Group admin actions, permissions, sudo tiers
│   ├── stats/                    Owner dashboard (process, hardware, runtime telemetry)
│   ├── payment/                  /buypremium QRIS flow
│   ├── moviebox/ anichin/ nekopoi/ punishworld/ simontok/ cosxplay/
│   ├── retrotubeexplorer/ drakor/ firmware/ asupan/
│   └── (top-level command modules — see the section below)
├── rag/                          SQLite FTS5 + BM25 retrieval subsystem
├── utils/                        Cross-cutting helpers (rich messages, HTTP, memory, browser, time)
├── data/                         Runtime state (gitignored): SQLite DBs, sessions, caches, RAG docs
├── fonts/                        Poppins font family used by image and stats renderers
├── tests/                        Standalone test scripts
└── scripts/                      Instagram session maintenance CLIs
```

### Root files

| File | Responsibility |
|:--|:--|
| `main.py` | Reads `API_ID`/`API_HASH`, checks whether port 8081 is already serving, reuses an existing `telegram-bot-api` or spawns one with `--local --http-port=8081 --dir=tgdata`, then launches `bot.py` and reaps both children on `SIGINT`/`SIGTERM`. |
| `bot.py` | `setup_logger()` installs `TokenRedactFilter` (strips `bot<id>:<token>` from any log line) and `EmojiFormatter`. Enables `uvloop` unless `BOT_UVLOOP=0`, starts the OpenCode proxy thread, builds the `Application` with `concurrent_updates(True)`, pool size 64, 20-minute read/write timeouts, registers the four handler sets, then `run_polling` with `drop_pending_updates=True`. |
| `install.sh` | Six-phase installer: detect package manager, install system deps, install Node.js, install Deno, install Ookla speedtest, create venv and install Python deps. Prints systemd service hints. |

### `database/` — persistence layer

Every module opens SQLite through `database/db.py::db_session`, which applies
`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=30000` and a 30-second
connection timeout. Connections without `busy_timeout` raise
`sqlite3.OperationalError: database is locked` under load.

| Module | Responsibility |
|:--|:--|
| `db.py` | Shared connection factory and `db_session` context manager. |
| `download_db.py` | Per-user download counters and quota lookups. |
| `payment_db.py` | `/buypremium` invoice store (`data/payments.sqlite3`). |
| `caca_billing_db.py` | Caca credit ledger: `caca_invoices`, `caca_credit_lots`, `caca_reservations`, `caca_usage_logs`, `caca_audit_logs`, `caca_settings` in `data/caca_billing.sqlite3`. FEFO consumption, atomic `BEGIN IMMEDIATE` reservations, idempotent fulfilment. |
| `caca_db.py` | Persona mode per user plus the premium user registry (`premium_users` in `data/caca.sqlite3`). |
| `moderation_db.py` | Per-group moderation config, warn counters, mute state. |
| `blacklist_db.py` | Global user and group blacklist. |
| `groups_db.py` | Known group registry and metadata. |
| `join_status_db.py` | Support-channel membership cache, TTL 120 s. |
| `nsfw_db.py` | Per-group NSFW toggle. |
| `asupan_db.py` | Asupan keyword history and preferences. |
| `share_db.py` | Shared-media gallery index. |
| `ship_db.py` | `/ship` pairing history. |
| `spoiler_db.py` | Per-group spoiler-mark toggle. |
| `user_settings_db.py` | Per-user preferences (silent downloads, default format). |
| `welcome_db.py` | Welcome config, verified users, pending verifications. |
| `premium.py` | Premium registry with a self-healing cache that re-reads SQLite whenever the in-memory set is empty. |
| `vps_db.py` | Full VPS store schema: `vps_pricing`, `vps_invoices`, `vps_instances`, `vps_firewall_rules`, `vps_accounts`, `vps_region_prefs`, `vps_settings`, `vps_audit` in `data/vps.sqlite3`. Also owns `next_vps_id()`, `claim_invoice_for_provision()`, `purge_old_terminated()`. |

### `handlers/` — top-level command modules

| Module | Commands / role |
|:--|:--|
| `commands.py` | `COMMAND_HANDLERS` table (119 handler entries) and the dispatcher. A command field may be a string or a list; three entries are built with list concatenation, which is how `/q`, `/qi`, and `/qs` each expand to 23 glued names (`/q`, `/q1`…`/q10`, `/qr`, `/q1r`…`/q10r`) instead of a hand-written list. Registers every `CommandHandler` on group `-1`. |
| `callbacks.py` | Central regex callback router. **Order matters** — see [Handler Pipeline](#handler-pipeline-ptb-groups). |
| `messages.py` | All `MessageHandler` registrations: blacklist gate, text-capture flows, auto-download detection, AI reply router, welcome, `$` prefix, collectors, quotly buffer, log forwarder. |
| `quotly.py` | `/q` (WebP sticker), `/qi` (PNG image), `/qs` (9:16 story). `parse_quote_args()` reads the glued command suffix and the argument list together, so `/q3`, `/q3r`, `/q -3`, `/q r 2`, and `/q #a/#b` all work. Count is clamped to `_MAX_QUOTE_COUNT = 10`; `-N` walks backwards, `r`/`reply` includes the replied-to message. Colour argument accepts a name, a hex pair, or a gradient. Reply-context for grand-replies is recovered through the recent-message buffer, then an MTProto re-fetch. Renders through an external quote service at `QUOTE_API_URI`. |
| `music.py` | `/music` search across sources with rich pagination and audio download. |
| `gsearch.py` | `/gsearch` Google Programmable Search with paging and ownership lock. |
| `welcome.py` | `/wlc` config, new-member handling, captcha verification HTTP server, timed kick for unverified users. |
| `join.py` | `require_join_or_block()` force-join gate, realtime `CHAT_MEMBER` leave detection, centralized `command_join_gate` on group `-101`, cached membership with `MEMBERSHIP_TTL = 120`. |
| `nsfw.py` / `spoiler.py` | Per-group toggles (`enable`/`disable`). |
| `premium.py` | `/premium` grant/revoke/list with cascade reset of mode and memory rows. |
| `premiumbenefit.py` | `/premiumbenefit` Rich table built live from `PREMIUM_ONLY_DOMAINS` and `PREMIUM_NO_NSFW_DOMAINS`. |
| `setting.py` | `/settings` per-user menu (silent download, default format). |
| `reload.py` | `/reload` hot module reload: env refresh first, `.state` modules first, dependency-ordered reimport, handler re-registration, cache refresh, RAG reindex, rollback on failure. |
| `restart.py` | `/restart`, `/reboot` in-place via `os.execv` (same PID). |
| `update.py` | `/update` git fetch/pull then hot reload. |
| `backup.py` | `/backup`, `/restore`, `/autobackup` with a 6-hour scheduled job. |
| `broadcast.py` | `/broadcast` to users and groups. |
| `blacklist.py` | Message gate on group `-100` and callback gate on group `-99`, both raising `ApplicationHandlerStop`. |
| `cookies.py` | `/cookies` Netscape-format `cookies.txt` upload (512 KB cap, atomic `os.replace`). |
| `collector.py` | Throttled group/user metadata collector (`COLLECT_TTL_SEC`, capped seen-set). |
| `prefix_dollar.py` | `$command` prefix router. |
| `asahotak.py` | Quiz game, reply-based answer matching (group `-4`). |
| `susunkata.py` | Word-scramble game, reply keyed by prompt message id (group `-3`). |
| `quiz.py` | Multiple-choice inline quiz. |
| `ship.py` | `/ship` compatibility generator. |
| `waifu.py` | Waifu image fetch with preference picker. |
| `kang.py` / `getsticker.py` | Sticker pack save and media-to-sticker conversion. |
| `translate.py` | `/tr`, `/trlist` via `deep-translator`. |
| `networking.py` | `/ip`, `/domain`, `/net`, `/whoisdomain`, rendered through `utils/rich_msg.Report`. |
| `weather.py` | `/weather` forecast, Rich report. |
| `kurs.py` | `/kurs`, `/kurs list` currency rates, Rich report. |
| `speedtest.py` | `/speedtest` Ookla CLI runner. |
| `resi.py` | `/resi` package tracking. |
| `nobg.py` / `upscale.py` | Background removal and AI upscale. |
| `aitext.py` / `aiimagedetector.py` | AI-content detectors. |
| `igstalk.py` / `igstory.py` | Instagram profile and story helpers. |
| `anime.py` / `quoteanime.py` | Anime search and quote generator. |
| `groq.py` | `/groq` with RAG context and memory window. |
| `donate.py` / `groups.py` / `reminder.py` / `share.py` / `delete.py` | Support link, group listing, scheduler, gallery, deletion. |
| `ping.py` | `/ping` latency plus `/cancel` which aborts pending firewall and account input flows. |
| `start.py` | `/start`, deep links (`vps`, `myvps`, `buypremium`, `verify_<token>`). |
| `help.py` / `helpowner.py` | Public help menu (Rich) and owner reference (Rich, 5 categories, `OWNER_ID` locked). |
| `fasttelethon.py` / `uploadengine.py` | Upload engine selection (MTProto Telethon / Pyrofork). |
| `__init__.py` | Package initializer. |

### `handlers/dl/` — downloader core modules

| Module | Responsibility |
|:--|:--|
| `router.py` | Link detection (`auto_dl_detect`), `/dl`, batch downloads (max 5 URLs), rate limiting (3 per 60 s for non-premium), premium/NSFW gate, resolution picker entry, worker pool (`YTDLP_SEM = 8`, batch `Semaphore(5)`), and the full latency log chain. |
| `service.py` | Download coordinator: sends the platform-specific worker to the right module, dispatches media/albums, splits groups of 10, orchestrates the post-download send. |
| `probe.py` | Registry of which URLs get a resolution picker (`supports_ytdlp_resolution`, `supports_nekopoi_resolution`, `supports_cosxplay_resolution`, `supports_darknessporn_resolution`, `supports_drakorid_resolution`, `supports_anichin_resolution`, `supports_moviebox_resolution`, `supports_resolution_picker`, `supports_both_resolution_engines`). |
| `progress.py` | `TransferStats` (byte tracking, windowed speed, ETA, adaptive interval), `render_progress_text`, `edit_status`, `prune_edit_cache`. Single source for all transfer UI and terminal logs. |
| `stages.py` | Staged status-message transitions (metadata → picker → progress). |
| `keyboards.py` | Format, resolution, engine, and TikTok-slideshow inline keyboards. |
| `state.py` | Shared mutable runtime registry (`DL_CACHE`, `DL_LIMIT_CACHE`). Carved out so `/reload` reloads it before its consumers. |
| `constants.py` | `PREMIUM_ONLY_DOMAINS` (58 including wildcards), `PREMIUM_NO_NSFW_DOMAINS` (5), `AUTO_DOWNLOAD_DOMAINS` (87), `MAX_TG_SIZE = 1999 * 1024 * 1024` (just under the 2 GB MTProto ceiling), `DL_FORMATS`, `TMP_DIR`, `AUTO_DL_DB`, `COOKIES_PATH`. |
| `adult_gate.py` | Sensitive-media gate for mixed platforms (X/Twitter `possibly_sensitive`, Reddit `over_18`). Raises `AdultContentNotAllowed`; non-premium gets a clear refusal instead of a yt-dlp fallback. |
| `utils.py` | URL normalization, supported-platform checks, ffprobe validation (`is_invalid_video` with a 15 s timeout), URL extraction. |
| `remux.py` | ffmpeg faststart remux, HLS AES-128 download, MP3 extraction, `prepare_download_result_for_send` which honours `remux_done`. |
| `proc.py` | `spawn_process()` and `AsyncProcess` — the uvloop-safe subprocess layer. Also `resolve_aria2()`. |
| `ytdlp.py` | yt-dlp runner built on `proc.py` with timeout handling. |
| `aria2.py` | `download_aria2()` — 16-connection `aria2c` transport with a single retry at 8 connections if the binary crashes, then streaming fallback. |
| `sweeper.py` | TTL cleanup of `downloads/`, orphan browser-profile reaper restricted to `playwright_`, `playwright-`, `camoufox_` prefixes with `/proc` in-use checks. |
| `mtproto_uploader.py` | Telethon bot client for 2 GB MTProto uploads. Also hosts `fetch_messages_raw()` used for quote reply-context recovery. |
| `pyrogram_uploader.py` | Pyrofork multi-connection uploader with a bounded `asyncio.wait_for` on start (20 s) so a slow handshake cannot hang startup. |
| `__init__.py` | Package exports. |

### `handlers/dl/<platform>/` — the 25 platform engines

| Package | Module set | Notes |
|:--|:--|:--|
| `anichin/` | `constants`, `extractor`, `main` | Donghua. Shortener chain through `mirrored.to`, multi-provider fallback, and a 4-layer streaming server chain (OK.ru → Rumble → Dailymotion → VidHide/StreamWish). All HTTP, never yt-dlp. |
| `asiangirl/` | `constants`, `extractor`, `main` | AES-128 CBC HLS with 12-way parallel segment download and local decryption. |
| `bdsmlust/` | `constants`, `extractor`, `main` | Dual embed resolver (`bdsmstreak` direct MP4, `bdsmx.tube` homoglyph-obfuscated base64 API). |
| `bunkr/` | `constants`, `extractor`, `main` | Three-tier album resolver (`/a/` → `/f/` → `/api/_001_v2` → CDN signing), Latin-1/UTF-8 filename repair, parallel album downloads. |
| `cosplaytele/` | `constants`, `cossora`, `extractor`, `main` | Photo galleries (parallel fetch, Pillow JPEG normalize) plus Cossora AES-256-CBC HLS video. |
| `cosxplay/` | `constants`, `extractor`, `main` | JSON-LD `VideoObject` plus `<video>` source parsing, high/low variants, MP3 extraction. |
| `darknessporn/` | `constants`, `extractor`, `main` | Same CDN family as CosXplay, `<source title="high"/"low">` variants. |
| `drakorid/` | `constants`, `extractor`, `main` | Korean/Asian drama. Download page scrape plus authenticated API fallback that bypasses the guest one-per-day limit; probes `files/` (720p), `720p/`, `480p/`, `360p/`. |
| `facebook/` | `main` | Facebook video via yt-dlp/direct parse. |
| `femdomvc/` | `constants`, `extractor`, `main` | 16-connection aria2c over a per-connection-throttled CDN. |
| `heavyr/` | `constants`, `extractor`, `main` | Direct MP4 or single-file HLS (`EXT-X-MAP` `.m4s`), with an embed-page fallback that validates the requested video id. |
| `instagram/` | `main`, `mobile_session` | Cookie-less Polaris GraphQL (dynamic LSD/CSRF), optional Android mobile session for age-gated posts, then NeoXR API. Carousel downloads run 6-way parallel. |
| `moviebox/` | `constants`, `extractor`, `main` | Self-contained resolver so `handlers/dl` never imports the UI package. Signed CDN URLs re-resolved per download. |
| `nekopoi/` | `constants`, `extractor`, `main` | Streampoi Dean-Edwards packer → HLS; IPv4-pinned session; `ouo.io` stealth bypass; DoodStream HTTP + Turnstile fallback; Pixeldrain and Mp4Upload prioritized by resolution. |
| `pawchive/` | `extractor`, `main` | Kemono-style Patreon archive. HTML parse with API JSON fallback, album dispatch. |
| `pinterest/` | `main` | Pinterest video and image. |
| `punishworld/` | `constants`, `extractor`, `main` | WP-Script `kolortube` cards, direct stream extraction, aria2c transport. |
| `reddit/` | `main` | Reddit video and gallery, `over_18` adult gate. |
| `retrotube/` | `constants`, `extractor`, `download`, `main`, `packer` | VOE.sx DDoS-Guard resolution with a narrow browser fallback, Dean-Edwards unpacker, `_MIRROR_GROUPS` TLD fallback. |
| `simontok/` | `constants`, `extractor`, `main` | AES-256-GCM player decrypt (`window.__PX` + `/api/pk` one-time nonce), HLS segments, plus a codec guard that refuses to ship an audio-only remux. |
| `threads/` | `main` | Threads media. |
| `tiktok/` | `constants`, `utils`, `extractor`, `downloader`, `fallback`, `main` | Split from a 1396-line original. PoW challenge solver, cookie helpers, three itemStruct parser shapes (universal/sigi/next), slideshow renderer, aria2c/aiohttp engine selection, tikwm fallback. |
| `twitter/` | `main` | X/Twitter media with `possibly_sensitive` adult gate. |
| `youtube/` | `main`, `community` | YouTube video, Shorts, and community posts via yt-dlp. |

### `handlers/vps/` — AWS EC2 reseller

| Module | Responsibility |
|:--|:--|
| `constants.py` | `TIERS` (`1g`/`2g`/`4g`/`8g` with instance types, fallbacks, vCPU, RAM, disk, bandwidth), 11 `OS_IMAGES`, `DURATIONS` (30d/14d/7d), `FEE_BPS` / `NASIONALPAY_FEE_BPS` (200 each) with `gateway_fee_bps()` and `ceil_to_step()`, `WARRANTY_DAYS = 7`, `normalize_tier()`, `get_tier()`, `get_os()`. |
| `aws.py` | `aws` CLI wrapper (no boto3), multi-account env injection (pops `AWS_PROFILE`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` then injects the account's credentials), live `list_regions()`, per-region AMI resolution and caching, network/SG resolution with auto-create, launch with subnet retry, `wait_for_ssh`, full `_describe_instance_sync` spec dump, volume enumeration, security-group create/authorize/revoke/delete, and `scan_account_instances()` over account × region with concurrency 10. |
| `accounts.py` | DB-backed account pool. `resolve_chain(region)` returns an ordered list (explicit per-region prefs → accounts declaring the region sorted by priority → the virtual `default` account), so `launch_vps()` can fall through on capacity/quota errors. `discover_account_regions()` probes each candidate region. |
| `account_cmd.py` | `/addawsaccount` (panel, staged flow, or one-line) and `/delawsaccount`. Credentials always verified with `sts get-caller-identity` before being written; the message containing the secret is deleted from the chat. |
| `gateway.py` | Thin NasionalPay wrapper for `/vps` (`create_qris`, `check_payment`, `qr_url`, DB-backed `qr_url` per invoice) with gateway logging. |
| `service.py` | Invoice polling (15 s), atomic claim, provisioning, QRIS creation through `gateway`, `gross_for()` fee gross-up, and the cached account scan (`SCAN_CACHE_TTL = 300`, `asyncio.Lock`, `invalidate_account_scan()`). |
| `firewall.py` | Per-server security group UI under the `fwr:` prefix, reply-scoped custom port input, protected port 22, automatic SG creation/attachment for pre-existing instances. |
| `render.py` | Every Rich screen and keyboard (including `confirm_content()` / `confirm_keyboard()` for the pre-purchase check), `merge_with_live()`, `_price_for()`, `_charged_amounts()`, and the `GRANT_STATE` store. |
| `rich.py` | `send_rich(bot, chat_id, reply_to, rich_html, plain_html, markup)` and `edit_rich(...)` with plain-HTML fallback. |
| `commands.py` | `/vps`, `/myvps`, `/vpsprice` with group→PM redirect via deep link. |
| `callbacks.py` | All `vps:` callbacks (tier, duration, region, OS, checkout, detail, restart, warranty, status refresh), `_toast_for()`, and `_LOADING_ACTIONS`. |
| `owner.py` | `/vpsadmin`, `/vpslist`, `/vpsstats`, `/vpsinfo`, `/vpsterminate`, `/vpsrenew`, `/vpsgrant`, plus the full inline grant flow and `grant_callback`. |
| `notify.py` | Owner DM on every provisioning path (purchase / grant / warranty) with server, region, account, access, buyer, payment, and validity in WIB. Complements the generic transaction feed in `handlers/payment/notify.py`. |
| `state.py` | Scan cache and `asyncio.Lock` holders. |
| `sweeper.py` | `sweep_expired_once()` (expiry termination) and `purge_old_records()` (7-day retention). |

### `handlers/caca/` — persona engine and billing

| Module | Responsibility |
|:--|:--|
| `caca.py` | Turn builder (`_build_persona_messages`), relation block, vision extraction, auto web search, provider routing, streaming, billing hooks. |
| `caca_prompt.py` | Owner-authored persona prompts (`default`, `bokep`, `toxic`, `sarkas`, `loli`, `yandere`, `cabul`, `masokis`). **Do not edit.** |
| `caca_mode.py` | `/mode` persona switcher with credit-based gating. |
| `caca_group.py` | `/cacaa` per-group controls. |
| `caca_billing.py` | `/cacatopup`, `/buycaca`, `/cacasaldo`, `/freecaca`, `/cacaadmin`, `/addcredit`, and `/providercaca`. |
| `codebuddy.py` | WorkBuddy/CodeBuddy gateway client. Stream-only, typed content blocks, `reasoning_effort: none`, per-request session/trace headers. |
| `codebuddy_store.py` | On-disk account pool (`data/codebuddy_accounts.json`) with rotation cursor and refresh-token bootstrap for email labels. |

### `handlers/proxy/` — `/ask`

| Module | Responsibility |
|:--|:--|
| `opencode.py` | Pure-stdlib OpenAI-compatible HTTP proxy. Serves `/health`, `/v1/models`, `/v1/chat/completions`, implements the upstream request contract exactly (endpoint, UA, dual session headers, verbatim tool definitions, forced streaming, canonical session ids), and strips inline reasoning tags from model output. Also `start_proxy_thread()` used by `bot.py`. |
| `client.py` | Bot-side client: `proxy_chat`, `proxy_chat_stream`, `proxy_chat_raw`, `proxy_models`, plus settings persistence. |
| `ask.py` | `/ask` command. Pre-search grounding (Firecrawl/Jina), system instruction with real Jakarta date, draft streaming, and owner-only `/getmodel`, `/setmodel`, `/thinking`, `/setsearch`. |

### `handlers/manga/`

`constants.py` (endpoints, `NH_API_KEY`, PDF semaphore), `state.py` (bounded
per-user cache plus reply-target registry), `http.py` (JSON/image/HTML fetch,
Pillow resize, `send_detail_view`), `rich.py` (Rich detail builders),
`search.py` (Rich search layer for all three sources), `pdf.py`
(`send_pdf_task`, 5 pages per batch, JPEG q75), `mangadex.py`, `maid.py`,
`nhentai.py`, `commands.py`, `callbacks.py`, `__init__.py`.

### `handlers/moderation/`

`auth.py` (`is_owner`, `is_admin_or_owner`), `permissions.py` (three-way checks:
moderation enabled, caller privileged, bot privileged), `actions.py` (ban, kick,
mute, promote, and friends), `commands.py`, `helpers.py`
(`resolve_target_user_id` supporting reply, numeric id, `@username`, and
text-mention), `sudo.py` (`data/sudouser.sqlite3`).

### `handlers/stats/`

`command.py` (`/stats`, owner-only), `renderer.py` (modern light-mode dashboard
with Poppins), `runtime_info.py`, `system_info.py`, `diagnostics.py`
(includes a live Quote API health probe), `formatting.py`.

### `handlers/payment/`

| Module | Responsibility |
|:--|:--|
| `buypremium.py` | Dynamic QRIS checkout through NasionalPay, local QR rendering with `qrcode`, 15-second polling with a hard 5-minute deadline, and manual refresh. |
| `notify.py` | Owner transaction feed: one template for every product (`premium`, `caca`, `vps`) and every event (`new`, `success`, `expired`, `cancelled`, `failed`), delivered to all `BOT_OWNER_ID` entries. |
| `__init__.py` | Re-exports the NasionalPay client surface (`create_qris`, `check_payment`, `configured`, `balance`, `verify_webhook`, `NasionalPayError`). |

### Explorer UI packages

Each follows the same file set: `constants.py`, `state.py`, `scraper.py`,
`render.py`, `rich.py`, `commands.py`, `callbacks.py`, `__init__.py`.

| Package | Command | Prefix | Source | Paging |
|:--|:--|:--|:--|:--|
| `moviebox/` | `/moviebox` | `mb:` | officialmoviebox.com | 50/page, 200 max, 59 sections |
| `anichin/` | `/anichin` | `ac:` | anichin.moe | 5/page, 100 max |
| `nekopoi/` | `/nekopoi` | `nk:` | nekopoi.care | 5/page, 100 max |
| `punishworld/` | `/punish` | `pw:` | punishworld.com | 5/page, 100 max, 126 categories |
| `simontok/` | `/simontok` | `sm:` | simontok.study | 5/page, 100 max |
| `cosxplay/` | `/cosxplay` | `cx:` | cosxplay.com | 5/page, 100 max, 176 categories |
| `retrotubeexplorer/` | `/becekku`, `/lendirqu` | `bq:`, `lq:` | two WP-Script sites | 5/page, 100 max |
| `drakor/` | `/drakorid` | `dk:` | drakorid.co | 5/page, 100 max |
| `firmware/` | `/firmware` | `fw:` | xmfirmwareupdater.com | 20/page, 200 max |

### `handlers/asupan/`

| Module | Responsibility |
|:--|:--|
| `commands.py` | `/asupan`, `/asupann`, `/autodel`, plus the asupan callback. |
| `fetcher.py` | Pool manager with per-keyword video pools (64 pool cap, 64 lock cap) and the vendored-search path. |
| `tiktoksource.py` | Async wrapper and pacing gate (`ASUPAN_TIKTOKAPI_INTERVAL`, default 2.0 s) around the vendored client. |
| `tiktokapi/` | Vendored MIT/Apache client: `signer` (pure-Python `X-Dynosaur`/`X-Gnarly`), `fetch`, `parsers`, `refs`, `search`, `videos`, `shared`, `config`, `cache_config`, `scraper_errors`. **Do not edit** — re-vendor from upstream. |
| `auth.py`, `cache.py`, `jobs.py`, `keyboards.py`, `state.py`, `constants.py` | Support modules for the above. |

### `rag/`

| Module | Responsibility |
|:--|:--|
| `loader.py` | Reads `data/rag_docs/*.md` (10 topic files). |
| `chunker.py` | Heading-aware chunking, ~600 characters per chunk. |
| `catalog.py` | `build_command_roster()` extracts every command name from the docs into a ~170-token roster. |
| `retriever.py` | In-memory SQLite FTS5 index with BM25. Intent gate, Indonesian stopwords, roster injection, relevance threshold, and `[RAG] hit/skip/miss` observability logs. |
| `prompt.py` | `<bot_reference>` prompt synthesis helper. |

### `utils/`

| Module | Responsibility |
|:--|:--|
| `menu_lock.py` | `register_message_owner`, `message_id_from_send`, `is_owner`, `guard_callback_owner`, `get_message_owner` — the fail-closed inline-button ownership system. |
| `rich_common.py` | `is_message_not_modified()` — shared no-op guard used by every rich edit helper. |
| `rich_msg.py` | `Report` builder (`add`, `add_raw`, `add_text`, `build`) plus `send_rich`/`edit_rich` for structured key-value screens. |
| `rich_search.py` | `build_search_content()` — the shared compact search table (`#`/`Title`/`Info`) with `Prev`/`Back`/`Close`/`Next`, used by every search explorer. |
| `rich_stream.py` | Streaming draft updater for AI answers. |
| `scrapling_browser.py` | Singleton `StealthySession` on a single-worker thread pool, cookie store, `warmup_browser`, `close_browser`, session recycling on failure. |
| `nasionalpay.py` | NasionalPay QRIS client (`aiohttp`, Store Key in the JSON body): `create_qris`, `check_payment`, `balance`, `verify_webhook` (HMAC-SHA256), string-amount and UTC-timestamp normalization, retry on 429/5xx. |
| `http.py` | Shared `aiohttp` session with tuned timeouts and pool limits; `get_http_session`, `close_http_session`. |
| `firecrawl.py` | `web_search` (fast/content depth) and `read_url`. |
| `jina.py` | `s.jina.ai` SERP and `r.jina.ai` reader fallback. |
| `timectx.py` | Single source for Asia/Jakarta time context (`now_jakarta`, `time_context`, `time_context_short`, `current_year`). |
| `recent_messages.py` | Rolling per-chat buffer (`remember`, `get_window`, `get_slice_after`, `forget`) used by quote range commands. |
| `caca_memory.py`, `ai_memory.py`, `groq_memory.py` | Conversation memory stores with turn/row caps and cleanup. |
| `nsfw.py` | NSFW state helper. |
| `config.py` | Central env accessor with `require_env`/`require_env_list` validation at import time. |
| `fonts.py` | Poppins font loader. |
| `text.py` | Sanitizing and formatting helpers. |
| `logger.py` | Command/reply logger forwarding to `LOG_CHAT_ID` (group 100). |
| `startup.py` | Startup warmups: browser, caches, background loops. |
| `stop_draft.py` | Listener for `stopped_message_generation` (group 101). |
| `user_collector.py` | User metadata collector (group 51). |

### `scripts/`

| Script | Purpose |
|:--|:--|
| `ig_login.py` | One-shot Android mobile-session login for Instagram (`instagrapi`), writes `data/ig_session.json`. Password never persisted. |
| `ig_status.py` | Health and validity report for the saved session. |
| `ig_resolve.py` | CLI resolver to test age-restricted posts end-to-end. |

### `tests/`

`test_caca_billing.py` — standalone, mock-based billing test covering lot
creation, FEFO consumption, reservation settlement, and idempotent fulfilment.
Run with `venv/bin/python tests/test_caca_billing.py`. No real money moves.

### `data/rag_docs/`

`about.md`, `ai_chat.md`, `downloader.md`, `explorer.md`, `games.md`,
`group_admin.md`, `manga.md`, `premium.md`, `utility.md`, `welcome.md`.
These files are the bot's only source of self-knowledge for `/ask`, `/groq`,
and `/caca`. **Any new command, platform, or explorer must be documented here**
in `- \`/command <args>\`: description.` bullet form, or the AI will invent
commands that do not exist.

---

## Handler Pipeline (PTB Groups)

`bot.py` calls four registration functions in this exact order:
`register_commands`, `register_messages`, `register_callbacks`,
`register_join_handlers`.

| Group | Handler | Module | Role |
|:--|:--|:--|:--|
| -101 | `MessageHandler(COMMAND)` | `join.py` | Central join gate. Every registered command is gated. Only exception: `/start verify_<token>`. |
| -100 | `MessageHandler(ALL)` | `blacklist.py` | Drops messages from blacklisted users/chats. |
| -100 | `ChatMemberHandler` | `join.py` | Realtime join/leave detection for the support channel. |
| -99 | `CallbackQueryHandler(.*)` | `blacklist.py` | Drops callbacks from blacklisted users/chats. |
| -96 | `MessageHandler(TEXT \| CAPTION)` | `messages.py` | Caca custom top-up amount, AWS account credential capture. |
| -90 | `MessageHandler(TEXT & ~COMMAND)` | `dl/router.py` | Auto-download link detection. |
| -89 | `MessageHandler(TEXT \| CAPTION)` | `vps/firewall.py` | Reply-scoped firewall port input. |
| -4 | `MessageHandler(REPLY & TEXT & ~COMMAND)` | `asahotak.py` | Quiz answers. |
| -3 | `MessageHandler(REPLY & TEXT & ~COMMAND)` | `susunkata.py` | Word-scramble answers. |
| -2 | `MessageHandler(AI_REPLY_FILTER)` | `messages.py` | AI reply router (`/ask`, `/groq`, Caca). |
| -2 | `MessageHandler(CACA_CAPTION_FILTER)` | `messages.py` | Caca with photo/sticker captions (PTB commands never match captions). |
| -1 | `CommandHandler` | `commands.py` | All 119 handler entries (≈180 command names once the `/q`-family lists expand). |
| 0 | `CallbackQueryHandler` | `callbacks.py` | All callback prefixes. |
| 2 | `NEW_CHAT_MEMBERS` + `ChatMemberHandler` | `welcome.py` | Welcome and verification. |
| 3 | `REPLY & TEXT` | `messages.py` | Reply-based deletion. |
| 4 | `TEXT & ~COMMAND` | `prefix_dollar.py` | `$command` prefix router. |
| 50 / 51 / 52 | `MessageHandler(ALL)` | `collector.py`, `user_collector.py`, `messages.py` | Group metadata, user metadata, quotly message buffer. |
| 100 | `MessageHandler(ALL)` | `utils/logger.py` | Forwards activity to `LOG_CHAT_ID`. |
| 101 | `TypeHandler(Update)` | `utils/stop_draft.py` | Handles `stopped_message_generation`. |

### Callback handler ordering rules

PTB executes **one handler per group**: `Application.process_update` stops at
the first handler whose `check_update()` is truthy. Overlapping prefixes are
therefore a silent-failure hazard — the button spins, nothing renders, no log
line appears.

Two rules follow from this and must be preserved:

1. **`grant_callback` is registered before `vpsowner_callback`,** and
   `vpsowner_callback` carries a negative lookahead excluding the grant
   prefixes:

   ```python
   app.add_handler(CallbackQueryHandler(vps_callback, pattern=r"^vps:"))
   app.add_handler(CallbackQueryHandler(
       grant_callback, pattern=r"^vpsown:(gtier|gdays|gregion|gos|ggo|ggrant|gcancel)"))
   app.add_handler(CallbackQueryHandler(
       vpsowner_callback,
       pattern=r"^vpsown:(?!gtier:|gdays:|gregion:|gos:|ggo:|ggrant:|gcancel$)"))
   ```

2. **Callback prefixes must stay disjoint.** Current allocation, in
   registration order: `help:`, `gsearch:`, `dlask:`, `dlres:`, `ttslide:`,
   `dl:`, `asupan:`, `helpowner:`, `buyprem:`, `reminder:`,
   `waifu:…:next` / `waifu:…:pref`, `music_(download|page|cancel):`,
   `quizans:`, `broadcast:`, `setting:`, `mgdex:` / `mgmaid:` / `mgnh:` and
   the legacy `readmanga_` / `switchch_` / `nav_` / `detailmanga_` /
   `nhdetail_` / `nhread_` / `nhnav_` / `maiddet_` / `maidread_` /
   `maidnav_` / `pdfdex_` / `pdfmaid_` / `pdfnh_` / `close_manga` /
   `ignore`, `dlengine:`, `lq:`, `bq:`, `nk:`, `pw:`, `cx:`, `sm:`, `dk:`,
   `mb:`, `ac:`, `fw:`, `vmethod:`, `cacabill:`, `cacaprov:`, `vps:`,
   `vpsown:`, `vpsacc:`, `fwr:`.

### Ownership guard ordering

Every downloader callback calls `guard_callback_owner(q)` **first**, before
anything else — including `close`. The ownership check must also precede
`require_join_or_block`: otherwise a stranger pressing somebody else's button
triggers a join-gate broadcast into the group. On callback queries inside a
group, the join gate answers with a popup alert and never sends a chat message.

---

## Downloader Subsystem

### Entry points

1. **Auto-detect** — any text message containing a URL from
   `AUTO_DOWNLOAD_DOMAINS` triggers `auto_dl_detect` on group `-90`.
   In groups, the join gate runs silently; refusal is not broadcast.
2. **`/dl <url> [url...]`** — explicit. Up to 5 URLs are treated as a batch.
3. **Explorer download buttons** — route through `_process_choice()`, which
   opens the resolution picker when the engine supports one.

### Gate order for every entry point

```
ownership guard → join gate → blacklist → premium/NSFW gate
    → per-user rate limit (3 per 60 s, non-premium only) → worker
```

The final worker re-checks premium access so a stale picker cannot bypass it.

### Premium domain policy

- `PREMIUM_ONLY_DOMAINS` — requires premium **and** (private chat or group with
  NSFW enabled). 58 entries including wildcard TLD rotation (`bunkr.*`,
  `becekku.*`, `lendirqu.*`, `bokepcrot.*`). A wildcard matches exactly one
  label and never an unrelated suffix.
- `PREMIUM_NO_NSFW_DOMAINS` — requires premium only, no NSFW condition:
  `drakorid.co`, `anichin.moe`, `officialmoviebox.com`, `moviebox.ph`,
  `moviebox.co`.
- `AUTO_DOWNLOAD_DOMAINS` — hosts that trigger auto-detection.

`/premiumbenefit` renders the union of the two premium sets directly from these
constants, so a new domain appears in the table automatically. Wildcard entries
are hidden from the table as unreadable.

### Transport selection

| Situation | Engine |
|:--|:--|
| Direct MP4 from a CDN that supports Range | `aria2c` with 16 sockets (`handlers/dl/aria2.py`) |
| HLS playlist | Parallel `.ts` segment fetch → byte concat → `ffmpeg -c copy -movflags +faststart` |
| HLS with AES-128 | Local CBC decryption per segment (fresh cipher object per segment) |
| yt-dlp territory | `yt-dlp` via `handlers/dl/proc.py` |
| Fallback | Streaming `curl_cffi` with 512 KB chunks |

`aria2c` retries once at 8 connections if the binary dies, then falls back to
streaming. Partial files and `.aria2` control files are removed before falling
back.

### Progress reporting

`TransferStats` is the single source of truth:

- `speed_bps` is computed over a 1-second sliding window (`DL_SPEED_WINDOW`).
  It is deliberately **not** `delta_bytes / delta_time` per chunk, which would
  report a frozen 15.6 MB/s for socket-burst traffic.
- `eta_seconds` is recomputed on every sample, not only on window rollover.
- `_done_edited` / `_done_logged` force exactly one 100% render and one
  completion log line, then stay silent.
- `edit_interval` and `log_interval` are clamped so `None` never reaches a
  comparison.
- Telegram status format: `<b>title</b>`, optional code line, monospace bar,
  `size/total`, `Speed:`, `ETA:`.
- Terminal format: `<Kind> progress | title=… pct=… speed=… avg_speed=… eta=…`
  and `<Kind> done | title=… size=… elapsed=… avg_speed=…`.

No engine names, connection counts, or segment counters ever appear in the
user-facing status text. Details stay in the terminal log.

### Latency log chain

`handlers/dl/router.py` writes one line per stage so a slow path is visible
without touching Telegram:

```
Link detected | platform=<X> url=... user_id=... chat_id=... delay=<s>
Resolution probe start | engine=<engine> url=... since_msg=<s>
Resolution picker ready | url=... engine=... elapsed=...s variants=[...]
Resolution picker empty | url=... engine=... elapsed=...s reason=...
Resolution callback selected | ... since_msg=<s>
Start download task | ... since_msg=<s>
Download worker start | ... since_msg=<s>
```

`since_msg` is measured from the original user message timestamp.

### Concurrency tuning

`YTDLP_SEM = 8`, `TIKTOK_LOCK = 6`, batch `Semaphore(5)`, Instagram carousel 6,
Bunkr album 6, Pawchive album 6, Nekopoi/Simontok HLS segments 12, RetroTube 10,
AsianGirl 12, Anichin OK.ru segments 12, aria2c 16 sockets.

---

## Explorer Subsystems

All ten explorers share one visual language: Rich Message Bot API 10.3 with a
plain-HTML fallback, a menu screen, browse/search listings paginated through
`utils/rich_search.build_search_content` (heading, `#`/`Title`/`Info` table,
`Prev`/`Back`/`Close`/`Next`), a detail screen, and a format/resolution picker
that feeds the downloader worker.

### `/moviebox` — movies and TV series

59 sections across three kinds: `home` (blocks from the site's operating list
such as Popular, Coming Soon, K-Drama, C-Drama, Anime, Action, Horror),
`trending` (paginated API calls deduplicated by subject id to reach 50 items),
and `filter` (country/genre/subjectType/sort). Search uses the SSR page
(`/newWeb/searchResult?keyword=`), because the POST search endpoint requires a
JWT the bot does not have. Stream resolution needs the exact `Referer` of the
video page; without it the server returns `streams: []` with HTTP 200.

Flow: menu → section or search → list (50 per page) → detail → for TV series,
season tabs and an episode grid (15 per page) → format `Video`/`MP3` →
resolution picker → download.

### `/anichin` — Donghua

Menu: Search, Schedule, Donghua List, Ongoing, Completed, Upcoming, Drop,
Genres, A–Z (letters scraped from the site, 10 per page with Prev/Next), Close.

Pagination always uses `/page/N/`; `?page=N` silently falls back to the front
page on several routes. Schedule parses `div.bixbox.schedulepage.sch_<day>`
blocks, not the `<h2>` headings.

Download resolution is pure HTTP and never yt-dlp:

1. Shortener chain (`bit.ly`, `lifehei.my.id`) → `mirrored.to/multilinks`.
2. Multilinks page → one `_links` entry per resolution.
3. `_links` → mirstats polling → `/out_url` POST → real file host.
4. Per-host direct extraction (Buzzheavier, Gofile, StreamWish, Google Drive).

When every mirror fails, the streaming server chain takes over in a fixed
order: **OK.ru → Rumble → Dailymotion → VidHide/StreamWish**, all sectioned HLS
downloaded 12-way in parallel. Pasting a series URL probes the newest episode.

### `/nekopoi` — Hentai and JAV

Browse categories (hentai, 2D animation, 3D hentai, JAV, JAV cosplay), genres,
A–Z series index fetched in one request and cached for an hour, release
schedule, and search using `quote(q, safe="")` — `quote_plus` breaks Nekopoi's
path encoding and returns zero results.

Series pages list episodes; episode pages carry the download rows. Download
priority is Streampoi HLS → Pixeldrain → Mp4Upload → DoodStream, each filling
only the resolutions the previous source lacks. Shortener hosts that return 5xx
are fast-failed and dropped so a dead link cannot shadow a live one.

### `/punish`, `/simontok`, `/cosxplay`, `/becekku`, `/lendirqu`

WP-Script-family and custom card parsers. Categories carry full nested paths
(`19621-uniform/13908-maid`) which requires a path-shaped regex, not an
`id`/`slug` split. `/punish` exposes 126 categories, `/cosxplay` 176, with
model and tag pickers alongside. All five render listings through the shared
search helper with a dynamic back button that returns to the exact picker page.

`/simontok` uses AES-256-GCM player decryption with a one-time nonce from
`/api/pk`; the origin is read from the embed URL every time because the TLD
rotates. A codec guard refuses to send an audio-only remux rather than shipping
a broken file.

### `/drakorid` — Asian drama

Uses a persisted login session (`data/drakorid_session.json`, 48-hour TTL) built
from `DRAKORID_EMAIL`/`DRAKORID_PASSWORD` to escape the guest one-download-per-
day limit. Resolution probe walks `files/` (which is actually 720p), `720p/`,
`480p/`, `360p/` and deduplicates by height.

### `/firmware` — Xiaomi ROM archive

Reads pre-rendered archive tables for HyperOS and MIUI from
`xmfirmwareupdater.com`, resolving a query against either codename or
commercial name (`munch` or `POCO F4`) from the full device map cached for a day.
Shows 20 ROMs per page up to 200, newest first, filterable by 9 regions
including Indonesia. Selecting a build opens a mirror screen with five official
CDNs (CDN Org, Aliyun SG, BN, BigOTA, HugeOTA) plus incremental OTA when
available, and the MD5 checksum.

---

## Caca AI Engine & Credit Billing

### Providers

Primary is **CodeBuddy / WorkBuddy** with a multi-account pool managed by
`/cbacc`. Fallback is **Shiteru** (`{CACA_API_BASE}/chat/completions`, default
model `deepseek-v4-flash`). `/providercaca` switches or forces a provider and
shows token status for every account.

CodeBuddy enforces three hard rules: the request must stream, the first message
must be `role: system`, and user content must be typed blocks rather than a
bare string. Thinking is disabled with `reasoning_effort: "none"`. Accounts are
rotated on 429/limit/token failure, but **only before any output has been
emitted** — falling back mid-stream would duplicate the reply.

### Persona mechanics

- The owner prompt in `caca_prompt.py` is injected verbatim, in full, inside
  the user turn (framed with instruction markers), never as `role: system`.
  Sending it as a system message triggers commercial guardrails and the model
  drops the entire persona.
- A relation block restates every turn that the conversation partner is male
  and that photos of other women must provoke jealousy. Nested only inside the
  first framed turn would leave older history unprotected.
- Time context (real WIB date and time) is injected in the framing block.
- Images are sent only on the current turn and reduced to a text placeholder
  in history, so base64 never accumulates in the database.
- Auto web search is orchestrated by the bot (detect → extract query → search →
  inject results), because upstream rejects native tool-calling payloads.

### Credit economics

| Setting | Default | Meaning |
|:--|:--|:--|
| `CACA_CREDIT_PER_RUPIAH` | 100 | 1 credit = Rp 0.01 |
| `CACA_CREDIT_PER_INPUT_TOKEN` | 1 | credits per input token |
| `CACA_CREDIT_PER_OUTPUT_TOKEN` | 2 | credits per output token |
| `CACA_MIN_CREDITS_TO_CHAT` | 50 | minimum balance to start a turn |
| `CACA_RESERVE_AMOUNT` | 2000 | reserved before each request, settled after |
| `CACA_MAX_TURNS` | 8 | turns resent per request |

Measured target: roughly 1,000 credits per chat (≈900 input + 50 output),
which yields **about 300 chats per Rp 3.000**.

Preset packages (bonus applies to presets only):

| Price | Base credits | Bonus | Total | Validity |
|:--|:--|:--|:--|:--|
| Rp 3.000 | 300.000 | — | 300.000 | 7 days |
| Rp 5.000 | 500.000 | 10.000 | 510.000 | 10 days |
| Rp 10.000 | 1.000.000 | 50.000 | 1.050.000 | 14 days |
| Rp 20.000 | 2.000.000 | 120.000 | 2.120.000 | 21 days |
| Rp 30.000 | 3.000.000 | 300.000 | 3.300.000 | 30 days |

Custom top-ups accept Rp 3.000 – Rp 200.000 with 7/14/21/30-day validity. Duration
changes expiry only, never credit count.

### Ledger invariants

- Every top-up creates its own lot with its own `expires_at`. A new purchase
  never extends an old lot.
- Consumption always draws from the lot expiring soonest (FEFO).
- Expired lots remain as history and are never usable.
- Expiry counts from `paid_at`, not `created_at`.
- Fulfilment uses `BEGIN IMMEDIATE` with a status re-check inside the
  transaction, so a duplicated payment or a forged callback cannot mint credit
  twice. Telegram callback status is never trusted; the bot re-verifies with
  `utils.nasionalpay.check_payment()`.
- Reservations lock balance before the AI call and settle against the actual
  token usage reported by the provider, with a ~4-chars-per-token estimate as
  fallback.

Owner bypass (`OWNER_ID`) and `/freecaca on` are separate code paths with
separate UI labels. Neither touches the lot table, and the balance screen never
shows "0 credits" to a user who actually has access.

---

## RAG & AI Commands

`/ask`, `/groq`, and `/caca` all draw on `rag/retriever.py`:

1. **Intent gate.** Retrieval only runs if the query contains a bot/command
   keyword from `BOT_INTENT_KEYWORDS`, a bot identity phrase, or a literal
   `/command`. A general question ("resep nasi goreng") costs zero context
   tokens.
2. **Roster injection.** Whenever the gate opens, the full command roster is
   always injected (~170 tokens) so the model cannot invent commands.
3. **Detail chunks.** Up to two chunks that clear a BM25 threshold
   (`_BM25_THRESHOLD = -0.40`), with the second required to score within 60% of
   the best.
4. **Injection format.** `<bot_reference>…</bot_reference>` inside the system
   instruction, plus a real Asia/Jakarta date so the model never claims a
   knowledge cutoff.

`/ask` additionally performs **pre-search grounding**: if the prompt looks like
it needs fresh facts, the bot searches (Firecrawl by default, Jina as
fallback), injects the result as `<web_search_results>`, and instructs the
model to answer directly without announcing that it searched.

`/caca` consumes RAG on demand only. Persona chat stays free of retrieval
context; bot-feature questions get a `[Info KiyoshiBot]` block plus an
instruction to answer in character rather than like a support desk.

Retrieval is reindexed on `/reload`, and every decision is logged:

```
[RAG] hit  | q=... terms=[...] candidates=N kept=N chunks=[...] best=... dropped_weak=N
[RAG] skip | q=... reason=general_query (0 token)
[RAG] miss | q=... candidates=N reason=all_below_threshold best=... (roster only)
```

---

## AWS VPS Reseller

### Catalogue

| Tier | Instance type (fallbacks) | vCPU | RAM | Disk (gp3) | Bandwidth |
|:--|:--|:--|:--|:--|:--|
| `1g` | `t3.micro` | 2 | 1 GB | 30 GB | 1 TB |
| `2g` | `t3.small` (`t3.micro`) | 2 | 2 GB | 60 GB | 2 TB |
| `4g` | `m7i-flex.large` (`c7i-flex.large`, `t3.small`, `t3.micro`) | 2 | 4 GB | 100 GB | 3 TB |
| `8g` | `m7i-flex.large` (`c7i-flex.large`, `t3.small`, `t3.micro`) | 2 | 8 GB | 160 GB | 5 TB |

Durations: 30, 14, and 7 days. `FEE_BPS = 200` (2% QRIS fee, grossed up to the
buyer so the owner nets the listed price). Seven-day warranty with a one-time
claimable replacement.

11 images: Ubuntu 24.04/22.04/20.04, Debian 13/12/11, Rocky 9, RHEL 9/8,
Amazon Linux 2023, Fedora 43. The OS list is filtered per region, because not
every image exists everywhere.

### Purchase flow

```
/vps → plan → duration → region (8 per page) → OS → confirm screen
      → QRIS invoice (confirmation message deleted)
      → payment polled every 15 s → claim invoice atomically
      → launch EC2 → wait for port 22 → credentials delivered
```

Post-payment, the invoice message is edited to "Preparing your VPS" and its
inline buttons are removed. Root credentials are sent only after
`wait_for_ssh()` confirms TCP 22 is open (measured ~26 s from launch).

### Multi-account pool

Accounts live in SQLite (`vps_accounts`, `vps_region_prefs`, `vps_settings`).
`resolve_chain(region)` returns an ordered list: explicit per-region
preferences first, then accounts declaring that region sorted by priority, with
the CLI `~/.aws/credentials` profile present as a virtual `default` entry
(priority 900, disableable, never deletable). Launch walks the chain until one
account succeeds, which absorbs per-region capacity and vCPU-quota failures.

Credential injection pops every inherited AWS variable before setting the
target account's keys — otherwise the default profile would leak into the wrong
account. Verification always runs against `us-east-1`, because IAM-scoped
accounts sign from the wrong region.

### Safety rules

- `account=None` is normalised to `"default"` before any AWS call. Passing
  `None` strips credentials and fails silently.
- Terminate, renew, restart, and warranty replacement always forward
  `region=inst["region"]` and `account=inst["account"]`.
- Provisioning is claimed atomically in SQLite (`PENDING → PROVISIONING → PAID`)
  so two concurrent paths cannot create two instances for one payment.
- Security groups are created per instance. A shared SG would let one customer
  open ports on another's server. Port 22 is protected from removal and from
  any rule whose range contains it.
- A failed `/vpsgrant` rolls back by terminating the instance and writing a
  `GRANT_ROLLBACK` audit entry.
- Terminated instances are retained for 7 days, then purged by the hourly
  sweeper. The owner panel hides them by default with a toggle.
- Instance classification uses the `ManagedBy=kiyoshibot` tag, not the presence
  of a DB row, so a bot-created instance with a lost DB row shows as `[bot]`
  and never as `[manual]`.

### Owner commands

`/vpsadmin` (panel), `/vpslist` (tagged bot/manual with per-region summary),
`/vpsstats`, `/vpsinfo <id>`, `/vpsterminate <id>`, `/vpsrenew <id> <days>`,
`/vpsprice` (interactive per-tier price editor accepting `20k`, `Rp 20.000`,
`12.5k`), `/vpsgrant` (full inline flow: plan → duration → region → OS →
confirm; delivery to the recipient by DM plus an owner notification),
`/addawsaccount`, `/delawsaccount`.

The account scan is cached for 300 seconds and invalidated on every state
change. Navigation buttons read cache; only `Rescan` forces a fresh sweep.

---

## Payments

Every selling flow issues a dynamic **NasionalPay** QRIS.

| Flow | Command | DB | What is sold |
|:--|:--|:--|:--|
| Premium | `/buypremium` | `data/payments.sqlite3` | Premium membership at `PREMIUM_PRICE` |
| Caca credits | `/cacatopup`, `/buycaca` | `data/caca_billing.sqlite3` | Chat credits in preset or custom packages |
| VPS | `/vps` | `data/vps.sqlite3` | EC2 instances |

Each flow renders the QR locally from the returned `qris_data` with the `qrcode`
library and falls back to the gateway-hosted PNG, then polls
`POST /api/payment/status` every 15 seconds. Every invoice also carries a manual
refresh button, so a buyer never has to wait out a poll interval. A failed check
(429/5xx/network) is **never** interpreted as "unpaid" — the last known status is
retained.

### NasionalPay specifics

The merchant API lives at `https://merchant.nasionalpay.com/api/payment`.
Authentication is the **Store Key sent in the JSON body** (`{"key": ...}`), not a
header. Transport is plain `aiohttp`; the API sits behind a normal reverse proxy,
so no browser fingerprint is needed.

| Call | Endpoint | Notes |
|:--|:--|:--|
| Create | `POST /generate` | `channel: "QRIS"`, `amount`, `player_username`. Returns `transaction_id`, `qris_data`, `qris_image`, `fee_mdr`, `nett_amount`, `expired_at` |
| Status | `POST /status` | `transaction_id` → `pending` / `success` |
| Balance | `POST /merchantbalance` | Store balance, `store_fee`, verification level |

Response quirks the client normalizes so callers never see them:

- `amount`, `fee_mdr`, and `nett_amount` are **strings**, not numbers.
- `created_at` / `expired_at` are **UTC without a timezone** — they are stamped
  as `Asia/Jakarta` on parse, otherwise invoices look seven hours old.
- The provider status is `pending` / `success`, not `PENDING` / `PAID`; it is
  mapped before anything compares it.
- The provider rejects amounts below **Rp 100**, so the VPS checkout clamps the
  grossed-up total to `max(100, ceil(gross / 100) * 100)`.
- `channel: "DANA"` currently answers `503` — QRIS is the only live channel.

### VPS purchase flow

```
/vps → plan → duration → region → operating system → confirm → QRIS → provisioning
```

Choosing an operating system does **not** create an invoice. It opens a
confirmation screen that restates the full specification (plan, vCPU, RAM,
storage, transfer, instance type, duration, region, OS, root access, warranty),
the exact price split (`Plan price` / `Admin fee` / `Total to pay`), and the
other duration prices, with **Buy Now** / **Back** / Close. Only `Buy Now`
calls the payment API. The amounts on that screen come from the same gross-up
helper the checkout uses, so the quoted price is always the charged price.

Once the QRIS message is delivered, the confirmation screen is deleted, so the
chat keeps a single live artifact — the invoice. Deletion happens *after* the
send succeeds; if delivery fails the confirmation stays put so the user can
retry instead of losing the order.

### Owner transaction feed

Every payment produces a direct message to each id in `BOT_OWNER_ID`, through
`handlers/payment/notify.py`. One template serves all three products and five
events (`new`, `success`, `expired`, `cancelled`, `failed`):

```
💳 Payment Successful

Product:      AWS VPS Instance
User ID:      5007562506
User:         Akhmad Rifqy (@rifqi1146)
Trx ID:       TRXQRJTY5TACV7I
Total Charged: Rp 1.100
Base Price:   Rp 1.000
Admin Fee:    Rp 100
Time:         2026-10-21 23:13:35 WIB

Plan:         1 GB / 2 vCPU
Duration:     7 days
Region:       ap-southeast-2
Operating System: Ubuntu 24.04 LTS
Reference:    eead5406c986
```

Product-specific blocks: VPS carries plan, duration, region, OS and the internal
`short_id`; Caca carries package type, credits and validity; Premium carries the
product title. `failed` is emitted when provisioning breaks after a successful
payment, so a paid order never disappears silently. Delivery failures are logged
and swallowed — a Telegram outage never rolls back a paid transaction.

### Admin fees are always paid by the buyer

The gateway deducts 2% from the settled amount, so charging the bare price would
leave the bot short. Every flow grosses the price up before creating the QR:

```
gross = ceil(base * 10000 / (10000 - fee_bps))   # fee_bps = 200
```

Invoices print the split explicitly — `Plan price`, `Admin fee`,
`Total to pay` — so the surcharge is never a surprise. NasionalPay additionally
exposes a *store markup* (`Fee Merchant %`, currently `0%`) in its dashboard that
is applied on top of whatever the bot sends; that knob stays in the dashboard so
changing it never requires a deploy.

NationalPay can deliver webhooks (`X-NasionalPay-Signature`, HMAC-SHA256 hex over
the raw body keyed by the Store Key) and `utils/nasionalpay.py:verify_webhook()`
implements verification, but the bot has no public HTTP listener, so polling is
the only live path.

## Moderation & Permissions

### Permission tiers

```
owner  (BOT_OWNER_ID)          implicit premium; sole access to /premium, /backup,
                               /restore, /autobackup, /broadcast, /addsudo,
                               /rmsudo, /cookies, /reload, /restart, /update,
                               /stats, /helpowner, /vpsadmin, /cacatopup admin
  ↓
sudo   (data/sudouser.sqlite3) bypasses admin-or-owner checks in moderation;
                               no owner-only commands
  ↓
group admin / creator          moderation actions while moderation is enabled
  ↓
premium (data/caca.sqlite3)    unlimited download rate, premium domains,
                               Caca (via credits), persona modes
  ↓
public
```

Destructive actions require three checks to pass: moderation enabled in that
chat, caller privileged, and the bot itself privileged. Any failure is a no-op
with an explanatory message — never a silent partial action.

`/kick` bans and immediately unbans without `until_date`, so the user can rejoin
at once through an invite. `/ban` honours `until_date` for temporary bans.

### Force join

`/vps`, `/myvps`, and any other command executed in a group redirect to a
private chat via a deep link (`https://t.me/<bot>?start=vps`) rather than
opening a menu in the group. `require_join_or_block` caches membership for 120
seconds; realtime `CHAT_MEMBER` updates invalidate it immediately. If the bot
lacks permission to read the channel, the gate logs `[JOIN CHECK BROKEN]`
instead of quietly allowing everyone through.

### Welcome / verification

`welcome.py` runs a small aiohttp server (`WEB_HOST`/`WEB_PORT`, default
`0.0.0.0:8080`, commonly configured as 5050) that serves a captcha page backed by
Cloudflare Turnstile or hCaptcha. `VERIFY_TIMEOUT_SECONDS = 5 * 60`: an
unverified member is removed after five minutes. The deep link is
`{PUBLIC_URL}/verify?token=<token>`, so `PUBLIC_URL` must be the externally
reachable origin (reverse proxy or TLS terminator in front of `WEB_PORT`).

---

## Database Layer

All files live under `data/` and are gitignored.

| File | Contents |
|:--|:--|
| `caca.sqlite3` | Persona modes, memory rows, `premium_users` |
| `caca_billing.sqlite3` | Caca invoices, credit lots, reservations, usage, audit |
| `caca_memory.sqlite3` | Persona conversation history (`utils/caca_memory.py`) |
| `ai_memory.sqlite3` | `/ask` conversation memory |
| `groq_memory.sqlite3` | `/groq` conversation memory |
| `gemini_memory.sqlite3` | Legacy AI memory left over from a removed handler; nothing reads it |
| `payments.sqlite3` | `/buypremium` invoices |
| `vps.sqlite3` | VPS pricing, invoices, instances, firewall rules, AWS accounts |
| `moderation.sqlite3` | Moderation config, warns, mutes |
| `blacklist.sqlite3` | User and group blacklist |
| `nsfw.sqlite3`, `spoiler.sqlite3` | Per-group toggles |
| `join_status.sqlite3` | Support-channel membership cache |
| `auto_dl.sqlite3` | Auto-download settings |
| `asupan.sqlite3` | Asupan keyword history |
| `broadcast.sqlite3` | Collected groups/users for broadcasts |
| `shared_media.sqlite3` | Shared-media gallery |
| `ship.sqlite3` | `/ship` pair history |
| `bot.db` | Auto-backup history and metadata used by `/backup` and `/autobackup` |
| `codebuddy_accounts.json` | CodeBuddy refresh/access token pool |
| `cookies.txt` | Netscape cookies for yt-dlp/gallery-dl |
| `ig_session.json` | Instagram Android mobile session |
| `drakorid_session.json` | Drakor.id login session |
| `caca_provider.txt` | Forced Caca provider |
| `opencode_model.json` | Selected `/ask` model |

Two files are legacy and unreferenced: `data/premium.sqlite3` (premium lives in
`caca.sqlite3`) and `data/users.sqlite3`.

---

## Scheduled Jobs

| Job | Interval | Module |
|:--|:--|:--|
| Invoice payment polling | 15 s until paid or 5-minute deadline | `payment/buypremium.py`, `vps/service.py` |
| Caca invoice polling | 15 s until terminal state | `caca/caca_billing.py` |
| VPS expiry sweep + purge | hourly | `vps/sweeper.py` |
| Download TTL sweep | 1800 s (`DOWNLOADS_TTL_SEC` 7200) | `dl/sweeper.py` |
| Browser temp profile reaper | 1800 s, 4-hour TTL | `dl/sweeper.py` |
| Auto backup | 6 hours | `backup.py` |
| Welcome cache cleanup | 60 s | `welcome.py` |
| Verification timeout | 5 minutes | `welcome.py` |
| Memory cleanup | startup + `/reload` | `utils/*_memory.py` |
| Reminder / asupan | one-shot `run_once` | `reminder.py`, `asupan/jobs.py` |

---

## Installation

### Quick install

```bash
git clone https://github.com/rifqi1146/kiyoshibot.git
cd kiyoshibot
sudo bash install.sh
```

`install.sh` detects `apt`, `dnf`, `yum`, `pacman`, `zypper`, or `apk`, installs
system packages, Node.js, Deno, Ookla speedtest, then builds the venv and
installs Python dependencies.

### Manual install

```bash
apt install -y \
  python3 python3-venv python3-pip git ffmpeg curl unzip \
  build-essential libjpeg-dev zlib1g-dev cmake libssl-dev gperf

# Node.js (required by some JS-heavy extractors)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt install -y nodejs

# Deno (required by yt-dlp for YouTube signature solving)
curl -fsSL https://deno.land/install.sh | sh
sudo ln -sf /root/.deno/bin/deno /usr/local/bin/deno

# Ookla speedtest (only if using /speedtest)
curl -L https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz | tar zx
sudo mv speedtest /usr/bin/speedtest && sudo chmod +x /usr/bin/speedtest

# Python environment
git clone https://github.com/rifqi1146/kiyoshibot.git
cd kiyoshibot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

The stealth-browser path (used by Nekopoi, RetroTube, and Asupan) needs a cached
browser:

```bash
venv/bin/pip install "scrapling[fetchers]"
venv/bin/scrapling install
```

`telegram-bot-api` (the local Bot API server) is optional but strongly
recommended — it raises the upload ceiling from 50 MB to 2 GB. Build it from
`https://github.com/tdlib/telegram-bot-api`, or use the Docker Compose stack.

### Docker

```bash
cp .env.example .env   # fill required values
docker compose up -d --build
```

The Compose file runs `kiyoshi_bot` plus a `telegram-bot-api` container sharing
the `tg-api-data` volume, with `LOCAL_BOT_API_HOST=telegram-bot-api` and the
Quote API pointed at the host gateway.

---

## Environment Variables

Only `BOT_TOKEN`, `BOT_OWNER_ID`, `LOG_CHAT_ID`, `API_ID`, and `API_HASH` are
mandatory. Everything else degrades gracefully when absent.

### Required

| Variable | Meaning |
|:--|:--|
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `BOT_OWNER_ID` | Comma-separated owner user IDs. Parsed as a `set[int]`. |
| `LOG_CHAT_ID` | Log destination, usually a `-100...` group or channel |
| `API_ID` / `API_HASH` | From https://my.telegram.org — required for the local Bot API and MTProto uploads |

### AI and grounding

| Variable | Default | Meaning |
|:--|:--|:--|
| `SHITERU_KEY` | — | Caca fallback provider key |
| `CACA_API_BASE` | `https://shiteru.id/v1` | Caca fallback endpoint |
| `CACA_MODEL` | `deepseek-v4-flash` | Caca fallback model (must be non-thinking) |
| `CACA_MAX_TOKENS` | 4096 | Output cap, hard-clamped to 4096 |
| `CACA_PROVIDER` | `codebuddy` | `codebuddy` or `shiteru` |
| `CODEBUDDY_MODEL` | `deepseek-v4.1-flash` | CodeBuddy model |
| `CACA_TIMEOUT` | 60 | Per-request timeout (seconds) |
| `OPENCODE_PROXY` | 1 | Start the local `/ask` proxy |
| `OPENCODE_PROXY_HOST` / `PORT` | `127.0.0.1` / `20130` | Proxy bind address |
| `OPENCODE_PROXY_BASE` | `http://127.0.0.1:20130` | Client base URL |
| `OPENCODE_MODEL` | empty | Empty means "first free model from upstream" |
| `OPENCODE_THINKING` | `off` | Reasoning toggle |
| `OPENCODE_VERSION` | `2.0.26` | Upstream UA version component |
| `GROQ_API_KEY` | — | `/groq` only — not used by Caca |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Groq endpoint |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model |
| `GROQ_MAX_MESSAGES` | 20 | Memory window |
| `GOOGLE_API_KEY` / `GOOGLE_CSE_ID` | — | `/gsearch` |
| `JINA_API_KEY` | — | Jina SERP + reader |
| `FIRECRAWL_API_KEY` | — | Firecrawl search + scrape |
| `SEARCH_ENGINE` | `firecrawl` | `firecrawl` or `jina` |
| `SEARCH_DEPTH` | `fast` | `fast` (SERP only) or `content` (SERP + pages) |
| `NEOXR_API_KEY` | — | Instagram age-gated fallback |
| `NH_API_KEY` | — | nHentai API key |

### Caca billing

| Variable | Default |
|:--|:--|
| `CACA_CREDIT_PER_RUPIAH` | 100 |
| `CACA_CREDIT_PER_INPUT_TOKEN` | 1 |
| `CACA_CREDIT_PER_OUTPUT_TOKEN` | 2 |
| `CACA_MIN_CREDITS_TO_CHAT` | 50 |
| `CACA_RESERVE_AMOUNT` | 2000 |
| `CACA_MAX_TURNS` | 8 |
| `PAYMENT_FEE_BPS` | 200 |

### Payments

| Variable | Default | Meaning |
|:--|:--|:--|
| `NASIONALPAY_BASE_URL` | `https://merchant.nasionalpay.com` | Gateway origin |
| `NASIONALPAY_STORE_KEY` | — | Server-side only. Acts as both API key and webhook HMAC secret. |
| `PREMIUM_PRICE` | 20000 | `/buypremium` price in rupiah |
| `PAYMENT_FEE_BPS` | 200 | Gross-up basis points passed to the buyer |

### Telegram infra and community

| Variable | Default | Meaning |
|:--|:--|:--|
| `LOCAL_BOT_API_HOST` / `PORT` | `127.0.0.1` / `8081` | Local Bot API address |
| `PREFER_LOCAL_BOT_API` | 1 | Fall back to cloud API if the probe fails |
| `QUOTE_API_URI` | `http://127.0.0.1:3000` | Quote renderer service |
| `SUPPORT_CH_ID` / `SUPPORT_CH_LINK` | — | Force-join channel |
| `DONATE_URL` | — | `/donate` target |
| `WEB_HOST` | `0.0.0.0` | Captcha server bind address |
| `WEB_PORT` | 8080 (commonly 5050) | Captcha verification server port |
| `PUBLIC_URL` | `http://<host>:<port>` | Public root for verification links |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | — | Cloudflare Turnstile |
| `HCAPTCHA_SITE_KEY` / `HCAPTCHA_SECRET_KEY` | — | hCaptcha alternative |
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_AUTH_TOKEN` | — | Workers AI, numbered suffixes for fallbacks |
| `DRAKORID_EMAIL` / `DRAKORID_PASSWORD` | — | Removes the Drakor.id guest limit |
| `IG_SESSION_ENABLED` | 1 | Use the saved Instagram mobile session |
| `IG_LOGIN_USERNAME` / `IG_LOGIN_PASSWORD` / `IG_LOGIN_2FA` | — | Consumed only by `scripts/ig_login.py` |
| `ASUPAN_TIKTOKAPI` | 1 | `0` disables the vendored TikTok client (tikwm only) |
| `ASUPAN_TIKTOKAPI_INTERVAL` | 2.0 | Pacing between signed calls (seconds) |

### Downloader tuning

| Variable | Default | Meaning |
|:--|:--|:--|
| `DL_PROGRESS_EDIT_INTERVAL` | 5.0 | Telegram edit floor (do not lower) |
| `DL_PROGRESS_LOG_INTERVAL` | 2.5 | Terminal progress cadence |
| `DL_SPEED_WINDOW` | 1.0 | Speed calculation window (seconds) |
| `DL_EDIT_CACHE_MAX` / `DL_EDIT_CACHE_TTL` | 2048 / 1800 | Edit-status cache bounds |
| `DL_GLOBAL_CONCURRENCY` / `DL_PER_USER_CONCURRENCY` | — | Worker limits |
| `DOWNLOADS_TTL_SEC` / `DOWNLOADS_SWEEP_INTERVAL_SEC` / `DOWNLOADS_GRACE_SEC` | 7200 / 1800 / — | Disk sweep |
| `TMP_BROWSER_TTL_SEC` | 14400 | Orphan browser profile age |
| `YTDLP_TIMEOUT` / `YTDLP_PROBE_TIMEOUT` / `GALLERYDL_TIMEOUT` | — | External tool timeouts |
| `FFMPEG_REMUX_TIMEOUT` / `FFMPEG_THUMB_TIMEOUT` / `FFPROBE_TIMEOUT` | — | ffmpeg/ffprobe budgets |
| `FFMPEG_HLS_TIMEOUT` | 900 | Serial HLS download budget (long videos need the headroom) |
| `ARIA2_CONNS` | 16 | aria2c socket count |
| `UPLOAD_ENGINE` | — | `mtproto` or `pyrofork` |
| `MTPROTO_UPLOAD`, `MTPROTO_FAST_UPLOAD`, `MTPROTO_PART_SIZE_KB`, `MTPROTO_PROGRESS_*` | — | MTProto uploader tuning |
| `BOT_UVLOOP` | 1 | Set `0` for debugging on the default event loop |

The AWS VPS store has **no environment variables**. Accounts are added at
runtime with `/addawsaccount` and stored in `data/vps.sqlite3`.

Copy `.env.example` to `.env` for the full annotated template.

---

## Running the Bot

```bash
cd kiyoshibot
venv/bin/python main.py
```

`main.py` checks whether port 8081 is already serving. If yes, it reuses that
`telegram-bot-api`; if not, it spawns one with `--local --http-port=8081
--dir=tgdata`. Then it starts `bot.py`. Both children are terminated together
on `SIGINT`/`SIGTERM`.

Never start a second instance against the same token — Telegram answers
`telegram.error.Conflict: terminated by other getUpdates request` and polling
stops. Kill the old PID first.

### Foreground / background

```bash
# Foreground
venv/bin/python main.py

# Background
nohup venv/bin/python bot.py > /tmp/kiyoshibot.log 2>&1 &
```

The system Python usually lacks `aiohttp`, `curl_cffi`, and the rest. Always
use the venv interpreter.

---

## Maintenance Commands

| Command | Effect | Restarts process? |
|:--|:--|:--|
| `/reload` | Refresh env → reload modules (`.state` first) → re-register handlers → refresh caches → reindex RAG. Rolls back on failure. | No |
| `/update` | `git fetch` → show diff → `git pull` → hot reload. Does **not** apply `requirements.txt` changes. | No |
| `/restart`, `/reboot` | `os.execv` in place. Same PID, connections not closed gracefully. | Yes (same PID) |
| `/backup` | Snapshot all databases | No |
| `/restore` | Restore from a backup | No |
| `/autobackup` | Toggle the 6-hour job | No |
| `/cookies` | Upload a Netscape `cookies.txt` (512 KB cap) | No |
| `/addsudo`, `/rmsudo`, `/sudolist` | Sudo tier management | No |
| `/premium add\|del <user>` | Grant/revoke premium. `del` cascades persona mode and memory rows. | No |
| `/providercaca` | Inline provider picker with account token status | No |
| `/cbacc` | CodeBuddy account pool: list/add/del/toggle/clear | No |
| `/getmodel`, `/setmodel`, `/thinking`, `/setsearch` | `/ask` runtime tuning | No |

Owner-only commands are documented in `/helpowner` and deliberately excluded
from `data/rag_docs/` so the AI never mentions commands users cannot run.

Any change to `requirements.txt`, new env variables, or system packages needs a
real restart, not `/reload`.

---

## Operational Notes & Known Quirks

**Local Bot API on port 8081.** Both `main.py` and Docker Compose may run a
`telegram-bot-api` on `0.0.0.0:8081` (SO_REUSEPORT). Two instances can coexist;
the bot connects to whichever answers first. Do not kill a `telegram-bot-api`
belonging to another deployment — a second spawn against a live `tqueue.binlog`
can abort with `SIGABRT`.

**`Task was destroyed but it is pending!`** during shutdown is expected. It
comes from the cache cleanup loop, the auto-backup loop, and the downloads
sweeper being cancelled by `SIGTERM`, not from a crash.

**`KeyError: '<dl_id>'` after `/reload`.** Caused by `.state` modules being
reloaded after their consumers, leaving two different `DL_CACHE` dicts alive.
`_module_priority()` forces every `*.state` module to priority 0. Do not reorder
it, and do not remove the `if dl_id not in DL_CACHE` guard in the router.

**Handlers swallowing text.** A message handler that matches `filters.TEXT`
without requiring a reply will consume ordinary chat and make the bot appear
"alive but mute". All multi-step text flows are reply-scoped: they key pending
state on `(chat_id, prompt_message_id)` and return immediately when
`msg.reply_to_message` is missing.

**`&bull;` in Rich HTML.** Telegram's rich HTML parser does not understand
named entities. Use literal `•` and `—` characters.

**Progress freezes at 100% after the file is complete.** Expected. `TransferStats`
forces one final render and one completion log line, then stops emitting so a
slow client socket cannot spam edits. If the percentage itself looks wrong, check
`handlers/dl/aria2.py`'s poll loop, which stops sampling as soon as
`cur >= total_size`.

**AWS `VcpuLimitExceeded`** in `us-east-1` (8 vCPU cap already consumed by
manual instances) is an account condition, not a bug. The launch chain moves to
the next account, or the tier falls back to a smaller instance type.

**Nekopoi and dead link shorteners.** `linkpoi.me` returns HTTP 500 for every
path. Dead-shortener detection fails those in ~0.36 s instead of burning ~50 s
in a stealth browser.

**Instagram 401.** The logged-out GraphQL endpoint rejects stale LSD/CSRF
tokens. The session is discarded and re-bootstrapped once; a persistent failure
usually means the post is age-gated and needs `scripts/ig_login.py`.

---

## Limitations & Things That Will Annoy You

**Site-specific scrapers break when sites change.** Every engine under
`handlers/dl/` parses markup it does not control. When a site redesigns, that
engine returns an honest error rather than a corrupt file. Nekopoi, Simontok,
MovieBox, and Anichin are the most fragile because they change hosts and
obfuscation frequently.

**Stealth browser work is slow by nature.** Bypassing Cloudflare Turnstile or
DDoS-Guard costs 15-50 seconds per handshake. The singleton browser in
`utils/scrapling_browser.py` amortizes that across requests, but the first
request after a restart pays full price.

**Free-tier LLM upstreams rotate their contract.** `/ask` works only while
`handlers/proxy/opencode.py` matches the current upstream request shape. A `403
FreeTierError` means the contract moved, not that the code is broken.

**External services are external.** NasionalPay QRIS, Shiteru, CodeBuddy, Firecrawl,
Jina, Groq, Telegram itself. When one is degraded, the feature that depends on
it is degraded. None of them are mocked into silence — failures surface.

**Uploads above 50 MB need the local Bot API.** Without `telegram-bot-api` on
port 8081, the bot falls back to the cloud API and the 2 GB MTProto path is
unavailable, even though both uploader engines are present.

**Region availability in AWS is not guaranteed.** Capacity and vCPU quotas vary
per region and per account. The bot retries across subnets, instance types, and
accounts, then reports a human-readable reason.

---

## Testing

```bash
# 1. Syntax
venv/bin/python -m py_compile handlers/vps/owner.py
venv/bin/python -m py_compile utils/nasionalpay.py handlers/vps/gateway.py

# 2. Imports (catches circular imports between dl/ and the UI layer)
venv/bin/python -c "from handlers import quotly, callbacks, commands"
venv/bin/python -c "from handlers.vps import grant_callback; print('ok')"
venv/bin/python -c "from handlers.dl.service import download_non_tiktok; print('ok')"

# 3. Static analysis
venv/bin/python -m pyflakes handlers/ database/ utils/ rag/

# 4. Quote API health (only if QUOTE_API_URI is configured)
curl http://127.0.0.1:3000/health

# 5. Billing tests (mock-backed, no real transactions)
venv/bin/python tests/test_caca_billing.py

# 6. RAG retrieval sanity check
venv/bin/python -c "
import asyncio
from rag.retriever import retrieve_context
from rag.loader import load_chunked_docs
print(asyncio.run(retrieve_context('cara download tiktok', load_chunked_docs())))"

# 7. Running process inventory
pgrep -af 'venv/bin/python (bot|main)\.py'
```

Step 7 is also the safe way to find PIDs before stopping the bot. Match on the
command line, never on a bare `python` pattern — a loose pattern will match the
shell issuing the kill.

A good smoke test after any change to handler registration is to press one
button from every callback family and confirm a log line appears. A button that
spins with no log almost always means an earlier handler swallowed the update
(see [Callback handler ordering rules](#callback-handler-ordering-rules)).

Live testing should be done in a private test chat or `LOG_CHAT_ID` — never in a
production group.

---

## Credits

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — Bot framework
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — General extractor engine
- [gallery-dl](https://github.com/mikf/gallery-dl) — Gallery extraction
- [NasionalPay](https://merchant.nasionalpay.com) — QRIS payment gateway
- [OpenCode](https://opencode.ai) — Free model backend behind `/ask`
- [Groq Cloud](https://console.groq.com) — LLM inference for `/groq`
- [Jina AI](https://jina.ai) — SERP + reader grounding
- [Firecrawl](https://www.firecrawl.dev) — SERP + scrape grounding
- [godv bot](https://github.com/govdbot/govd) — Reference for several downloader flows
- [TikWm](https://www.tikwm.com/) — TikTok URL resolution fallback
- [Pyrofork](https://github.com/Mayuri-Chan/pyrofork) — Multi-connection uploads
- [Telethon](https://github.com/LonamiWebs/Telethon) — MTProto uploads and message fetch
- [instagrapi](https://github.com/subzeroid/instagrapi) — MIT. Android private-API emulation for the optional Instagram mobile session (`scripts/ig_login.py`, `handlers/dl/instagram/mobile_session.py`)
- [omkarcloud/tiktok-scraper](https://github.com/omkarcloud/tiktok-scraper) — MIT, Copyright (c) 2026 Chetan Jain. Vendored TikTok web-API client under `handlers/asupan/tiktokapi/`
- [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) — Apache-2.0. The pure-Python `webmssdk` signer (`X-Dynosaur` / `X-Gnarly`) in `handlers/asupan/tiktokapi/signer.py` is ported from here

---

## License

GPLv3 — see [LICENSE](LICENSE).
