import logging

log = logging.getLogger(__name__)

ADULT_BLOCK_NOT_PREMIUM = "adult_not_premium"
ADULT_BLOCK_NOT_NSFW = "adult_not_nsfw"


def is_adult_media(*flags: object) -> bool:
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
