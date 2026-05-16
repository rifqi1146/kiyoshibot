from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from handlers.setting import render_settings_message


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
        "📖 <b>Help Menu</b>\n"
        "Select a category to see available commands."
    ),

    "features": (
        "<b>Main Features</b>\n\n"
        "• <code>/anime</code> — Search anime\n"
        "• <code>/aidetect</code> — Detect AI-generated images\n"
        "• <code>/aitext</code> — Detect AI-generated text\n"
        "• <code>/asupan</code> — Random TikTok content\n"
        "• <code>/dl</code> — Download videos from supported platforms\n"
        "• <code>/gsearch</code> — Search on Google\n"
        "• <code>/getsticker</code> — Get sticker as PNG or WEBM file\n"
        "• <code>/kang</code> — Add sticker to your pack\n"
        "• <code>/kurs</code> — Currency conversion\n"
        "• <code>/music</code> — Search music\n"
        "• <code>/nobg</code> — Remove image background\n"
        "• <code>/q</code> — Create quote sticker\n"
        "• <code>/quoteanime</code> — Random anime quotes\n"
        "• <code>/reminder</code> — Schedule a reminder\n"
        "• <code>/resi</code> — Track packages, Indonesia expedition only\n"
        "• <code>/ship</code> — Choose a couple\n"
        "• <code>/susunkata</code> — Play word arrangement game\n"
        "• <code>/tr</code> — Translate text between languages\n"
        "• <code>/trlist</code> — List supported languages\n"
        "• <code>/upscale</code> — Upscale images\n"
        "• <code>/waifu</code> — Get a waifu\n"
        "• <code>/weather</code> — Get weather information\n"
    ),

    "ai": (
        "<b>AI Chat</b>\n\n"
        "• <code>/ask</code> — Chat with Gemini\n"
        "• <code>/groq</code> — Chat with Groq\n"
        "• <code>/caca</code> — Caca Chat Bot\n"
    ),

    "utils": (
        "<b>Utilities</b>\n\n"
        "• <code>/ping</code> — Check bot response time\n"
        "• <code>/stats</code> — Bot & system statistics\n"
        "• <code>/ip</code> — IP address lookup\n"
        "• <code>/net</code> — all in one network information\n"
        "• <code>/domain</code> — Domain information\n"
        "• <code>/whoisdomain</code> — Detailed domain lookup\n"
    ),

    "privacy": (
        "<b>User Privacy</b>\n\n"
        "By using this bot, users understand and agree that:\n\n"
        "• The bot owner may view and store the command history used by users\n"
        "• The recorded data may include:\n"
        "  - Telegram user ID\n"
        "  - Username (if available)\n"
        "  - Commands used\n"
        "  - Usage time (timestamp)\n\n"
        "This data is used only for:\n"
        "• Development\n"
        "• Maintenance\n"
        "• Service improvement\n\n"
        "<b>❗ Do not send passwords, identification numbers, or other sensitive data.</b>\n\n"
        "By continuing to use this bot, users are considered to have agreed to this policy."
    ),

    "terms": (
        "<b>Terms of Use</b>\n"
        "By using this bot, you agree to these terms and conditions:\n\n"

        "<b>1. User Responsibility</b>\n"
        "You’re responsible for everything you do on this site. Every command you run, every link you click, every file you download with this bot.\n\n"

        "<b>2. Downloader Usage</b>\n"
        "The downloader is just a tool. You are on your own to ensure that you have the right or permission to download, save, share, or reuse anything from other platforms.\n\n"

        "This bot is not intended to grab or share any content that is: "
        "• Copyrighted and you don’t have permission to use; "
        "• Private or restricted access; "
        "• Paid stuff you haven’t been granted permission to get; "
        "• Illegal, harmful, abusive, or in violation of someone else’s rights.\n\n"

        "<b>3. Third-Party Platforms</b>\n"
        "You shouldn’t be on Instagram, TikTok, YouTube, Facebook, X, or any other platform. They have their own rules and terms that you should follow.\n\n"

        "<b>4. No Ownership Claim</b>\n"
        "Everything you download through this bot will stay the property of its creator. The bot doesn’t claim any rights to it.\n\n"

        "<b>5. No Guarantee</b>\n"
        "You get the bot as-is. Features can break, change, disappear, or be limited at any time due to technical stuff, platform updates, rate limits, or maintenance.\n\n"

        "<b>6. Abuse and Restrictions</b>\n"
        "If you abuse the bot, spam commands, overburden it, or use it for malicious stuff, the bot owner may block, limit, or disable your access to it.\n\n"

        "As long as you continue to use the bot, you agree to this."
    ),
    
    "settings": (
        "<b>Bot Settings</b>\n\n"
        "Select a menu below to see detailed options for each feature."
    ),

    "asupan": (
        "<b>Asupan Settings</b>\n\n"
        "• <code>/asupann enable</code> — Enable asupan in the group\n"
        "• <code>/asupann disable</code> — Disable asupan in the group\n"
        "• <code>/asupann status</code> — Check asupan status\n\n"
    ),

    "autodel": (
        "<b>Auto Delete Asupan</b>\n\n"
        "• <code>/autodel enable</code> — Enable auto-delete for asupan\n"
        "• <code>/autodel disable</code> — Disable auto-delete for asupan\n"
        "• <code>/autodel status</code> — Check auto-delete status\n\n"
    ),

    "autodl": (
        "<b>Auto Download Link</b>\n\n"
        "• <code>/autodl enable</code> — Enable automatic link detection\n"
        "• <code>/autodl disable</code> — Disable automatic link detection\n"
        "• <code>/autodl status</code> — Check auto-detect status\n\n"
    ),

    "cacaa": (
        "<b>Caca Settings</b>\n\n"
        "• <code>/mode</code> — Change Caca persona (Premium Only)\n"
        "• <code>/cacaa enable</code> — Enable Caca in the group\n"
        "• <code>/cacaa disable</code> — Disable Caca in the group\n"
        "• <code>/cacaa status</code> — Check Caca status\n\n"
    ),

    "nsfw": (
        "<b>NSFW Settings</b>\n\n"
        "• <code>/nsfw enable</code> — Enable NSFW in the group\n"
        "• <code>/nsfw disable</code> — Disable NSFW in the group\n"
        "• <code>/nsfw status</code> — Check NSFW status\n\n"
    ),

    "wlc": (
        "<b>Welcome Settings</b>\n\n"
        "• <code>/wlc enable</code> — Enable welcome messages\n"
        "• <code>/wlc disable</code> — Disable welcome messages\n\n"
    ),
}


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message or not user:
        return

    await update.message.reply_text(
        HELP_TEXT["menu"],
        reply_markup=help_main_keyboard(user.id),
        parse_mode="HTML"
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
        await q.edit_message_text(
            HELP_TEXT["menu"],
            reply_markup=help_main_keyboard(owner_id),
            parse_mode="HTML"
        )
        return

    if action == "settings":
        await q.edit_message_text(
            HELP_TEXT["settings"],
            reply_markup=help_settings_keyboard(owner_id),
            parse_mode="HTML"
        )
        return

    if action == "user_setting":
        return await render_settings_message(q.message, owner_id, source="help")

    text = HELP_TEXT.get(action)
    if text:
        if action in ("asupan", "autodel", "autodl", "cacaa", "nsfw", "wlc"):
            kb = help_settings_back_keyboard(owner_id)
        else:
            kb = help_back_keyboard(owner_id)

        await q.edit_message_text(
            text,
            reply_markup=kb,
            parse_mode="HTML"
        )

