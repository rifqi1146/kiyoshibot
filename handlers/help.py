from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from handlers.setting import render_settings_message
from utils.rich_stream import send_rich_message, edit_rich_message, is_rich_message
from utils.text import sanitize_ai_output


async def _send_help(bot, chat, text_md: str, keyboard, reply_to=None):
    try:
        return await send_rich_message(
            bot, chat.id, text_md, reply_markup=keyboard,
            message_thread_id=getattr(reply_to, "message_thread_id", None),
            reply_to_message_id=getattr(reply_to, "message_id", None),
        )
    except Exception:
        pass
    kwargs = {"chat_id": chat.id, "text": sanitize_ai_output(text_md), "reply_markup": keyboard}
    if reply_to is not None:
        kwargs["reply_to_message_id"] = reply_to.message_id
        tid = getattr(reply_to, "message_thread_id", None)
        if tid:
            kwargs["message_thread_id"] = tid
    return await bot.send_message(parse_mode="HTML", **kwargs)


async def _edit_help(q, text_md: str, keyboard):
    msg = q.message
    if is_rich_message(msg):
        try:
            return await edit_rich_message(
                q.get_bot(), msg.chat_id, msg.message_id, text_md,
                reply_markup=keyboard,
            )
        except Exception:
            pass
    try:
        return await q.edit_message_text(
            sanitize_ai_output(text_md), reply_markup=keyboard, parse_mode="HTML"
        )
    except Exception:
        try:
            return await q.edit_message_text(sanitize_ai_output(text_md), reply_markup=keyboard)
        except Exception:
            return None


def _help_cb(user_id: int, action: str) -> str:
    return f"help:{int(user_id)}:{action}"


def help_main_keyboard(user_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Media & Downloader", callback_data=_help_cb(user_id, "media")),
            InlineKeyboardButton("Stream & Explorer", callback_data=_help_cb(user_id, "explorer")),
        ],
        [
            InlineKeyboardButton("AI Chat & Caca", callback_data=_help_cb(user_id, "ai")),
            InlineKeyboardButton("Games & Fun", callback_data=_help_cb(user_id, "games")),
        ],
        [
            InlineKeyboardButton("Utilities", callback_data=_help_cb(user_id, "utils")),
            InlineKeyboardButton("AWS VPS", callback_data=_help_cb(user_id, "vps")),
        ],
        [
            InlineKeyboardButton("User Settings", callback_data=_help_cb(user_id, "user_setting")),
            InlineKeyboardButton("Group Settings", callback_data=_help_cb(user_id, "settings")),
        ],
        [
            InlineKeyboardButton("Terms & Privacy", callback_data=_help_cb(user_id, "legal")),
            InlineKeyboardButton("Close", callback_data=_help_cb(user_id, "close")),
        ],
    ])


def help_settings_keyboard(user_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Asupan", callback_data=_help_cb(user_id, "asupan")),
            InlineKeyboardButton("AutoDel", callback_data=_help_cb(user_id, "autodel")),
        ],
        [
            InlineKeyboardButton("AutoDL", callback_data=_help_cb(user_id, "autodl")),
            InlineKeyboardButton("Caca", callback_data=_help_cb(user_id, "cacaa")),
        ],
        [
            InlineKeyboardButton("NSFW", callback_data=_help_cb(user_id, "nsfw")),
            InlineKeyboardButton("Welcome", callback_data=_help_cb(user_id, "wlc")),
        ],
        [
            InlineKeyboardButton("Back", callback_data=_help_cb(user_id, "menu")),
            InlineKeyboardButton("Close", callback_data=_help_cb(user_id, "close")),
        ],
    ])


def help_back_keyboard(user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Back", callback_data=_help_cb(user_id, "menu"))],
        [InlineKeyboardButton("Close", callback_data=_help_cb(user_id, "close"))],
    ])


def help_settings_back_keyboard(user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Back", callback_data=_help_cb(user_id, "settings"))],
        [InlineKeyboardButton("Close", callback_data=_help_cb(user_id, "close"))],
    ])


_LEGACY_ALIASES = {
    "features": "media",
    "privacy": "legal",
    "terms": "legal",
    "caca": "ai",
}


HELP_TEXT = {
    "menu": (
        "### 📖 Help Menu\n"
        "\n"
        "Select a category below to explore commands and documentation.\n"
    ),

    "media": (
        "### 📥 Media & Downloader\n"
        "\n"
        "- `/dl <url>`: Download media from supported platforms\n"
        "- `/asupan`: Get a random short video\n"
        "- `/music <query>`: Search and download songs\n"
        "- `/nobg`: Remove image background via photo reply\n"
        "- `/upscale`: Upscale and enhance image resolution\n"
        "- `/getsticker`: Extract sticker as a PNG or WEBM file\n"
        "- `/kang`: Add replied sticker to your personal pack\n"
        "- `/igstalk <user>`: View Instagram profile details\n"
        "- `/igstory <user>`: Download Instagram stories by username\n"
        "- `/share`: Share media anonymously via secret link"
    ),

    "explorer": (
        "### 🎬 Stream & Explorer\n"
        "\n"
        "- `/moviebox`: Search and stream movies or series from MovieBox\n"
        "- `/drakorid`: Browse and stream Asian dramas from Drakor.id\n"
        "- `/anichin`: Browse and download donghua from Anichin\n"
        "- `/firmware`: Official Xiaomi, Redmi, and POCO ROM archive\n"
        "- `/anime`: Search anime database\n"
        "- `/nekopoi`: Browse Nekopoi video catalog (Premium)\n"
        "- `/simontok`: Browse Simontok video catalog (Premium)\n"
        "- `/punish`: Browse PunishWorld video catalog (Premium)\n"
        "- `/becekku`: Browse Becekku video catalog (Premium)\n"
        "- `/lendirqu`: Browse LendirQu video catalog (Premium)\n"
        "- `/cosxplay`: Browse CosXplay video catalog (Premium)"
    ),

    "ai": (
        "### 🤖 AI Chat & Caca\n"
        "\n"
        "**AI Assistants**\n"
        "- `/ask <text>`: Chat with AI model featuring web grounding\n"
        "- `/groq <text>`: Fast response AI powered by Groq\n"
        "- Reply to any AI message to continue the conversation thread\n"
        "\n"
        "**Caca Companion**\n"
        "- `/caca <text>`: Chat with Caca roleplay companion\n"
        "- `/mode`: View current active persona\n"
        "- `/mode <name>`: Switch persona (default, bokep, sarkas, toxic, yandere, cabul, masokis, slave, loli)\n"
        "- `/cacatopup`: Top up Caca AI credits via dynamic QRIS\n"
        "- `/buycaca`: Same as `/cacatopup`\n"
        "- `/cacasaldo`: Check active credit balance and expiry dates"
    ),

    "games": (
        "### 🎮 Games & Fun\n"
        "\n"
        "- `/asahotak`: Brain teaser quiz game\n"
        "- `/susunkata`: Word puzzle scramble game\n"
        "- `/quiz`: Trivia quiz challenge\n"
        "- `/ship`: Match random couple pairs in groups\n"
        "- `/quoteanime`: Random quote from anime series\n"
        "- `/waifu`: Fetch a random anime character picture"
    ),

    "utils": (
        "### 🧰 Utilities\n"
        "\n"
        "- `/q [N] [r] [color]`: Generate quote sticker\n"
        "- `/qi [N] [r] [color]`: Generate quote image wallpaper\n"
        "- `/qs [N] [r] [color]`: Generate 9:16 quote for stories\n"
        "- `/ping`: Check bot response time and gateway latency\n"
        "- `/weather <city>`: Get weather report and forecast\n"
        "- `/kurs`: Currency exchange rate converter\n"
        "- `/resi <courier> <receipt>`: Track Indonesia expedition packages\n"
        "- `/tr <text>`: Translate text between languages\n"
        "- `/trlist`: List all supported translation languages\n"
        "- `/ip <address>`: Lookup geolocation and ISP info of an IP\n"
        "- `/net <host>`: Network diagnostics and route information\n"
        "- `/domain <domain>`: Domain details and DNS records\n"
        "- `/whoisdomain <domain>`: Detailed WHOIS domain registration data\n"
        "- `/reminder <time> <text>`: Set a scheduled alert reminder\n"
        "- `/buypremium`: Purchase premium membership via QRIS\n"
        "- `/premiumbenefit`: View premium perks and unlocked domains"
    ),

    "vps": (
        "### 🖥️ AWS VPS\n"
        "\n"
        "High-performance cloud servers with low-latency global routing.\n"
        "\n"
        "**How to buy**\n"
        "- `/vps`: Open the plan menu and order a new server\n"
        "- `/myvps`: Manage servers you already own\n"
        "\n"
        "**Service terms**\n"
        "- Full 7-day warranty on every plan\n"
        "- Active period up to 30 days (7, 14, or 30 days), renewable anytime\n"
        "- Full root access over SSH with password login\n"
        "- Operating system: Ubuntu 24.04, Ubuntu 22.04, or Debian 12\n"
        "- Choose server location from multiple global AWS regions\n"
        "\n"
        "**Payment**\n"
        "- Dynamic QRIS (GoPay, OVO, DANA, ShopeePay, all banks)\n"
        "- Server activates automatically 30 to 60 seconds after payment confirmation"
    ),

    "settings": (
        "### ⚙️ Group Settings\n"
        "\n"
        "Select an option below to manage group moderation and features."
    ),

    "asupan": (
        "### 📺 Asupan Settings\n"
        "\n"
        "- `/asupann enable`: Enable asupan in the group\n"
        "- `/asupann disable`: Disable asupan in the group\n"
        "- `/asupann status`: Check asupan status"
    ),

    "autodel": (
        "### ⏱ Auto Delete Asupan\n"
        "\n"
        "- `/autodel enable`: Enable auto-delete for asupan\n"
        "- `/autodel disable`: Disable auto-delete for asupan\n"
        "- `/autodel status`: Check auto-delete status"
    ),

    "autodl": (
        "### 🔗 Auto Download Link\n"
        "\n"
        "- `/autodl enable`: Enable automatic link detection\n"
        "- `/autodl disable`: Disable automatic link detection\n"
        "- `/autodl status`: Check auto-detect status"
    ),

    "cacaa": (
        "### 💬 Caca Group Settings\n"
        "\n"
        "- `/cacaa enable`: Enable Caca in the group\n"
        "- `/cacaa disable`: Disable Caca in the group\n"
        "- `/cacaa status`: Check Caca status\n"
        "- `/cacaa list`: List active groups (owner only)\n"
        "\n"
        "> Use `/help` -> **AI Chat & Caca** for personas, chat, and top-up."
    ),

    "nsfw": (
        "### 🔞 NSFW Settings\n"
        "\n"
        "- `/nsfw enable`: Enable NSFW in the group\n"
        "- `/nsfw disable`: Disable NSFW in the group\n"
        "- `/nsfw status`: Check NSFW status\n"
        "- `/spoiler enable`: Send premium media with spoiler\n"
        "- `/spoiler disable`: Send premium media normally\n"
        "- `/spoiler status`: Check spoiler status"
    ),

    "wlc": (
        "### 👋 Welcome Settings\n"
        "\n"
        "- `/wlc enable`: Enable welcome messages\n"
        "- `/wlc disable`: Disable welcome messages"
    ),

    "legal": (
        "### 📜 Terms & Privacy\n"
        "\n"
        "**Terms of Use**\n"
        "- **User Responsibility**: You are responsible for every command executed and every file downloaded.\n"
        "- **Downloader Tool**: The downloader is provided as a utility tool. You must hold rights or permission for any media downloaded.\n"
        "- **Third-Party Platforms**: This bot is not affiliated with, endorsed, or sponsored by Instagram, TikTok, YouTube, Facebook, or X. You must comply with their respective terms.\n"
        "- **No Guarantee**: Services are provided as-is without warranties of uninterrupted uptime.\n"
        "\n"
        "**Privacy Policy**\n"
        "- Commands and interaction logs may be recorded strictly for maintenance and diagnostics.\n"
        "- Recorded metadata may include Telegram User ID, Username, and timestamps.\n"
        "- Do not send passwords, identification numbers, or confidential personal data."
    ),
}


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not msg or not user:
        return

    await _send_help(
        context.bot,
        msg.chat,
        HELP_TEXT["menu"],
        help_main_keyboard(user.id),
        reply_to=msg,
    )


def _vps_help_text() -> str:
    from database import vps_db
    try:
        available = vps_db.stock_enabled()
    except Exception:
        available = True

    if not available:
        return (
            "### 🖥️ AWS VPS\n"
            "\n"
            "**Out of stock.** New server orders are temporarily closed.\n"
            "\n"
            "**Existing servers**\n"
            "- `/myvps`: Manage servers you already own\n"
            "- Restart, renew, firewall, and warranty claims keep working as usual\n"
            "\n"
            "> New orders will reopen once stock is restocked. Watch this menu for updates."
        )

    return HELP_TEXT["vps"]


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    parts = q.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "help":
        return

    try:
        owner_id = int(parts[1])
    except Exception:
        try:
            await q.answer("Invalid help menu.", show_alert=True)
        except Exception:
            pass
        return

    action = parts[2]

    if q.from_user.id != owner_id:
        try:
            await q.answer(
                "Only the user who opened this menu can access it",
                show_alert=True
            )
        except Exception:
            pass
        return

    try:
        await q.answer()
    except Exception:
        pass

    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if action == "menu":
        await _edit_help(q, HELP_TEXT["menu"], help_main_keyboard(owner_id))
        return

    if action == "settings":
        await _edit_help(q, HELP_TEXT["settings"], help_settings_keyboard(owner_id))
        return

    if action == "user_setting":
        return await render_settings_message(q.message, owner_id, source="help")

    if action == "vps":
        return await _edit_help(q, _vps_help_text(), help_back_keyboard(owner_id))

    action = _LEGACY_ALIASES.get(action, action)

    text = HELP_TEXT.get(action)
    if text:
        if action in ("asupan", "autodel", "autodl", "cacaa", "nsfw", "wlc"):
            kb = help_settings_back_keyboard(owner_id)
        else:
            kb = help_back_keyboard(owner_id)

        await _edit_help(q, text, kb)
