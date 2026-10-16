from . import parsers as P

USER_NOT_FOUND = (10202, 10221, 10223)
VIDEO_NOT_FOUND = (10204, 10215, 10216)
MUSIC_NOT_FOUND = (10203, 10218)
HASHTAG_NOT_FOUND = (10205,)
EFFECT_NOT_FOUND = (10208,)
PLACE_NOT_FOUND = (205001,)
BAD_ID = (100001,)

INVALID_CURSOR = "invalid cursor: pass the next_cursor value from the previous page unchanged"


def has_more(payload):
    return bool(payload.get("hasMore") or payload.get("has_more") or payload.get("hasMorePrevious"))


def next_cursor(payload, key="cursor"):
    if not has_more(payload):
        return None
    value = payload.get(key)
    return str(value) if value not in (None, "") else None


def video_page(payload, key="itemList", **head):
    videos = P.videos(payload.get(key))
    cursor = next_cursor(payload)
    return {**head, "video_count": len(videos), "videos": videos, "next_cursor": cursor, "has_more": cursor is not None}
