import logging
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from handlers.dl.constants import PREMIUM_ONLY_DOMAINS

log = logging.getLogger(__name__)

BENEFITS = (
    (
        "Downloader",
        (
            ("/dl", "Unduhan tanpa limit tiga proses per menit."),
            ("/dl", "Resolusi video Up to 1080p+."),
        ),
    ),
    (
        "Search & Download",
        (
            ("/nekopoi", "Cari dan download konten dari Nekopoi."),
            ("/punish", "Cari dan download konten dari PunishWorld."),
            ("/lendirqu", "Cari dan download konten dari LendirQu."),
            ("/becekku", "Cari dan download konten dari Becekku."),
        ),
    ),
    ("Manga", (("/manga nh", "Membaca manga NH / nhentai."),)),
    ("Pengaturan", (("/settings", "Resolusi YouTube Up to 1080p."),)),
    ("Lainnya", (("/mode", "Ganti persona Caca."),)),
)

NOTE_NSFW = (
    "Website Premium hanya dapat dipakai di chat pribadi atau grup "
    "yang telah mengaktifkan NSFW."
)


def _display_domains(domains) -> list[str]:
    """Domain yang ditampilkan; wildcard tetap berlaku di router, tetapi disembunyikan dari daftar."""
    return [domain for domain in sorted(set(domains)) if "*" not in domain]


def _domain_rows(domains: list[str]) -> str:
    """Render tabel domain dua kolom agar daftar panjang tetap ringkas."""
    rows = []
    for index in range(0, len(domains), 2):
        left = escape(domains[index])
        right = escape(domains[index + 1]) if index + 1 < len(domains) else ""
        rows.append(f"<tr><td><code>{left}</code></td><td><code>{right}</code></td></tr>")
    return "".join(rows)


def _build_rich_message() -> dict:
    """Bangun InputRichMessage memakai Rich HTML Bot API 10.3."""
    plain = _display_domains(PREMIUM_ONLY_DOMAINS)
    total = len(plain)

    sections = [
        "<h1>Premium Benefits</h1>",
        "<p>Akses yang tersedia untuk <b>Donatur 😋</b>.</p>",
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
            "<h3>Catatan</h3>",
            f"<aside>{escape(NOTE_NSFW)}</aside>",
            "<details>",
            f"<summary><b>Website Premium ({total} domain)</b></summary>",
            "<p>Domain berikut memerlukan Qris jika digunakan lewat downloader.</p>",
            "<table bordered striped compact>",
            "<tr><th>Domain</th><th>Domain</th></tr>",
            _domain_rows(plain),
            "</table>",
            "</details>",
            "<footer>Dapatkan akses Premium dengan donate.</footer>",
        )
    )
    return {"html": "".join(sections)}


def _build_plain_text() -> str:
    """Versi HTML biasa sebagai fallback server Bot API lama."""
    plain = _display_domains(PREMIUM_ONLY_DOMAINS)
    total = len(plain)

    parts = ["<b>Premium Benefits</b>", "", "Akses yang tersedia untuk <b>Donatur 😋</b>."]
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
            f"<b>Website Premium ({total} domain)</b>",
            ", ".join(f"<code>{escape(domain)}</code>" for domain in plain),
            "",
            "Dapatkan akses Premium dengan donate.",
        )
    )
    return "\n".join(parts)


async def premiumbenefit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kirim daftar benefit Premium lewat endpoint Rich Message."""
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
        log.debug("Rich Message tidak didukung, fallback HTML | err=%r", e)

    try:
        return await message.reply_text(_build_plain_text(), parse_mode="HTML")
    except Exception as e:
        log.debug("Gagal mengirim fallback premiumbenefit | err=%r", e)
        return None
