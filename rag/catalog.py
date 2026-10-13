"""Compact command catalog for RAG.

Two representations:
    1. names_only: a 1-line comma-separated list of all ~74 bot commands
       (~170 tokens total). Always injected on bot-intent queries so the model
       knows the real command names (preventing hallucinated commands like
       /setwelcome).
    2. grouped_catalog: a category-grouped index with short descriptions
       (~750 tokens), used for broad questions like "apa saja fitur bot".
"""

import re

_CMD_TOKEN_RE = re.compile(r"`(/[a-z0-9_]+)(?:\s[^`]*)?`", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+")

_GROUP_LABELS = {
    "about": "Bot",
    "downloader": "Downloader",
    "explorer": "Explorer 18+",
    "ai_chat": "AI",
    "premium": "Premium",
    "utility": "Utility",
    "group_admin": "Grup",
}

_MAX_DESC = 40


def _doc_base(doc_id: str) -> str:
    parts = str(doc_id or "").split("_")
    while parts and parts[-1].isdigit():
        parts.pop()
    return "_".join(parts)


def _clean_desc(desc: str) -> str:
    desc = re.sub(r"\s+", " ", desc or "").strip(" :—–-·•\t")
    if len(desc) > _MAX_DESC:
        desc = desc[: _MAX_DESC - 1].rstrip() + "…"
    return desc


def _strip_backticks(text: str) -> str:
    return re.sub(r"`([^`]*)`", r"\1", text)


def _line_commands(line: str) -> list[str]:
    return [m.group(1) for m in _CMD_TOKEN_RE.finditer(line)]


def _line_description(line: str, cmds: list[str]) -> str:
    cleaned = _BULLET_RE.sub("", line).strip()
    if len(cmds) == 1 and cleaned.startswith(f"`{cmds[0]}"):
        tail = cleaned[len(cmds[0]) + 1:].lstrip("` :—–-·•\t")
        tail = _strip_backticks(tail)
        return _clean_desc(tail)
    return _clean_desc(_strip_backticks(cleaned))


def extract_all_commands(docs: list[dict]) -> list[str]:
    """Collect all unique command names across all documents."""
    seen: set[str] = set()
    names: list[str] = []
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        content = str(doc.get("content") or "")
        for m in _CMD_TOKEN_RE.finditer(content):
            c = m.group(1)
            key = c.lower()
            if key not in seen:
                seen.add(key)
                names.append(c)
    return names


def build_command_roster(docs: list[dict]) -> str:
    """A compact ~170-token comma-separated list of all commands."""
    names = extract_all_commands(docs)
    if not names:
        return ""
    return "Daftar command KiyoshiBot: " + ", ".join(names)


def build_command_catalog(docs: list[dict]) -> str:
    """Build a category-grouped catalog with brief descriptions."""
    seen: set[str] = set()
    groups: dict[str, list[str]] = {}
    order: list[str] = []

    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        base = _doc_base(doc.get("id") or "")
        label = _GROUP_LABELS.get(base, base.replace("_", " ").title() or "Bot")
        content = str(doc.get("content") or "")

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not _BULLET_RE.match(line):
                continue
            cmds = _line_commands(line)
            if not cmds:
                continue

            new_cmds = [c for c in cmds if c.lower() not in seen]
            if not new_cmds:
                continue
            for c in new_cmds:
                seen.add(c.lower())

            desc = _line_description(line, cmds)
            if len(cmds) > 1:
                entry = desc
            else:
                entry = f"{cmds[0]} {desc}".rstrip() if desc else cmds[0]

            if label not in groups:
                groups[label] = []
                order.append(label)
            groups[label].append(entry)

    if not order:
        return ""

    lines = ["Ringkasan fitur KiyoshiBot:"]
    for label in order:
        lines.append(f"[{label}] " + " | ".join(groups[label]))
    return "\n".join(lines)
