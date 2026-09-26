from urllib.parse import urlparse
from .community import is_youtube_post_url, download_youtube_post, is_youtube_shorts_url

def is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
        return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
    except Exception:
        text = (url or "").lower()
        return "youtu.be" in text or "youtube.com" in text