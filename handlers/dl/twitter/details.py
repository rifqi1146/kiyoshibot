import html
import re
from datetime import datetime
from handlers.dl.adult_gate import check_adult_access
from handlers.dl.details_fmt import (
    DetailsReport,
    esc,
    fmt_bitrate,
    fmt_duration,
    fmt_int,
    fmt_wib,
    tg_time_html,
)
from handlers.dl.utils import AdultContentNotAllowed
from . import main as _main

_TAG_RE = re.compile(r"<[^>]+>")

def _parse_x_time(raw: str) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        return int(datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except Exception:
        return 0

def _extract_tweet_text(result: dict, legacy: dict) -> str:
    note = (((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    raw = str(note.get("text") or legacy.get("full_text") or "").strip()
    if not raw:
        return ""
    raw = html.unescape(raw)
    return re.sub(r"\s*https?://t\.co/\w+\s*$", "", raw, flags=re.I).strip()

def _strip_source(raw: str) -> str:
    return html.unescape(_TAG_RE.sub("", str(raw or ""))).strip()

def build_twitter_details_report(result: dict) -> tuple[str, str, bool]:
    legacy = result.get("legacy") or {}
    tweet_id = str(result.get("rest_id") or legacy.get("id_str") or "").strip()
    user_res = (((result.get("core") or {}).get("user_results") or {}).get("result") or {})
    u_legacy = user_res.get("legacy") or {}
    u_core = user_res.get("core") or {}

    screen_name = str(u_legacy.get("screen_name") or u_core.get("screen_name") or "").strip()
    name = str(u_legacy.get("name") or u_core.get("name") or screen_name or "X User").strip()
    verified = bool(user_res.get("is_blue_verified") or u_legacy.get("verified"))

    created_ts = _parse_x_time(legacy.get("created_at") or "")
    created_str = fmt_wib(created_ts) if created_ts else str(legacy.get("created_at") or "N/A")
    profile_url = f"https://x.com/{screen_name}" if screen_name else ""
    post_url = f"https://x.com/{screen_name}/status/{tweet_id}" if (screen_name and tweet_id) else (f"https://x.com/i/status/{tweet_id}" if tweet_id else "")

    rep = DetailsReport(
        "X Post Details",
        "X",
        meta_rich=[
            f'<a href="{esc(profile_url)}">@{esc(screen_name)}</a>' if profile_url else "",
            f'<a href="{esc(post_url)}"><code>{esc(tweet_id)}</code></a>' if post_url else (f"<code>{esc(tweet_id)}</code>" if tweet_id else ""),
            tg_time_html(created_ts, created_str),
        ],
        meta_plain=[
            f'<a href="{esc(profile_url)}">@{esc(screen_name)}</a>' if profile_url else "",
            f"<code>{esc(tweet_id)}</code>" if tweet_id else "",
            esc(created_str),
        ],
    )
    rep.quote(_extract_tweet_text(result, legacy))

    views_raw = (result.get("views") or {}).get("count")
    rep.kv(
        "Engagement",
        "Metric",
        "Count",
        [
            ("Views", fmt_int(views_raw) if views_raw is not None else "N/A", True),
            ("Likes", fmt_int(legacy.get("favorite_count")), True),
            ("Reposts", fmt_int(legacy.get("retweet_count")), True),
            ("Replies", fmt_int(legacy.get("reply_count")), True),
            ("Quotes", fmt_int(legacy.get("quote_count")), True),
            ("Bookmarks", fmt_int(legacy.get("bookmark_count")), True),
        ],
        right_align=True,
    )

    author_handle = f'<a href="{esc(profile_url)}">@{esc(screen_name)}</a>' if profile_url else "N/A"
    joined_ts = _parse_x_time(u_legacy.get("created_at") or "")
    joined_str = fmt_wib(joined_ts).split(",")[0] if joined_ts else "N/A"
    rep.author(
        "Author",
        name,
        author_handle,
        "Blue Verified" if verified else "Standard",
        "Profile Metric",
        "Value",
        [
            ("Followers", fmt_int(u_legacy.get("followers_count")), True),
            ("Following", fmt_int(u_legacy.get("friends_count")), True),
            ("Posts", fmt_int(u_legacy.get("statuses_count")), True),
            ("Likes", fmt_int(u_legacy.get("favourites_count")), True),
            ("Joined", joined_str, False),
        ],
        right_align=True,
        bio=html.unescape(str(u_legacy.get("description") or "").strip()),
    )

    extended = legacy.get("extended_entities") or {}
    entities = legacy.get("entities") or {}
    media_list = extended.get("media") or entities.get("media") or []
    has_media = bool(media_list)

    if not media_list:
        rep.kv("Media", "Property", "Value", [("Type", "Text Post", False)])
    else:
        types = [str(m.get("type") or "photo").lower() for m in media_list]
        kind_label = "Video" if "video" in types else ("GIF" if "animated_gif" in types else "Photo")
        if len(media_list) > 1 and len(set(types)) > 1:
            kind_label = "Mixed Media"
        elif len(media_list) > 1 and kind_label == "Photo":
            kind_label = "Photo Album"

        media_rows = [("Type", kind_label, False), ("Items", str(len(media_list)), True)]
        first_video = next((m for m in media_list if str(m.get("type") or "").lower() in ("video", "animated_gif")), None)
        if first_video:
            dur_ms = int(((first_video.get("video_info") or {}).get("duration_millis")) or 0)
            if dur_ms > 0:
                media_rows.append(("Duration", fmt_duration(dur_ms / 1000.0), True))
        else:
            orig = media_list[0].get("original_info") or {}
            w, h = int(orig.get("width") or 0), int(orig.get("height") or 0)
            if w and h:
                media_rows.append(("Resolution", f"{w}×{h}", True))
        rep.kv("Media", "Property", "Value", media_rows)

        html_rows = []
        plain_rows = []
        for idx, m in enumerate(media_list, start=1):
            if str(m.get("type") or "").lower() not in ("video", "animated_gif"):
                continue
            mp4s = [v for v in ((m.get("video_info") or {}).get("variants") or []) if (v.get("content_type") or "") == "video/mp4" and v.get("url")]
            mp4s.sort(key=lambda v: int(v.get("bitrate") or 0), reverse=True)
            for v in mp4s:
                vw, vh = _main._resolution_from_url(v.get("url") or "")
                dim = f"{vw}×{vh}" if (vw and vh) else "N/A"
                br = fmt_bitrate(v.get("bitrate"))
                label = f"#{idx}" if len(media_list) > 1 else "MP4"
                html_rows.append(f'<tr><td>{esc(label)}</td><td><code>{esc(dim)}</code></td><td align="right">{esc(br)}</td></tr>')
                plain_rows.append(f"• {esc(label)} · <code>{esc(dim)}</code> · {esc(br)}")
        rep.grid("Available Streams", [("Stream", False), ("Resolution", False), ("Bitrate", True)], html_rows, plain_rows)

    sensitive = _main._tweet_is_sensitive(legacy)
    flag_rows = [
        ("Language", str(legacy.get("lang") or "N/A").upper(), True),
        ("Quote Post", "Yes" if legacy.get("is_quote_status") else "No", False),
        ("Sensitive Media", "Yes" if sensitive else "No", False),
    ]
    src = _strip_source(legacy.get("source") or "")
    if src:
        flag_rows.append(("Client", src, False))
    rep.details("Post Settings & Flags", "Setting", "Status", flag_rows)

    rich_html, plain_html = rep.build()
    return rich_html, plain_html, has_media

async def fetch_twitter_details(
    url: str,
    user_id: int | None = None,
    chat_id: int | None = None,
    chat_type: str = "",
    strict_adult: bool = False,
) -> tuple[dict, str, str, bool]:
    text = (url or "").strip()
    if _main.SHORT_RE.match(text):
        text = await _main._resolve_short_url(text)
    tweet_id = _main._extract_status_id(text)
    if not tweet_id:
        raise RuntimeError("X post ID could not be resolved from URL.")
    result = await _main._get_tweet_result(tweet_id)
    legacy = result.get("legacy") or {}
    if strict_adult and _main._tweet_is_sensitive(legacy):
        allowed, reason = check_adult_access(int(user_id or 0), int(chat_id or 0), chat_type or "private")
        if not allowed:
            raise AdultContentNotAllowed("X post contains sensitive media", reason)
    rich_html, plain_html, has_media = build_twitter_details_report(result)
    return result, rich_html, plain_html, has_media
