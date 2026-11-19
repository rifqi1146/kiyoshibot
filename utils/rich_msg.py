import html
import logging
import os
import warnings
from telegram import InlineKeyboardMarkup

from .rich_common import is_message_not_modified, post_rich_multipart

log = logging.getLogger(__name__)


class Report:

    def __init__(self, title: str, subtitle: str = "", *, aside: str = "", footer: str = ""):
        self.title = title
        self.subtitle = subtitle
        self.aside = aside
        self.footer = footer
        self.sections: list[tuple[str, list]] = []
        self._plain_extra: list[str] = []

    def add(self, heading: str, rows) -> "Report":
        rows = [(lbl, val) for lbl, val in rows if val is not None and str(val).strip()]
        self.sections.append((heading, rows))
        return self

    def add_raw(self, heading: str, rows) -> "Report":
        rows = [(lbl, val) for lbl, val in rows if val is not None and str(val).strip()]
        self.sections.append((heading, rows))
        return self

    def add_text(self, plain_block: str) -> "Report":
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
        if is_message_not_modified(e):
            log.debug("editMessageText rich not modified, no-op")
            return None
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

async def send_rich_with_media(
    bot,
    chat_id: int,
    items: list[dict],
    rich_html: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    has_spoiler: bool = False,
):
    attach: list[tuple[str, str]] = []
    media_entries: list[dict] = []
    tags: list[str] = []

    for idx, item in enumerate(items):
        path = item.get("path")
        if not path or not os.path.isfile(path):
            continue
        raw_kind = str(item.get("type") or "").strip().lower()
        kind = "video" if raw_kind in ("video", "animation") else "photo"
        mid = f"m{idx}"
        spoiler = bool(has_spoiler or item.get("has_spoiler"))
        media_obj: dict = {"type": kind, "media": f"attach://{mid}"}
        attach.append((mid, path))

        if kind == "video":
            media_obj["supports_streaming"] = True
            meta = item.get("meta") or {}
            for key in ("width", "height", "duration"):
                try:
                    val = int(meta.get(key) or 0)
                except (TypeError, ValueError):
                    val = 0
                if val > 0:
                    media_obj[key] = val
            thumb = item.get("thumb")
            if thumb and os.path.isfile(thumb):
                tid = f"t{idx}"
                attach.append((tid, thumb))
                media_obj["thumbnail"] = f"attach://{tid}"
            tags.append(f'<video src="tg://video?id={mid}"' + (" tg-spoiler" if spoiler else "") + "></video>")
        else:
            tags.append(f'<img src="tg://photo?id={mid}"' + (" tg-spoiler" if spoiler else "") + "/>")

        if spoiler:
            media_obj["has_spoiler"] = True
        media_entries.append({"id": mid, "media": media_obj})

    if not tags:
        raise RuntimeError("No valid media files for sendRichMessage")

    media_html = tags[0] if len(tags) == 1 else f"<tg-slideshow>{''.join(tags)}</tg-slideshow>"
    rich_message: dict = {"html": media_html + rich_html, "media": media_entries}
    payload: dict = {"chat_id": chat_id, "rich_message": rich_message}
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    md = _markup_dict(markup)
    if md:
        payload["reply_markup"] = md

    try:
        return await post_rich_multipart(bot, payload, attach)
    except Exception as e:
        if payload.get("reply_parameters") and "replied" in str(e).lower() and "not found" in str(e).lower():
            payload.pop("reply_parameters", None)
            return await post_rich_multipart(bot, payload, attach)
        raise
