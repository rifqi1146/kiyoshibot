from handlers.dl.details_fmt import DetailsReport, esc
from . import main as _main

def build_threads_details_report(data: dict) -> tuple[str, str, bool]:
    post_id = str(data.get("post_id") or "").strip()
    username = str(data.get("username") or "").strip()
    verified = bool(data.get("verified"))
    community_tag = str(data.get("community_tag") or "").strip()
    ts_text = str(data.get("timestamp_text") or "").strip() or "N/A"
    caption = str(data.get("caption") or "").strip()
    counts = list(data.get("counts") or [])
    items = list(data.get("items") or [])
    has_media = bool(items)

    profile_url = f"https://www.threads.com/@{username}" if username else ""
    post_url = f"https://www.threads.com/@{username}/post/{post_id}" if (username and post_id) else (f"https://www.threads.com/t/{post_id}" if post_id else "")

    rep = DetailsReport(
        "Threads Post Details",
        "Threads",
        meta_rich=[
            f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "",
            f'<a href="{esc(post_url)}"><code>{esc(post_id)}</code></a>' if (post_url and post_id) else (f"<code>{esc(post_id)}</code>" if post_id else ""),
            esc(ts_text),
        ],
        meta_plain=[
            f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "",
            f"<code>{esc(post_id)}</code>" if post_id else "",
            esc(ts_text),
        ],
    )
    rep.quote(caption)

    labels = ("Likes", "Replies", "Reposts", "Shares")
    rep.kv(
        "Engagement",
        "Metric",
        "Count",
        [(lbl, counts[idx] if idx < len(counts) and counts[idx] else "0", True) for idx, lbl in enumerate(labels)],
        right_align=True,
    )

    videos = sum(1 for it in items if it.get("type") == "video")
    photos = sum(1 for it in items if it.get("type") != "video")
    if videos and photos:
        media_type = "Mixed Media"
    elif videos > 1:
        media_type = "Video Carousel"
    elif videos == 1:
        media_type = "Video"
    elif photos > 1:
        media_type = "Photo Carousel"
    elif photos == 1:
        media_type = "Photo"
    else:
        media_type = "Text Post"

    author_handle = f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "N/A"
    post_rows = [("Media Type", media_type, False), ("Items", str(len(items)), True)]
    if community_tag:
        post_rows.append(("Topic / Tag", community_tag, False))
    rep.author("Author & Media", "", author_handle, "Verified" if verified else "Standard", "Property", "Value", post_rows)

    rich_html, plain_html = rep.build()
    return rich_html, plain_html, has_media

async def fetch_threads_details(url: str) -> tuple[dict, str, str, bool]:
    post_id = await _main.resolve_threads_post_id(url)
    if not post_id:
        raise RuntimeError("Threads post ID could not be resolved from URL.")
    body = await _main._fetch_threads_embed_html(post_id)
    data = _main._parse_threads_embed_details(body, post_id=post_id)
    rich_html, plain_html, has_media = build_threads_details_report(data)
    return data, rich_html, plain_html, has_media
