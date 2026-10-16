import re
import threading
import time

from . import parsers as P
from . import refs
from . import shared
from .fetch import TikTokBadRequest, TikTokNotFound, api, country_order, eu_country, get_text, resolve_redirect

COMMENTS_PAGE = 50
REPLIES_PAGE = 20
SHORT_TTL = 3600
_short_lock = threading.Lock()
_short_links = {}

_CUE_TIME_RE = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})")
_TAG_RE = re.compile(r"<[^>]+>")


def video_id(video, country=None):
    if video.get("id"):
        return video["id"]
    link = video["short_link"]
    with _short_lock:
        hit = _short_links.get(link)
        if hit and hit[0] > time.time():
            return hit[1]
    target = resolve_redirect(link, country=country)
    found = refs.video_id_from_link(target)
    if not found:
        raise TikTokNotFound("that share link does not lead to a video")
    with _short_lock:
        if len(_short_links) > 5000:
            _short_links.clear()
        _short_links[link] = (time.time() + SHORT_TTL, found)
    return found


def _item(video, country):
    vid = video_id(video, country)
    payload = api("/api/item/detail/", {"itemId": vid}, countries=country_order(country, eu_country()),
                  not_found=shared.VIDEO_NOT_FOUND, label=f"video {vid}")
    item = (payload.get("itemInfo") or {}).get("itemStruct")
    if not isinstance(item, dict) or not item.get("id"):
        raise TikTokNotFound(f"video {vid} not found")
    return item


def get_details(video, country=None):
    return P.video(_item(video, country))


def get_media(video, country=None):
    return P.media(_item(video, country))


def resolve(video, country=None):
    parsed = P.video(_item(video, country))
    return {"id": parsed["id"], "link": parsed["link"], "type": parsed["type"],
            "created_at": parsed["created_at"], "author": parsed["author"]}


def get_comments(video, cursor=None, country=None):
    vid = video_id(video, country)
    payload = api("/api/comment/list/", {"aweme_id": vid, "count": COMMENTS_PAGE, "cursor": cursor or 0},
                  country=country, label=f"comments of video {vid}")
    if payload.get("comments") is None and payload.get("total") is None and not cursor:
        _item({"id": vid}, country)
    comments = P.comments(payload.get("comments"))
    next_cursor = shared.next_cursor(payload)
    return {
        "video_id": vid,
        "total_count": P.to_int(payload.get("total")),
        "comment_count": len(comments),
        "comments": comments,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


def get_comment_replies(video, comment_id, cursor=None, country=None):
    vid = video_id(video, country)
    payload = api("/api/comment/list/reply/", {"item_id": vid, "comment_id": comment_id, "count": REPLIES_PAGE,
                                               "cursor": cursor or 0},
                  country=country, label=f"replies to comment {comment_id}")
    replies = P.comments(payload.get("comments"))
    next_cursor = shared.next_cursor(payload)
    return {
        "video_id": vid,
        "comment_id": comment_id,
        "total_count": P.to_int(payload.get("total")),
        "reply_count": len(replies),
        "replies": replies,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


def get_related(video, country=None):
    vid = video_id(video, country)
    payload = api("/api/related/item_list/", {"itemID": vid, "count": 16, "cursor": 0},
                  country=country, label=f"videos related to {vid}")
    videos = P.videos(payload.get("itemList"))
    return {"video_id": vid, "video_count": len(videos), "videos": videos}


def _seconds(hours, minutes, seconds, millis):
    return round(int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000, 3)


def parse_webvtt(text):
    segments, current = [], None
    for line in (text or "").replace("\r", "").split("\n"):
        match = _CUE_TIME_RE.search(line)
        if match:
            g = match.groups()
            current = {"start_seconds": _seconds(*g[:4]), "end_seconds": _seconds(*g[4:]), "text": ""}
            segments.append(current)
        elif not line.strip():
            current = None
        elif current is not None:
            piece = _TAG_RE.sub("", line).strip()
            if piece:
                current["text"] = f"{current['text']} {piece}".strip()
    return [segment for segment in segments if segment["text"]]


_ISO3 = {"en": "eng", "es": "spa", "pt": "por", "de": "deu", "fr": "fra", "it": "ita", "ja": "jpn", "ko": "kor",
         "id": "ind", "ru": "rus", "ar": "ara", "vi": "vie", "zh": "cmn", "tr": "tur", "th": "tha", "nl": "nld",
         "pl": "pol", "hi": "hin", "ms": "msa", "sv": "swe", "ro": "ron", "uk": "ukr", "he": "heb", "el": "ell",
         "cs": "ces", "hu": "hun", "fil": "fil", "tl": "fil", "bn": "ben", "ur": "urd", "fa": "fas"}


def _language_matches(track_language, wanted):
    track, wanted = (track_language or "").lower(), wanted.lower()
    if track == wanted:
        return True
    primary, wanted_primary = track.split("-")[0], wanted.split("-")[0]
    if primary not in (wanted_primary, _ISO3.get(wanted_primary)):
        return False
    region = wanted.split("-")[1] if "-" in wanted else None
    return region is None or track.endswith("-" + region)


def _pick_subtitle(tracks, language):
    if language:
        return next((track for track in tracks if _language_matches(track["language"], language)), None)
    originals = [track for track in tracks if not track["is_translation"]]
    return (originals or tracks)[0]


def get_transcript(video, language=None, country=None):
    item = _item(video, country)
    tracks = [t for t in P.subtitles((item.get("video") or {}).get("subtitleInfos")) if (t["format"] or "").lower() == "webvtt"]
    available = sorted({track["language"] for track in tracks if track["language"]})
    if not tracks:
        raise TikTokNotFound(f"video {item.get('id')} has no captions")
    track = _pick_subtitle(tracks, language)
    if track is None:
        raise TikTokBadRequest(f"no captions in {language!r}; available: {', '.join(available)}")
    segments = parse_webvtt(get_text(track["link"], country=country))
    return {
        "video_id": P.to_id(item.get("id")),
        "language": track["language"],
        "is_auto_generated": track["is_auto_generated"],
        "is_translation": track["is_translation"],
        "available_languages": available,
        "text": " ".join(segment["text"] for segment in segments),
        "segment_count": len(segments),
        "segments": segments,
    }
