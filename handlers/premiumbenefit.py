import logging
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from handlers.dl.constants import PREMIUM_ONLY_DOMAINS, PREMIUM_NO_NSFW_DOMAINS

log = logging.getLogger(__name__)

BENEFITS = (
    (
        "Downloader",
        (
            ("/dl", "Unlimited downloads (bypasses 3 processes per minute)."),
            ("/dl", "Video resolution up to 1080p+."),
        ),
    ),
    (
        "Search & Download",
        (
            ("/nekopoi", "Search and download from Nekopoi."),
            ("/punish", "Search and download from PunishWorld."),
            ("/lendirqu", "Search and download from LendirQu."),
            ("/becekku", "Search and download from Becekku."),
            ("/simontok", "Search and download from Simontok."),
            ("/drakorid", "Search and browse Korean dramas on Drakor.id."),
        ),
    ),
    ("Manga", (("/manga nh", "Read NH / nhentai manga."),)),
    ("Settings", (("/settings", "YouTube resolution up to 1080p."),)),
    ("Other", (("/mode", "Change Caca persona."),)),
)

NOTE_NSFW = (
    "Premium websites can only be used in private chat or groups "
    "with NSFW enabled."
)


def _display_domains(domains) -> list[str]:
    """Displayed domains; wildcards still apply in the router but are hidden from the list."""
    return [domain for domain in sorted(set(domains)) if "*" not in domain]


def _domain_rows(domains: list[str]) -> str:
    """Render a two-column domain table to keep long lists compact."""
    rows = []
    for index in range(0, len(domains), 2):
        left = escape(domains[index])
        right = escape(domains[index + 1]) if index + 1 < len(domains) else ""
        rows.append(f"<tr><td><code>{left}</code></td><td><code>{right}</code></td></tr>")
    return "".join(rows)


def _build_rich_message() -> dict:
    """Build an InputRichMessage using Rich HTML from Bot API 10.3."""
    plain = _display_domains(PREMIUM_ONLY_DOMAINS | PREMIUM_NO_NSFW_DOMAINS)
    total = len(plain)

    sections = [
        "<h1>Premium Benefits</h1>",
        "<p>Benefits available for <b>Donors 😋</b>.</p>",
        "<hr/>",
    ]

    for heading, items in BENEFITS:
        sections.append(f"<h3>{escape(heading)}</h3>")
        sections.append("<ul>")
        for command, description in items:
            sections.append(
                f"<li><code>{escape(command)}</code> &mdash; {escape(description)}</li>"
            )
        sections.append("</ul>")

    sections.extend(
        (
            "<hr/>",
            "<h3>Notes</h3>",
            f"<aside>{escape(NOTE_NSFW)}</aside>",
            "<details>",
            f"<summary><b>Premium Websites ({total} domains)</b></summary>",
            "<p>The following domains require Qris when used through the downloader.</p>",
            "<table bordered striped compact>",
            "<tr><th>Domain</th><th>Domain</th></tr>",
            _domain_rows(plain),
            "</table>",
            "</details>",
            "<footer>Get Premium access by donating.</footer>",
        )
    )
    return {"html": "".join(sections)}


def _build_plain_text() -> str:
    """Plain HTML version as fallback for older Bot API servers."""
    plain = _display_domains(PREMIUM_ONLY_DOMAINS | PREMIUM_NO_NSFW_DOMAINS)
    total = len(plain)

    parts = ["<b>Premium Benefits</b>", "", "Benefits available for <b>Donors 😋</b>."]
    for heading, items in BENEFITS:
        parts.append("")
        parts.append(f"<b>{escape(heading)}</b>")
        for command, description in items:
            parts.append(f"- <code>{escape(command)}</code> — {escape(description)}")
    parts.extend(
        (
            "",
            escape(NOTE_NSFW),
            "",
            f"<b>Premium Websites ({total} domains)</b>",
            ", ".join(f"<code>{escape(domain)}</code>" for domain in plain),
            "",
            "Get Premium access by donating.",
        )
    )
    return "\n".join(parts)


async def premiumbenefit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the Premium benefits list via the Rich Message endpoint."""
    message = update.effective_message
    if message is None:
        return

    payload = {
        "chat_id": message.chat_id,
        "rich_message": _build_rich_message(),
        "reply_parameters": {"message_id": message.message_id},
    }
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id:
        payload["message_thread_id"] = thread_id

    try:
        return await context.bot.do_api_request("sendRichMessage", payload)
    except Exception as e:
        log.debug("Rich Message not supported, falling back to HTML | err=%r", e)

    try:
        return await message.reply_text(_build_plain_text(), parse_mode="HTML")
    except Exception as e:
        log.debug("Failed to send premiumbenefit fallback | err=%r", e)
        return None
