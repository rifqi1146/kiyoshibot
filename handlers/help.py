from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from handlers.setting import render_settings_message
from utils.rich_stream import send_rich_message, edit_rich_message, is_rich_message
from utils.text import sanitize_ai_output


def _is_dm(chat) -> bool:
    return getattr(chat, "type", None) == "private" or getattr(chat, "id", 0) > 0


async def _send_help(bot, chat, text_md: str, keyboard, reply_to=None):
    """Kirim help: DM pakai rich message, grup fallback ke sendMessage HTML."""
    if _is_dm(chat):
        try:
            return await send_rich_message(
                bot, chat.id, text_md, reply_markup=keyboard,
                reply_to_message_id=getattr(reply_to, "message_id", None),
            )
        except Exception:
            pass
    kwargs = {"chat_id": chat.id, "text": sanitize_ai_output(text_md), "reply_markup": keyboard}
    if reply_to is not None:
        kwargs["reply_to_message_id"] = reply_to.message_id
    return await bot.send_message(parse_mode="HTML", **kwargs)


async def _edit_help(q, text_md: str, keyboard):
    """Update menu: pertahankan rich message di DM, fallback HTML di grup."""
    msg = q.message
    if _is_dm(msg.chat) and is_rich_message(msg):
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
            InlineKeyboardButton("Features", callback_data=_help_cb(user_id, "features")),
            InlineKeyboardButton("AI Chat", callback_data=_help_cb(user_id, "ai")),
        ],
        [
            InlineKeyboardButton("Utilities", callback_data=_help_cb(user_id, "utils")),
            InlineKeyboardButton("Privacy", callback_data=_help_cb(user_id, "privacy")),
        ],
        [
            InlineKeyboardButton("Terms of Use", callback_data=_help_cb(user_id, "terms")),
        ],
        [
            InlineKeyboardButton("Settings", callback_data=_help_cb(user_id, "settings")),
        ],
        [
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
            InlineKeyboardButton("User Setting", callback_data=_help_cb(user_id, "user_setting")),
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


HELP_TEXT = {
    "menu": (
        "### 📖 Help Menu\n"
        "\n"
        "Pilih kategori lewat tombol di bawah 👇\n"
    ),

    "features": (
        "### 🤩 Main Features\n"
        "\n"
        "- `/anime` — Search anime\n"
        "- `/asahotak` — Random question\n"
        "- `/aidetect` — Detect AI-generated images\n"
        "- `/aitext` — Detect AI-generated text\n"
        "- `/asupan` — Random TikTok content\n"
        "- `/dl` — Download videos from supported platforms\n"
        "- `/gsearch` — Search on Google\n"
        "- `/getsticker` — Get sticker as PNG or WEBM file\n"
        "- `/igstalk` — Stalking Instagram account\n"
        "- `/igstory` — Download story Instagram via username\n"
        "- `/kang` — Add sticker to your pack\n"
        "- `/kurs` — Currency conversion\n"
        "- `/music` — Search music\n"
        "- `/nobg` — Remove image background\n"
        "- `/q` — Create quote sticker\n"
        "- `/quoteanime` — Random anime quotes\n"
        "- `/reminder` — Schedule a reminder\n"
        "- `/resi` — Track packages, Indonesia expedition only\n"
        "- `/ship` — Choose a couple\n"
        "- `/share` — Share media anonymous\n"
        "- `/susunkata` — Play word arrangement game\n"
        "- `/tr` — Translate text between languages\n"
        "- `/trlist` — List supported languages\n"
        "- `/upscale` — Upscale images\n"
        "- `/waifu` — Get a waifu\n"
        "- `/weather` — Get weather information\n"
        "\n"
        "> Tip: ketik command-nya aja langsung, tanpa perlu argumen tambahan 😉"
    ),

    "ai": (
        "### 🤖 AI Chat\n"
        "\n"
        "- `/ask` — Chat with Gemini\n"
        "- `/groq` — Chat with Groq\n"
        "- `/caca` — Caca Chat Bot\n"
        "\n"
        "> Balas pesan bot dengan pertanyaan buat lanjutin obrolan."
    ),

    "utils": (
        "### 🧰 Utilities\n"
        "\n"
        "- `/ping` — Check bot response time\n"
        "- `/stats` — Bot & system statistics\n"
        "- `/ip` — IP address lookup\n"
        "- `/net` — all in one network information\n"
        "- `/domain` — Domain information\n"
        "- `/whoisdomain` — Detailed domain lookup"
    ),

    "privacy": (
        "### 🔒 User Privacy\n"
        "\n"
        "By using this bot, users understand and agree that:\n"
        "\n"
        "- The bot owner may view and store the command history used by users\n"
        "- The recorded data may include:\n"
        "  - Telegram user ID\n"
        "  - Username (if available)\n"
        "  - Commands used\n"
        "  - Usage time (timestamp)\n"
        "\n"
        "This data is used only for:\n"
        "- Development\n"
        "- Maintenance\n"
        "- Service improvement\n"
        "\n"
        "> **❗ Do not send passwords, identification numbers, or other sensitive data.**\n"
        "\n"
        "By continuing to use this bot, users are considered to have agreed to this policy."
    ),

    "terms": (
        "### 📜 Terms of Use\n"
        "\n"
        "By using this bot, you agree to these terms and conditions:\n"
        "\n"
        "**1. User Responsibility**\n"
        "You’re responsible for everything you do on this site. Every command you run, every link you click, every file you download with this bot.\n"
        "\n"
        "**2. Downloader Usage**\n"
        "The downloader is just a tool. You are on your own to ensure that you have the right or permission to download, save, share, or reuse anything from other platforms.\n"
        "\n"
        "This bot is not intended to grab or share any content that is:\n"
        "- Copyrighted and you don’t have permission to use;\n"
        "- Private or restricted access;\n"
        "- Paid stuff you haven’t been granted permission to get;\n"
        "- Illegal, harmful, abusive, or in violation of someone else’s rights.\n"
        "\n"
        "**3. Third-Party Platforms**\n"
        "This bot is not affiliated with, endorsed, or sponsored by Instagram, TikTok, YouTube, Facebook, X, or any other platform. You must respect their respective terms of service when downloading their content.\n"
        "\n"
        "**4. No Ownership Claim**\n"
        "Everything you download through this bot will stay the property of its creator. The bot doesn’t claim any rights to it.\n"
        "\n"
        "**5. No Guarantee**\n"
        "You get the bot as-is. Features can break, change, disappear, or be limited at any time due to technical stuff, platform updates, rate limits, or maintenance.\n"
        "\n"
        "**6. Abuse and Restrictions**\n"
        "If you abuse the bot, spam commands, overburden it, or use it for malicious stuff, the bot owner may block, limit, or disable your access to it.\n"
        "\n"
        "As long as you continue to use the bot, you agree to this."
    ),

    "settings": (
        "### ⚙️ Bot Settings\n"
        "\n"
        "Select a menu below to see detailed options for each feature."
    ),

    "asupan": (
        "### 📺 Asupan Settings\n"
        "\n"
        "- `/asupann enable` — Enable asupan in the group\n"
        "- `/asupann disable` — Disable asupan in the group\n"
        "- `/asupann status` — Check asupan status"
    ),

    "autodel": (
        "### ⏱ Auto Delete Asupan\n"
        "\n"
        "- `/autodel enable` — Enable auto-delete for asupan\n"
        "- `/autodel disable` — Disable auto-delete for asupan\n"
        "- `/autodel status` — Check auto-delete status"
    ),

    "autodl": (
        "### 🔗 Auto Download Link\n"
        "\n"
        "- `/autodl enable` — Enable automatic link detection\n"
        "- `/autodl disable` — Disable automatic link detection\n"
        "- `/autodl status` — Check auto-detect status"
    ),

    "cacaa": (
        "### 💬 Caca Settings\n"
        "\n"
        "- `/mode` — Change Caca persona (Premium Only)\n"
        "- `/cacaa enable` — Enable Caca in the group\n"
        "- `/cacaa disable` — Disable Caca in the group\n"
        "- `/cacaa status` — Check Caca status"
    ),

    "nsfw": (
        "### 🔞 NSFW Settings\n"
        "\n"
        "- `/nsfw enable` — Enable NSFW in the group\n"
        "- `/nsfw disable` — Disable NSFW in the group\n"
        "- `/nsfw status` — Check NSFW status"
    ),

    "wlc": (
        "### 👋 Welcome Settings\n"
        "\n"
        "- `/wlc enable` — Enable welcome messages\n"
        "- `/wlc disable` — Disable welcome messages"
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
            await q.answer("Menu help tidak valid.", show_alert=True)
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

    text = HELP_TEXT.get(action)
    if text:
        if action in ("asupan", "autodel", "autodl", "cacaa", "nsfw", "wlc"):
            kb = help_settings_back_keyboard(owner_id)
        else:
            kb = help_back_keyboard(owner_id)

        await _edit_help(q, text, kb)

