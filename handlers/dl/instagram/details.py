from handlers.dl.details_fmt import (
    DetailsReport,
    esc,
    fmt_duration,
    fmt_int,
    fmt_wib,
    tg_time_html,
)
from . import main as _main

def _post_type_label(details: dict, items: list[dict]) -> str:
    ptype = str(details.get("product_type") or "").strip().lower()
    mtype = int(details.get("media_type") or 0)
    if ptype == "clips":
        return "Reel"
    if ptype == "carousel_container" or mtype == 8 or len(items) > 1:
        return "Carousel"
    if mtype == 2 or (len(items) == 1 and items[0].get("type") == "video"):
        return "Video"
    if mtype == 1 or (len(items) == 1 and items[0].get("type") == "photo"):
        return "Photo"
    return "Post"

def build_instagram_details_report(meta: dict, url: str = "") -> tuple[str, str, bool]:
    details = meta.get("details") or {}
    items = meta.get("items") or []
    username = str(meta.get("username") or "").strip()
    nickname = str(meta.get("nickname") or "").strip() or username or "Instagram User"
    caption = str(meta.get("caption") or "").strip()
    shortcode = str(details.get("code") or _main._extract_shortcode(url) or "").strip()
    taken_at = int(details.get("taken_at") or 0)
    created_str = fmt_wib(taken_at)

    profile_url = f"https://www.instagram.com/{username}/" if username else ""
    post_url = f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""

    rep = DetailsReport(
        "Instagram Post Details",
        "Instagram",
        meta_rich=[
            f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "",
            f'<a href="{esc(post_url)}"><code>{esc(shortcode)}</code></a>' if post_url else (f"<code>{esc(shortcode)}</code>" if shortcode else ""),
            tg_time_html(taken_at, created_str),
        ],
        meta_plain=[
            f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "",
            f"<code>{esc(shortcode)}</code>" if shortcode else "",
            esc(created_str),
        ],
    )
    rep.quote(caption)

    counts_disabled = bool(details.get("counts_disabled"))
    like_raw = details.get("like_count")
    comment_raw = details.get("comment_count")
    play_raw = details.get("play_count")

    eng_rows = []
    if counts_disabled and like_raw is None:
        eng_rows.append(("Likes", "Hidden", False))
    elif like_raw is not None:
        eng_rows.append(("Likes", fmt_int(like_raw), True))
    else:
        eng_rows.append(("Likes", "N/A", False))
    eng_rows.append(("Comments", fmt_int(comment_raw), True) if comment_raw is not None else ("Comments", "N/A", False))
    if play_raw is not None:
        eng_rows.append(("Plays / Views", fmt_int(play_raw), True))
    rep.kv("Engagement", "Metric", "Count", eng_rows, right_align=True)

    author_handle = f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "N/A"
    post_rows = [("Type", _post_type_label(details, items), False)]
    if details.get("location"):
        post_rows.append(("Location", str(details["location"]), False))
    coauthors = details.get("coauthors") or []
    if coauthors:
        post_rows.append(("Co-authors", ", ".join(f"@{c}" for c in coauthors), False))
    rep.author(
        "Author & Post",
        nickname,
        author_handle,
        "Verified" if details.get("is_verified") else "Standard",
        "Property",
        "Value",
        post_rows,
    )

    has_media = bool(items)
    media_rows = []
    if len(items) == 1:
        it = items[0]
        w = int(it.get("width") or details.get("width") or 0)
        h = int(it.get("height") or details.get("height") or 0)
        dur = int(it.get("duration") or details.get("duration") or 0)
        media_rows.append(("Media Type", "Video" if it.get("type") == "video" else "Photo", False))
        if w and h:
            media_rows.append(("Resolution", f"{w}×{h}", True))
        if dur > 0:
            media_rows.append(("Duration", fmt_duration(dur), True))
    elif len(items) > 1:
        videos = sum(1 for it in items if it.get("type") == "video")
        photos = sum(1 for it in items if it.get("type") != "video")
        media_rows.append(("Slides", str(len(items)), True))
        if photos:
            media_rows.append(("Photos", str(photos), True))
        if videos:
            media_rows.append(("Videos", str(videos), True))
    else:
        w, h = int(details.get("width") or 0), int(details.get("height") or 0)
        if w and h:
            media_rows.append(("Resolution", f"{w}×{h}", True))

    if details.get("has_audio") is not None:
        media_rows.append(("Has Audio", "Yes" if details.get("has_audio") else "No", False))
    if details.get("audio_title"):
        audio_label = details["audio_title"]
        if details.get("audio_artist"):
            audio_label = f"{audio_label} — {details['audio_artist']}"
        media_rows.append(("Audio Track", audio_label, False))
    rep.kv("Media", "Property", "Value", media_rows)

    if len(items) > 1:
        html_rows = []
        plain_rows = []
        for idx, it in enumerate(items, start=1):
            kind = "Video" if it.get("type") == "video" else "Photo"
            w, h = int(it.get("width") or 0), int(it.get("height") or 0)
            dim = f"{w}×{h}" if (w and h) else "N/A"
            dur = fmt_duration(it.get("duration")) if it.get("type") == "video" else "—"
            html_rows.append(f'<tr><td>#{idx}</td><td>{esc(kind)}</td><td><code>{esc(dim)}</code></td><td align="right">{esc(dur)}</td></tr>')
            plain_rows.append(f"• #{idx} · {esc(kind)} · <code>{esc(dim)}</code>" + (f" · {esc(dur)}" if dur != "—" else ""))
        rep.grid(
            "Carousel Slides",
            [("Slide", False), ("Type", False), ("Resolution", False), ("Duration", True)],
            html_rows,
            plain_rows,
        )

    flag_rows = [
        ("Caption Edited", "Yes" if details.get("caption_edited") else "No", False),
        ("Comments Disabled", "Yes" if details.get("comments_disabled") else "No", False),
        ("Counts Hidden", "Yes" if counts_disabled else "No", False),
    ]
    if details.get("accessibility_caption"):
        flag_rows.append(("Alt Text", str(details["accessibility_caption"]), False))
    rep.details("Post Settings & Flags", "Setting", "Status", flag_rows)

    rich_html, plain_html = rep.build()
    return rich_html, plain_html, has_media

async def fetch_instagram_details(url: str) -> tuple[dict, str, str, bool]:
    shortcode = _main._extract_shortcode(url)
    if not shortcode:
        raise RuntimeError("Instagram post shortcode could not be resolved from URL.")
    cached = _main._meta_cache_get(shortcode)
    if cached and cached.get("details"):
        meta = cached
    else:
        try:
            meta = await _main._fetch_instagram_metadata(url)
        except Exception:
            meta = await _main._fetch_instagram_caption_meta(url)
            if not (meta.get("caption") or meta.get("username") or meta.get("nickname")):
                raise
        if meta.get("items"):
            _main._meta_cache_put(shortcode, meta)
    rich_html, plain_html, has_media = build_instagram_details_report(meta, url=url)
    return meta, rich_html, plain_html, has_media
