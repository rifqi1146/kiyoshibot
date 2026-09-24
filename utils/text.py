import re
import html
from typing import List

def bold(text: str) -> str:
    return f"<b>{html.escape(text)}</b>"

def italic(text: str) -> str:
    return f"<i>{html.escape(text)}</i>"

def underline(text: str) -> str:
    return f"<u>{html.escape(text)}</u>"

def code(text: str) -> str:
    return f"<code>{html.escape(text)}</code>"

def pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"

def mono(text: str) -> str:
    return f"<tt>{html.escape(text)}</tt>"

def link(label: str, url: str) -> str:
    return f'<a href="{html.escape(url)}">{html.escape(label)}</a>'

#split
def split_message(text: str, max_length: int = 4000) -> List[str]:
    """
    Splits a long text into chunks not exceeding max_length.
    Tries to split by paragraphs/words first, falls back to char split.
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = ""
    paragraphs = text.split("\n")

    for paragraph in paragraphs:
        if current_chunk and not current_chunk.endswith("\n"):
            current_chunk += "\n"

        if len(paragraph) + len(current_chunk) <= max_length:
            current_chunk += paragraph
        else:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = paragraph

            if len(current_chunk) > max_length:
                temp_chunks = []
                temp_chunk = ""
                words = current_chunk.split(" ")
                for word in words:
                    word_to_add = f" {word}" if temp_chunk else word
                    if len(temp_chunk) + len(word_to_add) <= max_length:
                        temp_chunk += word_to_add
                    else:
                        if temp_chunk:
                            temp_chunks.append(temp_chunk)
                        temp_chunk = word
                if temp_chunk:
                    temp_chunks.append(temp_chunk)

                chunks.extend(temp_chunks)
                current_chunk = ""

    if current_chunk:
        chunks.append(current_chunk)

    final_chunks: List[str] = []
    for chunk in chunks:
        if len(chunk) > max_length:
            for i in range(0, len(chunk), max_length):
                final_chunks.append(chunk[i : i + max_length])
        else:
            final_chunks.append(chunk)

    return final_chunks

_PH = "\ue000"  # placeholder private-use, tidak match regex markdown apa pun

# Tag HTML yang SAH dikirim Telegram — boleh lolos dari escape (model sering
# meniru tag dari history/RAG yang sudah berupa HTML)
_SAFE_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|s|strike|del|code|pre|blockquote|tg-spoiler)"
    r"(?:\s[^<>]*)?>|<a\s[^<>]*>|</a>",
    re.IGNORECASE,
)


def _balance_tags(text: str, tags: list[str]) -> str:
    """Tutup tag whitelist yang tak seimbang agar Telegram tak membalas 400."""
    for raw in tags:
        m = re.match(r"</?([a-zA-Z0-9-]+)", raw)
        if not m:
            continue
        name = m.group(1).lower()
        opens = len(re.findall(rf"<{name}(?:\s[^<>]*)?>", text, re.IGNORECASE))
        closes = len(re.findall(rf"</{name}\s*>", text, re.IGNORECASE))
        if opens > closes:
            text += f"</{name}>" * (opens - closes)
        elif closes > opens:
            for _ in range(closes - opens):
                text = re.sub(rf"</{name}>\s*$", "", text, count=1, flags=re.IGNORECASE)
    return text


def strip_tags_plain(text: str) -> str:
    """Fallback darurat: buang semua tag HTML + unescape -> teks polos aman."""
    text = re.sub(r"</?[a-zA-Z][^<>]*>", "", text)
    return html.unescape(text)


def _md_tables_to_bullets(text: str) -> str:
    """Tabel markdown jadi bullet '- **kolom1**: nilai' (Telegram tak punya <table>)."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        sep = lines[i + 1] if i + 1 < len(lines) else ""
        if (
            line.strip().startswith("|")
            and line.strip().endswith("|")
            and "-" in sep
            and re.match(r"^\s*\|?[\s:|-]+\|\s*$", sep)
        ):
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            ncol = len(headers)
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if cells:
                    if len(cells) == ncol >= 2 and cells[0]:
                        rest = " — ".join(c for c in cells[1:] if c)
                        out.append(f"- **{cells[0]}**: {rest}" if rest else f"- **{cells[0]}**")
                    else:
                        out.append(f"- {' — '.join(c for c in cells if c)}")
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def sanitize_ai_output(text: str) -> str:
    """Konversi markdown mentah model -> Telegram HTML (parse_mode='HTML').

    Dulunya membuang semua **bold**/*italic* jadi teks polos dan merusak baris
    tabel jadi artefak '• <b>label</b>'. Sekarang dirender beneran.
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # Sembunyikan code block dulu agar isinya tak tersentuh markdown parser
    blocks: list[str] = []

    def _save_fence(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        cls = f' class="language-{html.escape(lang, quote=False)}"' if lang else ""
        blocks.append(f"<pre><code{cls}>{html.escape(m.group(2), quote=False)}</code></pre>")
        return f"{_PH}{len(blocks) - 1}{_PH}"

    if text.count("```") % 2 == 1:
        text = text.rstrip() + "\n```"
    text = re.sub(r"```([a-zA-Z0-9_+#-]*)[ \t]*\n?(.*?)```", _save_fence, text, flags=re.DOTALL)

    codes: list[str] = []

    def _save_inline(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"{_PH}i{len(codes) - 1}{_PH}"

    text = re.sub(r"`([^`\n]+)`", _save_inline, text)

    text = _md_tables_to_bullets(text)

    # 1) Normalisasi entitas dulu agar "&amp;amp;" atau "&amp; air" tidak double-escape
    # Decode berkala sampai tak ada lagi entitas bertumpuk
    for _ in range(2):
        text = text.replace("&amp;amp;", "&amp;").replace("&amp;lt;", "&lt;").replace("&amp;gt;", "&gt;")
    # Entitas kutip dan whitespace model dikembalikan ke literal
    text = (
        text.replace("&quot;", '"')
        .replace("&#x27;", "'")
        .replace("&apos;", "'")
        .replace("&nbsp;", " ")
    )

    # 2) Model suka meniru tag HTML dari history (misal <b>Layar:</b> atau <i>teks</i>).
    # Lindungi tag HTML yang sah dari escape, tapi buang/escape tag asing (<script>, <style>).
    saved_tags: list[str] = []

    def _save_safe_tag(m: re.Match) -> str:
        tag = m.group(0)
        idx = len(saved_tags)
        saved_tags.append(tag)
        return f"{_PH}t{idx}{_PH}"

    text = _SAFE_TAG_RE.sub(_save_safe_tag, text)

    # 3) Escape karakter WAJIB yang tersisa (<, >, & yang bukan tag sah)
    # Catatan: jika & diikuti entity name yang valid (&amp;, &lt;, &gt;) jangan di-escape ulang
    text = re.sub(r"&(?!amp;|lt;|gt;)", "&amp;", text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")

    # 4) Kembalikan tag aman
    for idx, tag in enumerate(saved_tags):
        text = text.replace(f"{_PH}t{idx}{_PH}", tag)

    # Blockquote > ... (setelah escape jadi '&gt; ...')
    quoted: list[str] = []
    buf: list[str] = []

    def _flush_quote() -> None:
        if buf:
            quoted.append("<blockquote>" + "\n".join(buf) + "</blockquote>")
            buf.clear()

    for ln in text.split("\n"):
        if ln.startswith("&gt;"):
            buf.append(ln[4:].lstrip(" ") if ln != "&gt;" else "")
        else:
            _flush_quote()
            quoted.append(ln)
    _flush_quote()
    text = "\n".join(quoted)

    text = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text)
    text = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![a-zA-Z0-9])__(.+?)__(?![a-zA-Z0-9])", r"<b>\1</b>", text)
    text = re.sub(r"(?<![a-zA-Z0-9*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![a-zA-Z0-9*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![a-zA-Z0-9_])_(?!\s)([^_\n]+?)(?<!\s)_(?![a-zA-Z0-9_])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\[(.+?)\]\(((?:https?://|tg://)[^\s)]+)\)", r'<a href="\2">\1</a>', text)

    # Bullet markdown -> bullet Telegram (di luar blockquote agar indentasi tetap)
    text = re.sub(r"(?m)^([ \t]*)[-*•◦▪‣·⁃]\s+", r"\1• ", text)

    for i, c in enumerate(codes):
        text = text.replace(f"{_PH}i{i}{_PH}", c)
    for i, b in enumerate(blocks):
        text = text.replace(f"{_PH}{i}{_PH}", b)

    # Pastikan tag HTML seimbang agar Telegram tidak menolak (400 Bad Request)
    text = _balance_tags(text, saved_tags)

    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n[ \t]+\n", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def convert_bullets(text: str) -> str:
    """Konversi bullet unicode (• ▪ ◦ dst) ke list markdown '-' / '1.'.

    Gemini suka menulis bullet dengan karakter '•' + single newline. Di
    Rich Markdown, '•' bukan penanda list dan single newline = softbreak
    (spasi), sehingga seluruh isi melipat jadi satu baris. Dengan '-',
    parser Telegram membloknya sebagai list yang benar.
    Diabaikan di dalam code fence.
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            line = re.sub(r"^([ \t]*)[•◦▪‣·⁃]\s+", r"\1- ", line)
            line = re.sub(r"^([ \t]*)(\d+)\)(?=[ \t])", r"\1\2.", line)
        out.append(line)
    return "\n".join(out)


def sanitize_markdown(text: str) -> str:
    """Rapikan markdown mentah dari model untuk Rich Message (field markdown).

    Tidak melakukan escaping HTML — Rich Markdown Telegram sudah menangani
    entitas, tag inline, dan formatting yang belum lengkap saat streaming.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = convert_bullets(text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    # Fence code block yang belum tertutup ditutup agar tetap ter-render rapi
    if text.count("```") % 2 == 1:
        text = text.rstrip() + "\n```"
    return text.strip()
