from email.utils import parsedate_to_datetime
from handlers.dl.details_fmt import (
    DetailsReport,
    esc,
    fmt_duration,
    fmt_int,
    fmt_wib,
    tg_time_html,
)
from . import main as _main

def _parse_pin_time(raw: str) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        return int(parsedate_to_datetime(text).timestamp())
    except Exception:
        return 0

def _collect_video_formats(pin: dict) -> list[dict]:
    out = []
    videos = pin.get("videos")
    if isinstance(videos, dict):
        vlist = videos.get("video_list") or {}
        if isinstance(vlist, dict):
            for key, v in vlist.items():
                if not isinstance(v, dict) or not v.get("url"):
                    continue
                dur = int(v.get("duration") or 0)
                out.append({
                    "format_id": str(key),
                    "width": int(v.get("width") or 0),
                    "height": int(v.get("height") or 0),
                    "duration": dur // 1000 if dur > 1000 else dur,
                    "is_hls": "HLS" in str(key).upper() or ".m3u8" in str(v.get("url") or "").lower(),
                })
    out.sort(key=lambda x: (x["width"] * x["height"], not x["is_hls"]), reverse=True)
    return out

def build_pinterest_details_report(pin: dict) -> tuple[str, str, bool]:
    pin_id = str(pin.get("id") or "").strip()
    title = str(pin.get("title") or pin.get("grid_title") or "").strip()
    desc = str(pin.get("description") or "").strip()
    if not title and desc:
        title, desc = desc, ""

    creator = pin.get("native_creator") or pin.get("pinner") or {}
    username = str(creator.get("username") or "").strip()
    full_name = str(creator.get("full_name") or creator.get("first_name") or username or "Pinterest User").strip()
    followers = creator.get("follower_count")
    domain_verified = bool(creator.get("domain_verified"))

    created_ts = _parse_pin_time(pin.get("created_at") or "")
    created_str = fmt_wib(created_ts) if created_ts else str(pin.get("created_at") or "N/A")
    profile_url = f"https://www.pinterest.com/{username}/" if username else ""
    pin_url = f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else ""

    rep = DetailsReport(
        "Pinterest Pin Details",
        "Pinterest",
        meta_rich=[
            f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "",
            f'<a href="{esc(pin_url)}"><code>{esc(pin_id)}</code></a>' if pin_url else (f"<code>{esc(pin_id)}</code>" if pin_id else ""),
            tg_time_html(created_ts, created_str),
        ],
        meta_plain=[
            f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "",
            f"<code>{esc(pin_id)}</code>" if pin_id else "",
            esc(created_str),
        ],
    )
    caption_text = f"{title}\n\n{desc}" if (title and desc and desc != title) else (title or desc)
    rep.quote(caption_text)

    agg = ((pin.get("aggregated_pin_data") or {}).get("aggregated_stats") or {})
    reactions = pin.get("reaction_counts") or {}
    reaction_total = sum(int(v or 0) for v in reactions.values()) if isinstance(reactions, dict) else 0
    eng_rows = [
        ("Total Saves", fmt_int(agg.get("saves") if agg.get("saves") is not None else pin.get("repin_count")), True),
        ("Repins", fmt_int(pin.get("repin_count")), True),
        ("Comments", fmt_int(pin.get("comment_count")), True),
        ("Reactions", fmt_int(reaction_total), True),
    ]
    if agg.get("done") is not None:
        eng_rows.append(("Tried / Done", fmt_int(agg.get("done")), True))
    rep.kv("Engagement", "Metric", "Count", eng_rows, right_align=True)

    board_name = str((pin.get("board") or {}).get("name") or "").strip()
    creator_handle = f'<a href="{esc(profile_url)}">@{esc(username)}</a>' if profile_url else "N/A"
    creator_rows = []
    if followers is not None:
        creator_rows.append(("Followers", fmt_int(followers), True))
    if board_name:
        creator_rows.append(("Board", board_name, False))
    if pin.get("domain"):
        creator_rows.append(("Source Domain", str(pin.get("domain")), True))
    rep.author("Creator & Board", full_name, creator_handle, "", "Property", "Value", creator_rows)

    video_formats = _collect_video_formats(pin)
    story = pin.get("story_pin_data") if isinstance(pin.get("story_pin_data"), dict) else None
    orig = ((pin.get("images") or {}).get("orig") or {}) if isinstance(pin.get("images"), dict) else {}
    has_media = bool(video_formats or orig.get("url") or story or (isinstance(pin.get("embed"), dict) and pin["embed"].get("src")))

    media_rows = []
    if video_formats:
        best = video_formats[0]
        media_rows.append(("Type", "Video Pin", False))
        if best["width"] and best["height"]:
            media_rows.append(("Resolution", f"{best['width']}×{best['height']}", True))
        if best["duration"] > 0:
            media_rows.append(("Duration", fmt_duration(best["duration"]), True))
    elif story:
        media_rows.extend([("Type", "Idea / Story Pin", False), ("Pages", str(len(story.get("pages") or [])), True)])
    elif orig.get("url"):
        w, h = int(orig.get("width") or 0), int(orig.get("height") or 0)
        media_rows.append(("Type", "Image Pin", False))
        if w and h:
            media_rows.append(("Resolution", f"{w}×{h}", True))
    else:
        media_rows.append(("Type", "Pin", False))
    rep.kv("Media", "Property", "Value", media_rows)

    if video_formats:
        html_rows = []
        plain_rows = []
        for vf in video_formats:
            dim = f"{vf['width']}×{vf['height']}" if (vf["width"] and vf["height"]) else "N/A"
            kind = "HLS" if vf["is_hls"] else "MP4"
            dur = fmt_duration(vf["duration"])
            html_rows.append(
                f'<tr><td><code>{esc(vf["format_id"])}</code></td><td><code>{esc(dim)}</code></td><td>{esc(kind)}</td><td align="right">{esc(dur)}</td></tr>'
            )
            plain_rows.append(f"• <code>{esc(vf['format_id'])}</code> · <code>{esc(dim)}</code> · {esc(kind)} · {esc(dur)}")
        rep.grid(
            "Available Streams",
            [("Format", False), ("Resolution", False), ("Type", False), ("Duration", True)],
            html_rows,
            plain_rows,
        )

    flag_rows = [
        ("Verified Domain", "Yes" if domain_verified else "No", False),
        ("Download Eligible", "Yes" if pin.get("is_eligible_for_image_download", True) else "No", False),
    ]
    if pin.get("link"):
        flag_rows.append(("Source Link", str(pin.get("link")), False))
    rep.details("Pin Settings & Source", "Property", "Value", flag_rows)

    rich_html, plain_html = rep.build()
    return rich_html, plain_html, has_media

async def fetch_pinterest_details(url: str) -> tuple[dict, str, str, bool]:
    pin = await _main.fetch_pinterest_pin(url)
    rich_html, plain_html, has_media = build_pinterest_details_report(pin)
    return pin, rich_html, plain_html, has_media
