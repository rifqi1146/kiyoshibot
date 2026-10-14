import asyncio
import logging
import re
import sqlite3
from typing import Sequence

log = logging.getLogger(__name__)

# Common Indonesian stopwords and particles that should not trigger RAG retrieval.
INDONESIAN_STOPWORDS = {
    "ada", "adalah", "adanya", "agar", "aja", "akan", "aku", "anda", "antar", "antara",
    "apa", "apabila", "apakah", "apalagi", "arti", "artinya", "atas", "atau",
    "bagaimana", "bagi", "bahkan", "bahwa", "baik", "banyak", "batu", "bawah",
    "beberapa", "begitu", "belum", "benar", "berapa", "berikut", "bersama",
    "besar", "bisa", "buat", "bukan", "cara", "coba", "cukup", "cuma", "dah", "dan", "dapat",
    "dari", "daripada", "dekat", "demi", "demikian", "dengan", "depan", "di",
    "dia", "dong", "dulu", "gak", "gimana", "gua", "gue", "hanya", "hari",
    "harus", "ia", "ialah", "ibu", "ingin", "ini", "itu", "jadi", "jangan",
    "jauh", "jika", "juga", "kalau", "kami", "kamu", "kapan", "karena", "kata",
    "ke", "kecil", "kembali", "kemudian", "kenapa", "kepada", "kini", "kita",
    "kok", "kpd", "kurang", "lagi", "lah", "lain", "lalu", "lama", "lu", "maka",
    "malah", "mana", "masih", "masuk", "mau", "maupun", "melalui", "melihat",
    "memang", "mempunyai", "menjadi", "mereka", "merupakan", "meski", "min",
    "namun", "nanti", "nih", "nya", "oleh", "pada", "padahal", "para", "pasti",
    "pernah", "pun", "saat", "saja", "sama", "sampai", "sana", "sangat",
    "saya", "sebab", "sebagai", "sebuah", "secara", "sedang", "sedangkan",
    "sedikit", "sehingga", "sejak", "sekali", "sekarang", "selain", "selalu",
    "selama", "seluruh", "semakin", "sementara", "semua", "sendiri", "seperti",
    "sering", "serta", "sesuatu", "sesudah", "setelah", "setiap", "siapa",
    "sih", "sini", "sudah", "supaya", "tahu", "tak", "tambah", "tampak",
    "tanpa", "tapi", "telah", "tentang", "tentu", "tempat", "terhadap",
    "terlalu", "tersebut", "terus", "tetapi", "tiap", "tidak", "tolong",
    "tuh", "untuk", "utk", "wah", "waktu", "walau", "walaupun", "ya", "yah",
    "yang", "yg", "yuk",
}

# Specific keywords that signal a query is asking about the bot itself or its features.
# Generic words ("cara", "gimana", "help") are intentionally excluded: they appear in
# everyday questions ("cara masak nasi goreng") and would inject docs needlessly.
BOT_INTENT_KEYWORDS = {
    "bot", "kiyoshi", "kiyoshibot", "fitur", "command", "perintah", "bantuan",
    "downloader", "download", "unduh", "premium", "buypremium", "qris", "bayar",
    "harga", "vip", "benefit", "premiumbenefit", "iklan", "ads", "sponsor",
    "grup", "admin", "moderasi", "ban", "kick", "mute", "unmute", "wlc", "welcome",
    "nsfw", "spoiler", "autodl", "autodel", "asupann", "cacaa",
    "caca", "mode", "persona", "ask", "groq", "gemini", "ai",
    "quotly", "quote", "sticker", "stiker", "kang", "getsticker",
    "music", "lagu", "mp3", "anime", "drakor", "drakorid", "moviebox", "film",
    "anichin", "donghua", "manga", "manhwa", "mangadex", "maid", "nhentai",
    "nekopoi", "simontok", "punish", "becekku",
    "lendirqu", "cosxplay", "bunkr", "pawchive", "darknessporn", "femdomvc",
    "cosplaytele", "heavyr", "bdsmlust", "asiangirl",
    "instagram", "igstory", "igstalk", "tiktok", "douyin",
    "youtube", "twitter", "facebook", "reddit", "pinterest", "threads", "asupan",
    "ip", "domain", "whois", "speedtest", "ping", "stats",
    "kurs", "cuaca", "weather", "resi", "lacak", "translate", "trlist", "gsearch",
    "upscale", "nobg", "aidetect", "aitext", "waifu", "ship", "reminder", "share",
    "donate", "settings", "uploadengine", "fasttelethon", "asahotak", "susunkata",
    "quiz", "quoteanime", "sidang", "owner", "cookies", "backup", "broadcast",
    "sudo", "restore", "autobackup", "groups", "helpowner", "game", "permainan",
}

# Multi-word intent phrases: matched as substrings of the raw lowercased query.
# Handles identity questions ("kamu siapa", "kamu itu apa") and feature questions
# whose keywords are ordinary Indonesian words ("game apa aja", "cara main asah
# otak") that cannot be bare keywords without false positives ("cara" alone must
# NOT trigger RAG — "cara masak nasi goreng" is not a bot question).
BOT_INTENT_PHRASES = (
    "siapa kamu", "siapa lu", "siapa anda", "kamu siapa", "lu siapa", "anda siapa",
    "kamu itu siapa", "lu itu siapa", "kamu itu apa", "lu itu apa", "kamu apaan",
    "itu bot apa", "bot apa ini", "ini bot apa", "bot ini apa", "apa ini bot",
    "asah otak", "susun kata", "cara main", "cara pakai", "cara make", "cara guna",
    "apa aja fitur", "fitur apa aja", "apa saja fitur", "fitur bot", "fungsi bot",
    "developer bot", "owner bot", "pembuat bot", "yang bikin bot", "yang buat bot",
    "bebas iklan", "ada iklan", "tanpa iklan", "no ads", "game apa", "permainan apa",
)

_FTS_DB: sqlite3.Connection | None = None
_FTS_LOCK = asyncio.Lock()
_BM25_THRESHOLD = -0.40  # In FTS5 BM25, lower is better. Results worse than this are discarded.
_CACHED_ROSTER = ""


def _get_fts_db() -> sqlite3.Connection:
    global _FTS_DB
    if _FTS_DB is None:
        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
                id UNINDEXED,
                title,
                content,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        _FTS_DB = con
    return _FTS_DB


def index_documents(documents: Sequence[dict | str]) -> int:
    """Populate or refresh the in-memory FTS5 search index with chunks."""
    global _CACHED_ROSTER
    db = _get_fts_db()
    db.execute("DELETE FROM rag_fts;")

    count = 0
    for idx, doc in enumerate(documents):
        if isinstance(doc, dict):
            doc_id = str(doc.get("id") or f"doc_{idx}")
            title = str(doc.get("title") or "")
            content = str(doc.get("content") or "").strip()
        else:
            doc_id = f"doc_{idx}"
            title = ""
            content = str(doc or "").strip()

        if not content:
            continue

        db.execute(
            "INSERT INTO rag_fts(id, title, content) VALUES (?, ?, ?);",
            (doc_id, title, content),
        )
        count += 1

    db.commit()
    try:
        from .catalog import build_command_roster
        _CACHED_ROSTER = build_command_roster([d if isinstance(d, dict) else {"content": str(d)} for d in documents])
    except Exception as e:
        log.warning("RAG command roster build failed | err=%r", e)
        _CACHED_ROSTER = ""
    log.info("RAG FTS5 index rebuilt | count=%d", count)
    return count


def _clean_query_terms(query: str) -> list[str]:
    """Extract informative non-stopword tokens from the query."""
    raw_tokens = re.findall(r"[\w/]+", (query or "").lower())
    terms = []
    for t in raw_tokens:
        clean = t.lstrip("/").strip()
        if not clean or len(clean) < 2:
            continue
        if clean in INDONESIAN_STOPWORDS:
            continue
        terms.append(clean)
    return terms


def _has_bot_intent(query_lower: str, terms: list[str]) -> bool:
    """True if the query explicitly mentions bot features, commands, or identity."""
    if re.search(r"\b/[a-z0-9_]+\b", query_lower):
        return True
    for t in terms:
        if t in BOT_INTENT_KEYWORDS:
            return True
    for phrase in BOT_INTENT_PHRASES:
        if phrase in query_lower:
            return True
    return False


def _identity_chunk(db: sqlite3.Connection, top_k: int) -> list[str]:
    """Fetch the bot identity/about chunk for stopword-only identity questions."""
    try:
        cur = db.execute(
            """
            SELECT title, content FROM rag_fts
            WHERE id LIKE 'about_%'
            LIMIT ?;
            """,
            (max(1, top_k),),
        )
        rows = cur.fetchall()
    except Exception as e:
        log.warning("RAG identity fallback failed | err=%r", e)
        return []

    out: list[str] = []
    for title, content in rows:
        out.append(f"### {title}\n{content}" if title else content)
    return out


def _search_fts(query: str, top_k: int = 2) -> list[str]:
    """Return relevant context: a compact command roster + BM25-matched detail chunks.

    The roster (~170 tokens) is cheap and always included on bot-intent queries so
    the model knows every real command name. Detail chunks are added only when they
    score strongly enough (strict relevance gate) so we never inject off-topic text.
    """
    terms = _clean_query_terms(query)
    query_lower = (query or "").lower()
    has_intent = _has_bot_intent(query_lower, terms)
    # Stopword-only queries ("kamu siapa?", "kamu itu apa?") lose every term, but
    # they are still identity questions about the bot — keep the roster + best match.
    if not terms and not has_intent:
        log.info("[RAG] skip | q=%r reason=no_terms_no_intent (0 token)", query)
        return []

    # If the user is chatting generally (math, cooking, coding) without any bot-related
    # terms, bypass RAG completely. This saves tokens on 80%+ of chat interactions.
    if not has_intent:
        log.info("[RAG] skip | q=%r reason=general_query (0 token)", query)
        return []

    results: list[str] = []
    if _CACHED_ROSTER:
        results.append(_CACHED_ROSTER)

    db = _get_fts_db()
    safe_terms = [re.sub(r"[^\w]", "", t) for t in terms]
    safe_terms = [t for t in safe_terms if len(t) >= 2]
    if not safe_terms:
        # No searchable terms (e.g. "kamu siapa?"): fall back to the identity/about
        # chunk so the model can answer who it is instead of hallucinating.
        fallback = _identity_chunk(db, top_k)
        results.extend(fallback)
        log.info(
            "[RAG] hit | q=%r via=identity_fallback chunks=%d reason=no_searchable_terms",
            query, len(fallback),
        )
        return results

    fts_expression = " OR ".join(f'"{t}"*' for t in safe_terms[:8])

    try:
        cur = db.execute(
            """
            SELECT title, content, bm25(rag_fts) as score
            FROM rag_fts
            WHERE rag_fts MATCH ?
            ORDER BY score ASC
            LIMIT ?;
            """,
            (fts_expression, max(top_k * 3, 6)),
        )
        rows = cur.fetchall()
    except Exception as e:
        log.warning("RAG FTS5 query failed | expr=%r err=%r", fts_expression, e)
        return results

    # Relevance gate: keep only chunks that clear the absolute threshold AND
    # (for the 2nd+ chunk) sit close to the best score, so weak matches are dropped.
    best = rows[0][2] if rows else 0.0
    kept = 0
    picked: list[str] = []
    dropped_weak = 0
    for title, content, score in rows:
        if kept >= top_k:
            break
        if score > _BM25_THRESHOLD:
            dropped_weak += 1
            continue
        if kept > 0 and score > best * 0.6:
            dropped_weak += 1
            continue
        formatted = f"### {title}\n{content}" if title else content
        results.append(formatted)
        picked.append(title or "?")
        kept += 1

    if picked:
        log.info(
            "[RAG] hit | q=%r terms=%s candidates=%d kept=%d chunks=%s best=%.3f dropped_weak=%d",
            query, safe_terms[:8], len(rows), kept, picked, best, dropped_weak,
        )
    else:
        log.info(
            "[RAG] miss | q=%r terms=%s candidates=%d reason=all_below_threshold best=%.3f (roster only)",
            query, safe_terms[:8], len(rows), best,
        )

    return results


async def retrieve_context(
    query: str,
    documents: Sequence[dict | str] | None = None,
    top_k: int = 2,
) -> list[str]:
    """Retrieve relevant context chunks for a query using SQLite FTS5 (BM25).

    Returns an empty list when the query is unrelated to the bot (saving tokens),
    or up to ``top_k`` most relevant topic chunks when related.
    """
    if not query or not query.strip():
        return []

    async with _FTS_LOCK:
        # Auto-seed the index if not yet populated
        db = _get_fts_db()
        cur = db.execute("SELECT count(*) FROM rag_fts;")
        if cur.fetchone()[0] == 0:
            if documents:
                await asyncio.to_thread(index_documents, documents)
            else:
                from .loader import load_chunked_docs
                chunks = await asyncio.to_thread(load_chunked_docs)
                await asyncio.to_thread(index_documents, chunks)

        return await asyncio.to_thread(_search_fts, query, top_k)
