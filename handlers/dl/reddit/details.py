import html
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

def _has_downloadable_media(post: dict) -> bool:
    return bool(
        _main._parse_gallery_items(post)
        or _main._parse_reddit_video(post)
        or _main._parse_preview_video(post)
        or _main._parse_direct_url(post)
        or _main._parse_preview_image(post)
    )

def build_reddit_details_report(post: dict) -> tuple[str, str, bool]:
    post_id = str(post.get("id") or "").strip()
    title = html.unescape(str(post.get("title") or "").strip()) or "Reddit Post"
    selftext = html.unescape(str(post.get("selftext") or "").strip())
    author = str(post.get("author") or "[deleted]").strip()
    sub = str(post.get("subreddit_name_prefixed") or "").strip()
    if not sub and post.get("subreddit"):
        sub = f"r/{post.get('subreddit')}"
    permalink = str(post.get("permalink") or "").strip()
    post_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else ""
    sub_url = f"https://www.reddit.com/{sub}" if sub else ""
    author_url = f"https://www.reddit.com/user/{author}" if author and author != "[deleted]" else ""

    created_ts = int(float(post.get("created_utc") or 0))
    created_str = fmt_wib(created_ts)

    rep = DetailsReport(
        "Reddit Post Details",
        "Reddit",
        meta_rich=[
            f'<a href="{esc(sub_url)}">{esc(sub)}</a>' if sub_url else "",
            f'<a href="{esc(author_url)}">u/{esc(author)}</a>' if author_url else (f"u/{esc(author)}" if author else ""),
            f'<a href="{esc(post_url)}"><code>{esc(post_id)}</code></a>' if (post_url and post_id) else (f"<code>{esc(post_id)}</code>" if post_id else ""),
            tg_time_html(created_ts, created_str),
        ],
        meta_plain=[
            f'<a href="{esc(sub_url)}">{esc(sub)}</a>' if sub_url else "",
            f"u/{esc(author)}" if author else "",
            f"<code>{esc(post_id)}</code>" if post_id else "",
            esc(created_str),
        ],
    )
    rep.quote(title)
    rep.quote(selftext)

    ratio = float(post.get("upvote_ratio") or 0.0)
    rep.kv(
        "Engagement",
        "Metric",
        "Count",
        [
            ("Score", fmt_int(post.get("score")), True),
            ("Upvote Ratio", f"{round(ratio * 100)}%" if ratio > 0 else "N/A", True),
            ("Comments", fmt_int(post.get("num_comments")), True),
            ("Awards", fmt_int(post.get("total_awards_received")), True),
            ("Crossposts", fmt_int(post.get("num_crossposts")), True),
        ],
        right_align=True,
    )

    post_rows = [
        ("Subreddit", sub or "N/A", False),
        ("Author", f"u/{author}" if author else "N/A", False),
        ("Domain", str(post.get("domain") or "self"), True),
    ]
    flair = html.unescape(str(post.get("link_flair_text") or "").strip())
    if flair:
        post_rows.append(("Flair", flair, False))
    rep.kv("Post Info", "Property", "Value", post_rows)

    media_obj = post.get("media") or {}
    secure_obj = post.get("secure_media") or {}
    rv = media_obj.get("reddit_video") or secure_obj.get("reddit_video") or {}
    gallery_items = _main._parse_gallery_items(post)
    has_media = _has_downloadable_media(post)

    media_rows = []
    if rv:
        w, h = int(rv.get("width") or 0), int(rv.get("height") or 0)
        dur = int(rv.get("duration") or 0)
        br = int(rv.get("bitrate_kbps") or 0)
        has_audio = bool(rv.get("has_audio")) if "has_audio" in rv else (not bool(rv.get("is_gif")))
        media_rows.append(("Type", "GIF" if rv.get("is_gif") else "Reddit Video", False))
        if w and h:
            media_rows.append(("Resolution", f"{w}×{h}", True))
        if dur > 0:
            media_rows.append(("Duration", fmt_duration(dur), True))
        if br > 0:
            media_rows.append(("Bitrate", fmt_bitrate(br * 1000), True))
        media_rows.append(("Audio", "Yes" if has_audio else "No", False))
    elif gallery_items:
        photos = sum(1 for x in gallery_items if x.get("type") == "photo")
        videos = sum(1 for x in gallery_items if x.get("type") == "video")
        media_rows.extend([("Type", "Gallery", False), ("Items", str(len(gallery_items)), True)])
        if photos:
            media_rows.append(("Photos", str(photos), True))
        if videos:
            media_rows.append(("Animated", str(videos), True))
    else:
        preview_imgs = ((post.get("preview") or {}).get("images") or [])
        if preview_imgs:
            src = ((preview_imgs[0] or {}).get("source") or {})
            w, h = int(src.get("width") or 0), int(src.get("height") or 0)
            media_rows.append(("Type", "Image" if has_media else "Link Preview", False))
            if w and h:
                media_rows.append(("Resolution", f"{w}×{h}", True))
        else:
            media_rows.append(("Type", "Text / Link Post", False))
    rep.kv("Media", "Property", "Value", media_rows)

    rep.details(
        "Post Settings & Flags",
        "Setting",
        "Status",
        [
            ("NSFW (18+)", "Yes" if post.get("over_18") else "No", False),
            ("Spoiler", "Yes" if post.get("spoiler") else "No", False),
            ("Locked", "Yes" if post.get("locked") else "No", False),
            ("Stickied", "Yes" if post.get("stickied") else "No", False),
            ("Original Content", "Yes" if post.get("is_original_content") else "No", False),
        ],
    )

    rich_html, plain_html = rep.build()
    return rich_html, plain_html, has_media

async def fetch_reddit_details(
    url: str,
    user_id: int | None = None,
    chat_id: int | None = None,
    chat_type: str = "",
    strict_adult: bool = False,
) -> tuple[dict, str, str, bool]:
    post = await _main.fetch_reddit_post(url)
    if strict_adult and bool(post.get("over_18")):
        allowed, reason = check_adult_access(int(user_id or 0), int(chat_id or 0), chat_type or "private")
        if not allowed:
            raise AdultContentNotAllowed("Reddit post is marked NSFW (over_18)", reason)
    rich_html, plain_html, has_media = build_reddit_details_report(post)
    return post, rich_html, plain_html, has_media
