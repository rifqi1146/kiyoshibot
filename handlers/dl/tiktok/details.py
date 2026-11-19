from handlers.dl.details_fmt import (
    DetailsReport,
    esc,
    fmt_bitrate,
    fmt_codec,
    fmt_duration,
    fmt_int,
    fmt_size,
    fmt_wib,
    tg_time_html,
)
from . import extractor as _extractor
from . import native as _native

def build_tiktok_details_report(item: dict) -> tuple[str, str]:
    vid_id = str(item.get("id") or "").strip()
    desc = str(item.get("desc") or "").strip()
    author = item.get("author") or {}
    uid = str(author.get("uniqueId") or "").strip()
    nick = str(author.get("nickname") or "").strip() or uid or "TikTok User"
    verified = bool(author.get("verified"))

    image_post = item.get("imagePost") or item.get("image_post") or {}
    images = image_post.get("images") if isinstance(image_post, dict) else []
    video = item.get("video") or {}
    is_slideshow = bool(images)

    created = int(item.get("createTime") or 0)
    created_str = fmt_wib(created)
    post_kind = "photo" if is_slideshow else "video"
    profile_url = f"https://www.tiktok.com/@{uid}" if uid else ""
    post_url = f"https://www.tiktok.com/@{uid}/{post_kind}/{vid_id}" if (uid and vid_id) else ""

    rep = DetailsReport(
        "TikTok Post Details",
        "TikTok",
        meta_rich=[
            f'<a href="{esc(profile_url)}">@{esc(uid)}</a>' if profile_url else "",
            f'<a href="{esc(post_url)}"><code>{esc(vid_id)}</code></a>' if post_url else (f"<code>{esc(vid_id)}</code>" if vid_id else ""),
            tg_time_html(created, created_str),
        ],
        meta_plain=[
            f'<a href="{esc(profile_url)}">@{esc(uid)}</a>' if profile_url else "",
            f"<code>{esc(vid_id)}</code>" if vid_id else "",
            esc(created_str),
        ],
    )
    rep.quote(desc)

    pstats = item.get("statsV2") or item.get("stats") or {}
    rep.kv(
        "Engagement",
        "Metric",
        "Count",
        [
            ("Views", fmt_int(pstats.get("playCount")), True),
            ("Likes", fmt_int(pstats.get("diggCount")), True),
            ("Comments", fmt_int(pstats.get("commentCount")), True),
            ("Shares", fmt_int(pstats.get("shareCount")), True),
            ("Saves", fmt_int(pstats.get("collectCount")), True),
            ("Reposts", fmt_int(pstats.get("repostCount")), True),
        ],
        right_align=True,
    )

    astats = item.get("authorStatsV2") or item.get("authorStats") or {}
    author_handle = f'<a href="{esc(profile_url)}">@{esc(uid)}</a>' if profile_url else "N/A"
    rep.author(
        "Author",
        nick,
        author_handle,
        "Verified" if verified else "Standard",
        "Profile Metric",
        "Count",
        [
            ("Followers", fmt_int(astats.get("followerCount")), True),
            ("Following", fmt_int(astats.get("followingCount")), True),
            ("Total Likes", fmt_int(astats.get("heartCount")), True),
            ("Videos", fmt_int(astats.get("videoCount")), True),
        ],
        right_align=True,
        bio=str(author.get("signature") or "").strip(),
    )

    if is_slideshow:
        rep.kv("Media", "Property", "Value", [("Type", "Photo Slideshow", False), ("Photos", str(len(images)), True)])
    elif video:
        dur = video.get("duration") or (item.get("music") or {}).get("duration") or 0
        w = int(video.get("width") or 0)
        h = int(video.get("height") or 0)
        ratio = str(video.get("ratio") or "").strip()
        res_val = f"{w}×{h}" if (w and h) else "N/A"
        res_html = f"<code>{esc(res_val)}</code> ({esc(ratio)})" if (ratio and res_val != "N/A") else f"<code>{esc(res_val)}</code>"
        rep.kv_raw(
            "Video",
            "Property",
            "Value",
            [
                ("Resolution", res_html),
                ("Duration", f"<code>{esc(fmt_duration(dur))}</code>"),
                ("Codec", f"<code>{esc(fmt_codec(video.get('codecType')))}</code>"),
                ("Bitrate", f"<code>{esc(fmt_bitrate(video.get('bitrate')))}</code>"),
                ("Format", f"<code>{esc(str(video.get('format') or 'mp4').upper())}</code>"),
            ],
        )

        variants = [v for v in (video.get("bitrateInfo") or []) if isinstance(v, dict)]
        if variants:
            variants.sort(
                key=lambda v: (
                    int((v.get("PlayAddr") or {}).get("Width") or 0) * int((v.get("PlayAddr") or {}).get("Height") or 0),
                    int(v.get("Bitrate") or 0),
                ),
                reverse=True,
            )
            html_rows = []
            plain_rows = []
            for var in variants:
                pa = var.get("PlayAddr") or {}
                vw = int(pa.get("Width") or 0)
                vh = int(pa.get("Height") or 0)
                fps = int(var.get("FPS") or var.get("BitrateFPS") or 30)
                codec = fmt_codec(var.get("CodecType"))
                br = fmt_bitrate(var.get("Bitrate"))
                sz = fmt_size(pa.get("DataSize"))
                dim = f"{vw}×{vh}" if (vw and vh) else "N/A"
                html_rows.append(
                    f'<tr><td><code>{esc(dim)}</code> · {fps}fps</td>'
                    f'<td><code>{esc(codec)}</code></td>'
                    f'<td align="right">{esc(br)}</td>'
                    f'<td align="right">{esc(sz)}</td></tr>'
                )
                plain_rows.append(f"• <code>{esc(dim)}</code> · {fps}fps · <code>{esc(codec)}</code> · {esc(br)} · {esc(sz)}")
            rep.grid(
                "Available Streams",
                [("Stream", False), ("Codec", False), ("Bitrate", True), ("Size", True)],
                html_rows,
                plain_rows,
            )

    music = item.get("music") or {}
    subs = [s for s in (video.get("subtitleInfos") or []) if isinstance(s, dict)]
    if music or subs:
        audio_rows = []
        if music:
            audio_rows.extend([
                ("Title", str(music.get("title") or "Original Sound"), False),
                ("Artist", str(music.get("authorName") or nick), False),
                ("Source", "Original Sound" if music.get("original") else "Reused / Licensed", False),
                ("Duration", fmt_duration(music.get("duration")), True),
            ])
        if subs:
            sub_names = []
            seen_subs = set()
            for s in subs:
                lang = str(s.get("LanguageCodeName") or s.get("LanguageID") or "Unknown").strip()
                fmt = str(s.get("Format") or "vtt").upper()
                label = f"{lang} ({fmt})"
                if label not in seen_subs:
                    seen_subs.add(label)
                    sub_names.append(label)
            if sub_names:
                audio_rows.append(("Subtitles", ", ".join(sub_names), False))
        rep.kv("Audio & Subtitles", "Property", "Value", audio_rows)

    flag_rows = [
        ("Commercial / Ad", "Yes" if item.get("isAd") else "No", False),
        ("Duet", "Allowed" if item.get("duetEnabled") else "Disabled", False),
        ("Stitch", "Allowed" if item.get("stitchEnabled") else "Disabled", False),
        ("Share", "Allowed" if item.get("shareEnabled") else "Disabled", False),
    ]
    text_lang = str(item.get("textLanguage") or "").strip()
    if text_lang and text_lang.lower() != "un":
        flag_rows.append(("Text Language", text_lang, True))
    for anc in (item.get("anchors") or []):
        if isinstance(anc, dict) and anc.get("description"):
            flag_rows.append(("Anchor", str(anc.get("description")), False))
            break
    rep.details("Post Settings & Flags", "Setting", "Status", flag_rows)

    return rep.build()

async def fetch_tiktok_details(url: str) -> tuple[dict, str, str]:
    aweme_id = await _extractor._resolve_aweme_id(url)
    if not aweme_id:
        raise RuntimeError("TikTok post ID could not be resolved from URL.")
    item = await _native.fetch_item(aweme_id)
    rich_html, plain_html = build_tiktok_details_report(item)
    return item, rich_html, plain_html
