import re


def chunk_markdown(doc_id: str, title: str, text: str, max_chunk_chars: int = 600) -> list[dict]:
    """Split markdown document into coherent topic chunks by headers or paragraphs.

    Returns a list of dicts: {"id": str, "title": str, "content": str}.
    """
    lines = text.splitlines()
    sections = []
    current_title = title
    current_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("# ", "## ", "### ")):
            if current_lines:
                chunk_text = "\n".join(current_lines).strip()
                if chunk_text:
                    sections.append((current_title, chunk_text))
                current_lines = []
            header_name = re.sub(r"^#+\s*", "", stripped).strip()
            current_title = f"{title} - {header_name}" if title != header_name else header_name
        else:
            current_lines.append(line)

    if current_lines:
        chunk_text = "\n".join(current_lines).strip()
        if chunk_text:
            sections.append((current_title, chunk_text))

    chunks = []
    for idx, (sec_title, content) in enumerate(sections):
        if len(content) <= max_chunk_chars:
            chunks.append({
                "id": f"{doc_id}_{idx}",
                "title": sec_title,
                "content": content,
            })
        else:
            paragraphs = content.split("\n\n")
            buf = []
            sub_idx = 0
            for p in paragraphs:
                p_str = p.strip()
                if not p_str:
                    continue
                if sum(len(x) for x in buf) + len(p_str) > max_chunk_chars and buf:
                    chunks.append({
                        "id": f"{doc_id}_{idx}_{sub_idx}",
                        "title": sec_title,
                        "content": "\n\n".join(buf).strip(),
                    })
                    sub_idx += 1
                    buf = []
                buf.append(p_str)
            if buf:
                chunks.append({
                    "id": f"{doc_id}_{idx}_{sub_idx}",
                    "title": sec_title,
                    "content": "\n\n".join(buf).strip(),
                })

    return chunks


def chunk_text(text: str, max_size: int = 500) -> list[str]:
    """Backward compatibility helper."""
    chunks = []
    buf = ""
    for line in text.splitlines():
        if len(buf) + len(line) > max_size:
            if buf.strip():
                chunks.append(buf.strip())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf.strip())
    return chunks
