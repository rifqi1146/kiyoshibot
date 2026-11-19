import html
from datetime import datetime
from zoneinfo import ZoneInfo
from . import native as _native
from .extractor import _resolve_aweme_id

def _fmt_int(val) -> str:
    try:
        return f"{int(val):,}"
    except (TypeError, ValueError):
        return "0"

def _fmt_duration(seconds) -> str:
    try:
        s = int(seconds or 0)
        if s <= 0:
            return "N/A"
        if s >= 60:
            return f"{s // 60}m {s % 60}s"
        return f"{s}s"
    except (TypeError, ValueError):
        return "N/A"

def _fmt_codec(raw: str) -> str:
    c = str(raw or "").strip().lower()
    if not c:
        return "N/A"
    if "264" in c or "avc" in c:
        return "H.264"
    if "265" in c or "hvc" in c or "hevc" in c or "bytevc1" in c:
        return "H.265"
    if "av1" in c or "av01" in c:
        return "AV1"
    return c.upper()

def _fmt_bitrate(bps) -> str:
    try:
        val = int(bps or 0)
        if val <= 0:
            return "N/A"
        return f"{round(val / 1000):,} kbps"
    except (TypeError, ValueError):
        return "N/A"

def _fmt_size(size_bytes) -> str:
    try:
        sz = int(size_bytes or 0)
        if sz <= 0:
            return "N/A"
        return f"{sz / (1024 * 1024):.2f} MB"
    except (TypeError, ValueError):
        return "N/A"

def _esc(val) -> str:
    return html.escape(str(val or ""))

def _quote_block(text: str) -> str:
    escaped = _esc(text)
    if len(text) > 140 or text.count("\n") >= 2:
        return f"<blockquote expandable>{escaped}</blockquote>"
    return f"<blockquote>{escaped}</blockquote>"

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
    created_str = (
        datetime.fromtimestamp(created, ZoneInfo("Asia/Jakarta")).strftime("%b %d, %Y, %H:%M WIB")
        if created
        else "N/A"
    )

    post_kind = "photo" if is_slideshow else "video"
    profile_url = f"https://www.tiktok.com/@{uid}" if uid else ""
    post_url = f"https://www.tiktok.com/@{uid}/{post_kind}/{vid_id}" if (uid and vid_id) else ""

    meta_parts = []
    if profile_url:
        meta_parts.append(f'<a href="{_esc(profile_url)}">@{_esc(uid)}</a>')
    if post_url:
        meta_parts.append(f'<a href="{_esc(post_url)}"><code>{_esc(vid_id)}</code></a>')
    elif vid_id:
        meta_parts.append(f"<code>{_esc(vid_id)}</code>")
    if created > 0:
        meta_parts.append(f'<tg-time unix="{created}" format="wDT">{_esc(created_str)}</tg-time>')
    else:
        meta_parts.append(_esc(created_str))

    rich_parts = [
        "<h1>TikTok Post Details</h1>",
        f"<p>{' • '.join(meta_parts)}</p>",
    ]
    plain_parts = [
        "<b>TikTok Post Details</b>",
        " • ".join(
            p for p in (
                f'<a href="{_esc(profile_url)}">@{_esc(uid)}</a>' if profile_url else "",
                f"<code>{_esc(vid_id)}</code>" if vid_id else "",
                _esc(created_str),
            ) if p
        ),
    ]

    if desc:
        q_html = _quote_block(desc)
        rich_parts.append(q_html)
        plain_parts.append(q_html)

    pstats = item.get("statsV2") or item.get("stats") or {}
    eng_rows = [
        ("Views", _fmt_int(pstats.get("playCount"))),
        ("Likes", _fmt_int(pstats.get("diggCount"))),
        ("Comments", _fmt_int(pstats.get("commentCount"))),
        ("Shares", _fmt_int(pstats.get("shareCount"))),
        ("Saves", _fmt_int(pstats.get("collectCount"))),
        ("Reposts", _fmt_int(pstats.get("repostCount"))),
    ]
    rich_parts.append("<h3>Engagement</h3>")
    rich_parts.append(
        '<table bordered striped compact>'
        '<tr><th>Metric</th><th align="right">Count</th></tr>'
        + "".join(
            f'<tr><td>{_esc(lbl)}</td><td align="right"><code>{_esc(val)}</code></td></tr>'
            for lbl, val in eng_rows
        )
        + "</table>"
    )
    plain_parts.append("")
    plain_parts.append("<b>Engagement</b>")
    plain_parts.extend(f"• <b>{_esc(lbl)}:</b> <code>{_esc(val)}</code>" for lbl, val in eng_rows)

    astats = item.get("authorStatsV2") or item.get("authorStats") or {}
    badge = "Verified" if verified else "Standard"
    author_handle = f'<a href="{_esc(profile_url)}">@{_esc(uid)}</a>' if profile_url else "N/A"
    author_rows = [
        ("Followers", _fmt_int(astats.get("followerCount"))),
        ("Following", _fmt_int(astats.get("followingCount"))),
        ("Total Likes", _fmt_int(astats.get("heartCount"))),
        ("Videos", _fmt_int(astats.get("videoCount"))),
    ]
    rich_parts.append("<h3>Author</h3>")
    rich_parts.append(f"<p><b>{_esc(nick)}</b> ({author_handle}) • {_esc(badge)}</p>")
    rich_parts.append(
        '<table bordered striped compact>'
        '<tr><th>Profile Metric</th><th align="right">Count</th></tr>'
        + "".join(
            f'<tr><td>{_esc(lbl)}</td><td align="right"><code>{_esc(val)}</code></td></tr>'
            for lbl, val in author_rows
        )
        + "</table>"
    )
    plain_parts.append("")
    plain_parts.append(f"<b>Author:</b> {_esc(nick)} ({author_handle}) • {_esc(badge)}")
    plain_parts.extend(f"• <b>{_esc(lbl)}:</b> <code>{_esc(val)}</code>" for lbl, val in author_rows)

    sig = str(author.get("signature") or "").strip()
    if sig:
        bio_html = _quote_block(sig)
        rich_parts.append(bio_html)
        plain_parts.append(bio_html)

    if is_slideshow:
        media_rows = [
            ("Type", "Photo Slideshow"),
            ("Photos", str(len(images))),
        ]
        rich_parts.append("<h3>Media</h3>")
        rich_parts.append(
            '<table bordered striped compact>'
            '<tr><th>Property</th><th>Value</th></tr>'
            + "".join(
                f"<tr><td>{_esc(lbl)}</td><td><code>{_esc(val)}</code></td></tr>"
                for lbl, val in media_rows
            )
            + "</table>"
        )
        plain_parts.append("")
        plain_parts.append("<b>Media</b>")
        plain_parts.extend(f"• <b>{_esc(lbl)}:</b> <code>{_esc(val)}</code>" for lbl, val in media_rows)
    elif video:
        dur = video.get("duration") or (item.get("music") or {}).get("duration") or 0
        w = int(video.get("width") or 0)
        h = int(video.get("height") or 0)
        ratio = str(video.get("ratio") or "").strip()
        res_val = f"{w}×{h}" if (w and h) else "N/A"
        if ratio and res_val != "N/A":
            res_html = f"<code>{_esc(res_val)}</code> ({_esc(ratio)})"
            res_plain = f"<code>{_esc(res_val)}</code> ({_esc(ratio)})"
        else:
            res_html = f"<code>{_esc(res_val)}</code>"
            res_plain = f"<code>{_esc(res_val)}</code>"

        spec_rows = [
            ("Resolution", res_html, res_plain),
            ("Duration", f"<code>{_esc(_fmt_duration(dur))}</code>", f"<code>{_esc(_fmt_duration(dur))}</code>"),
            ("Codec", f"<code>{_esc(_fmt_codec(video.get('codecType')))}</code>", f"<code>{_esc(_fmt_codec(video.get('codecType')))}</code>"),
            ("Bitrate", f"<code>{_esc(_fmt_bitrate(video.get('bitrate')))}</code>", f"<code>{_esc(_fmt_bitrate(video.get('bitrate')))}</code>"),
            ("Format", f"<code>{_esc(str(video.get('format') or 'mp4').upper())}</code>", f"<code>{_esc(str(video.get('format') or 'mp4').upper())}</code>"),
        ]
        rich_parts.append("<h3>Video</h3>")
        rich_parts.append(
            '<table bordered striped compact>'
            '<tr><th>Property</th><th>Value</th></tr>'
            + "".join(f"<tr><td>{_esc(lbl)}</td><td>{val_html}</td></tr>" for lbl, val_html, _ in spec_rows)
            + "</table>"
        )
        plain_parts.append("")
        plain_parts.append("<b>Video</b>")
        plain_parts.extend(f"• <b>{_esc(lbl)}:</b> {val_plain}" for lbl, _, val_plain in spec_rows)

        variants = [v for v in (video.get("bitrateInfo") or []) if isinstance(v, dict)]
        if variants:
            def _var_sort_key(v: dict) -> tuple[int, int]:
                pa = v.get("PlayAddr") or {}
                vw = int(pa.get("Width") or 0)
                vh = int(pa.get("Height") or 0)
                return (vw * vh, int(v.get("Bitrate") or 0))

            variants.sort(key=_var_sort_key, reverse=True)
            stream_rows_html = []
            stream_rows_plain = []
            for var in variants:
                pa = var.get("PlayAddr") or {}
                vw = int(pa.get("Width") or 0)
                vh = int(pa.get("Height") or 0)
                fps = int(var.get("FPS") or var.get("BitrateFPS") or 30)
                codec = _fmt_codec(var.get("CodecType"))
                br = _fmt_bitrate(var.get("Bitrate"))
                sz = _fmt_size(pa.get("DataSize"))
                dim = f"{vw}×{vh}" if (vw and vh) else "N/A"
                stream_rows_html.append(
                    f'<tr><td><code>{_esc(dim)}</code> · {fps}fps</td>'
                    f'<td><code>{_esc(codec)}</code></td>'
                    f'<td align="right">{_esc(br)}</td>'
                    f'<td align="right">{_esc(sz)}</td></tr>'
                )
                stream_rows_plain.append(
                    f"• <code>{_esc(dim)}</code> · {fps}fps · <code>{_esc(codec)}</code> · {_esc(br)} · {_esc(sz)}"
                )
            rich_parts.append("<h3>Available Streams</h3>")
            rich_parts.append(
                '<table bordered striped compact>'
                '<tr><th>Stream</th><th>Codec</th><th align="right">Bitrate</th><th align="right">Size</th></tr>'
                + "".join(stream_rows_html)
                + "</table>"
            )
            plain_parts.append("")
            plain_parts.append("<b>Available Streams</b>")
            plain_parts.extend(stream_rows_plain)

    music = item.get("music") or {}
    subs = [s for s in (video.get("subtitleInfos") or []) if isinstance(s, dict)]
    if music or subs:
        audio_rows = []
        if music:
            audio_rows.extend([
                ("Title", str(music.get("title") or "Original Sound"), False),
                ("Artist", str(music.get("authorName") or nick), False),
                ("Source", "Original Sound" if music.get("original") else "Reused / Licensed", False),
                ("Duration", _fmt_duration(music.get("duration")), True),
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

        rich_parts.append("<h3>Audio &amp; Subtitles</h3>")
        rich_parts.append(
            '<table bordered striped compact>'
            '<tr><th>Property</th><th>Value</th></tr>'
            + "".join(
                f"<tr><td>{_esc(lbl)}</td><td>{'<code>' + _esc(val) + '</code>' if is_code else _esc(val)}</td></tr>"
                for lbl, val, is_code in audio_rows
            )
            + "</table>"
        )
        plain_parts.append("")
        plain_parts.append("<b>Audio &amp; Subtitles</b>")
        plain_parts.extend(
            f"• <b>{_esc(lbl)}:</b> {'<code>' + _esc(val) + '</code>' if is_code else _esc(val)}"
            for lbl, val, is_code in audio_rows
        )

    flag_rows = [
        ("Commercial / Ad", "Yes" if item.get("isAd") else "No"),
        ("Duet", "Allowed" if item.get("duetEnabled") else "Disabled"),
        ("Stitch", "Allowed" if item.get("stitchEnabled") else "Disabled"),
        ("Share", "Allowed" if item.get("shareEnabled") else "Disabled"),
    ]
    text_lang = str(item.get("textLanguage") or "").strip()
    if text_lang and text_lang.lower() != "un":
        flag_rows.append(("Text Language", text_lang))
    for anc in (item.get("anchors") or []):
        if isinstance(anc, dict) and anc.get("description"):
            flag_rows.append(("Anchor", str(anc.get("description"))))
            break

    rich_parts.append("<hr/>")
    rich_parts.append(
        "<details><summary><b>Post Settings &amp; Flags</b></summary>"
        '<table bordered striped compact>'
        '<tr><th>Setting</th><th>Status</th></tr>'
        + "".join(f"<tr><td>{_esc(lbl)}</td><td>{_esc(val)}</td></tr>" for lbl, val in flag_rows)
        + "</table></details>"
    )
    rich_parts.append("<footer>KiyoshiBot TikTok Inspector</footer>")

    plain_parts.append("")
    plain_parts.append("<b>Post Settings &amp; Flags</b>")
    plain_parts.extend(f"• <b>{_esc(lbl)}:</b> {_esc(val)}" for lbl, val in flag_rows)
    plain_parts.append("")
    plain_parts.append("<i>KiyoshiBot TikTok Inspector</i>")

    return "".join(rich_parts), "\n".join(plain_parts)

async def fetch_tiktok_details(url: str) -> tuple[dict, str, str]:
    aweme_id = await _resolve_aweme_id(url)
    if not aweme_id:
        raise RuntimeError("TikTok post ID could not be resolved from URL.")
    item = await _native.fetch_item(aweme_id)
    rich_html, plain_html = build_tiktok_details_report(item)
    return item, rich_html, plain_html
