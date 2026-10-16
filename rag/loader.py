import os
import logging

from .chunker import chunk_markdown

log = logging.getLogger(__name__)

DOC_DIR = os.getenv("RAG_DOC_DIR", "data/rag_docs")


def load_chunked_docs(doc_dir: str | None = None) -> list[dict]:
    target_dir = doc_dir or DOC_DIR
    chunks: list[dict] = []

    if not os.path.isdir(target_dir):
        log.warning("RAG doc directory not found | dir=%s", target_dir)
        return chunks

    for fname in sorted(os.listdir(target_dir)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(target_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            log.warning("Failed to read RAG doc | file=%s err=%r", path, e)
            continue

        doc_id = os.path.splitext(fname)[0]
        base_title = _first_heading(text) or doc_id.replace("_", " ").title()
        chunks.extend(chunk_markdown(doc_id, base_title, text))

    log.info("RAG docs loaded | files=%s chunks=%s dir=%s", len(set(c["id"].split("_")[0] for c in chunks)), len(chunks), target_dir)
    return chunks


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def load_local_contexts() -> list[str]:
    return [c["content"] for c in load_chunked_docs()]
