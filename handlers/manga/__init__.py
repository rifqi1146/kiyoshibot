import logging

from .commands import manga_cmd
from .callbacks import manga_callback

log = logging.getLogger(__name__)

try:
    from database.nsfw_db import nsfw_db_init
    nsfw_db_init()
except Exception as e:
    log.warning(f"Failed to initialize manga NSFW database: {e}")

__all__ = ["manga_cmd", "manga_callback"]
