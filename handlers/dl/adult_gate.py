"""Gate konten dewasa untuk platform media sosial (X/Twitter, Reddit, TikTok, IG).

Sumber media sosial berbagi domain dengan konten normal, jadi tidak bisa
memakai `PREMIUM_ONLY_DOMAINS` (yang memblokir seluruh domain). Gate di sini
dijalankan **setelah metadata di-scrape**, memakai flag sensitivitas yang
dilaporkan platform itu sendiri:

- X/Twitter : `legacy.possibly_sensitive` (author set "may contain sensitive
              material") + `ext_sensitive_media_warning` per media entity.
- Reddit    : `over_18` dari JSON post.
- TikTok    : tidak ada flag resmi -> dianggap normal.

Aturan (sama dengan gate premium domain NSFW):
1. User premium -> boleh (di grup tetap wajib NSFW aktif / private chat).
2. User non-premium -> TIDAK boleh, pesan khusus konten dewasa.
3. Private chat -> lolos syarat NSFW.
"""
import logging

log = logging.getLogger(__name__)

# Alasan blok -> dipakai router untuk memilih teks pesan.
ADULT_BLOCK_NOT_PREMIUM = "adult_not_premium"
ADULT_BLOCK_NOT_NSFW = "adult_not_nsfw"


def is_adult_media(*flags: object) -> bool:
    """True kalau SALAH SATU flag sensitivitas platform bernilai benar.

    Menerima bool, dict (`ext_sensitive_media_warning`), atau None. Dict
    dianggap sensitif bila ada nilai truthy di dalamnya (mis.
    `{"adult_content": True, "graphic_violence": False, "other": False}`).
    """
    for flag in flags:
        if isinstance(flag, dict):
            if any(bool(v) for v in flag.values()):
                return True
        elif flag is True:
            return True
        elif isinstance(flag, str) and flag.strip().lower() in ("true", "1", "yes"):
            return True
    return False


def check_adult_access(user_id: int, chat_id: int, chat_type: str) -> tuple[bool, str]:
    """Gerbang konten dewasa. Return `(allowed, reason)`.

    `reason` kosong kalau diizinkan; kalau tidak, nilainya salah satu konstanta
    `ADULT_BLOCK_*` di atas.
    """
    # Import lokal supaya modul ini tidak menarik paket UI/router (konvensi
    # handlers/dl/* tetap bisa di-import tanpa layer atas).
    from database.download_db import is_premium_user
    from database.nsfw_db import is_nsfw_allowed

    if not is_premium_user(user_id):
        return False, ADULT_BLOCK_NOT_PREMIUM

    if chat_type == "private":
        return True, ""

    try:
        if is_nsfw_allowed(chat_id, chat_type):
            return True, ""
    except Exception as e:
        log.warning("Adult NSFW check failed | chat_id=%r chat_type=%r err=%r", chat_id, chat_type, e)

    return False, ADULT_BLOCK_NOT_NSFW
