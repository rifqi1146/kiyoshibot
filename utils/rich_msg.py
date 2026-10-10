"""Helper Rich Message (Bot API 10.3) generik untuk pesan non-search.

Dipakai handler yang output-nya terstruktur (key-value / tabel): `/ip`,
`/domain`, `/net`, `/whoisdomain`, `/weather`, `/kurs`, dll.

Desain: pemanggil menyusun daftar **section** `(heading, rows)` di mana `rows`
adalah list `(label, value)`. Helper menghasilkan DUA versi sekaligus:

- `rich`: `<h1>` + `<h3>` + `<table compact>` + `<aside>`.
- `plain`: HTML biasa (`<b>` + `• label: value`) untuk fallback server lama.

API utama:
- `Report` + `report.add(...)` -> `(rich_html, plain_html)`.
- `send_rich(...)` / `edit_rich(...)` dengan fallback otomatis.
"""
import html
import logging
import warnings
from telegram import InlineKeyboardMarkup

log = logging.getLogger(__name__)


class Report:
    """Akumulator section untuk pesan terstruktur rich + plain."""

    def __init__(self, title: str, subtitle: str = "", *, aside: str = "", footer: str = ""):
        self.title = title
        self.subtitle = subtitle
        self.aside = aside
        self.footer = footer
        # (heading, rows) ; rows = list[(label, value)] ; value None -> dilewati
        self.sections: list[tuple[str, list]] = []
        self._plain_extra: list[str] = []

    def add(self, heading: str, rows) -> "Report":
        rows = [(lbl, val) for lbl, val in rows if val is not None and str(val).strip()]
        self.sections.append((heading, rows))
        return self

    def add_raw(self, heading: str, rows) -> "Report":
        """Seperti `add`, tapi `value` TIDAK di-escape (pemanggil sudah bikin markup)."""
        rows = [(lbl, val) for lbl, val in rows if val is not None and str(val).strip()]
        self.sections.append((heading, rows))
        return self

    def add_text(self, plain_block: str) -> "Report":
        """Tambah blok plain tambahan (mis. daftar name server) ke fallback."""
        if plain_block and plain_block.strip():
            self._plain_extra.append(plain_block)
        return self

    def _rich(self) -> str:
        parts = [f"<h1>{html.escape(self.title)}</h1>"]
        if self.subtitle:
            parts.append(f"<p>{html.escape(self.subtitle)}</p>")
        parts.append("<hr/>")
        for heading, rows in self.sections:
            if heading:
                parts.append(f"<h3>{html.escape(heading)}</h3>")
            if rows:
                body = "".join(
                    f"<tr><td><b>{html.escape(str(lbl))}</b></td><td>{html.escape(str(val))}</td></tr>"
                    for lbl, val in rows
                )
                parts.append(f"<table compact>{body}</table>")
            parts.append("<hr/>")
        if self.aside:
            parts.append(f"<aside>💡 {html.escape(self.aside)}</aside>")
        if self.footer:
            parts.append(f"<footer>{html.escape(self.footer)}</footer>")
        return "".join(parts)

    def _plain(self) -> str:
        parts = [f"<b>{html.escape(self.title)}</b>"]
        if self.subtitle:
            parts.append(html.escape(self.subtitle))
        for heading, rows in self.sections:
            parts.append("")
            if heading:
                parts.append(f"<b>{html.escape(heading)}</b>")
            for lbl, val in rows:
                parts.append(f"• <b>{html.escape(str(lbl))}:</b> {html.escape(str(val))}")
        for extra in self._plain_extra:
            parts.append("")
            parts.append(extra)
        return "\n".join(parts)

    def build(self) -> tuple[str, str]:
        return self._rich(), self._plain()


def _markup_dict(markup):
    if markup is None:
        return None
    if hasattr(markup, "to_dict"):
        return markup.to_dict()
    return markup if isinstance(markup, dict) else None


async def send_rich(bot, chat_id: int, rich_html: str, plain_html: str, markup: InlineKeyboardMarkup | None = None, *, reply_to_message_id: int | None = None, message_thread_id: int | None = None):
    payload: dict = {"chat_id": chat_id, "rich_message": {"html": rich_html}}
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    md = _markup_dict(markup)
    if md:
        payload["reply_markup"] = md

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("sendRichMessage", payload)
    except Exception as e:
        log.debug("sendRichMessage fallback | err=%r", e)

    return await bot.send_message(
        chat_id=chat_id,
        text=plain_html,
        reply_markup=markup,
        parse_mode="HTML",
        reply_to_message_id=reply_to_message_id,
        disable_web_page_preview=True,
    )


async def edit_rich(bot, chat_id: int, message_id: int, rich_html: str, plain_html: str, markup: InlineKeyboardMarkup | None = None):
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": {"html": rich_html},
    }
    md = _markup_dict(markup)
    if md:
        payload["reply_markup"] = md

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return await bot.do_api_request("editMessageText", payload)
    except Exception as e:
        log.debug("editMessageText rich fallback | err=%r", e)

    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=plain_html,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.debug("edit_message_text fallback failed | err=%r", e)
        return None
