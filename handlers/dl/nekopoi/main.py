import os
import time
import uuid
import shutil
import logging
import asyncio
from urllib.parse import urlparse, urlsplit

from handlers.dl.constants import TMP_DIR, MAX_TG_SIZE
from handlers.dl.utils import sanitize_filename, FileSizeLimitExceeded

from .constants import EMBED_HOST_STREAMPOI, EMBED_HOST_DOOD

log = logging.getLogger(__name__)

_VARIANT_CACHE: dict = {}
_CACHE_TTL = 300.0


def _import_extractor():
    from . import extractor
    return extractor


def is_nekopoi_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("nekopoi.care", "nekopoi.best", "nekopoi.pw", "nekopoi.win"):
        return False
    return bool((parsed.path or "").strip("/"))


def _prune_cache():
    now = time.time()
    for key in [k for k, v in list(_VARIANT_CACHE.items()) if now - v.get("ts", 0) > _CACHE_TTL]:
        _VARIANT_CACHE.pop(key, None)


def cache_probe(post_url: str, probe: dict) -> None:
    _prune_cache()
    _VARIANT_CACHE[(post_url or "").strip()] = {"ts": time.time(), "probe": probe}


def peek_probe(post_url: str) -> dict | None:
    item = _VARIANT_CACHE.get((post_url or "").strip())
    if not item:
        return None
    if time.time() - item.get("ts", 0) > _CACHE_TTL:
        _VARIANT_CACHE.pop((post_url or "").strip(), None)
        return None
    return item.get("probe")


def _dedup_by_height(variants: list) -> list:
    best: dict = {}
    for v in variants:
        height = int(v.get("height") or 0)
        if height <= 0:
            continue
        cur = best.get(height)
        if cur is None or int(v.get("bandwidth") or 0) > int(cur.get("bandwidth") or 0):
            best[height] = v
    out = list(best.values())
    out.sort(key=lambda v: int(v.get("height") or 0), reverse=True)
    for v in out:
        v["format_id"] = str(int(v.get("height") or 0))
    return out


def _list_download_hosts(html_text: str) -> list[str]:
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return []
    try:
        soup = BeautifulSoup(html_text or "", "html.parser")
    except Exception:
        return []
    seen: list[str] = []
    for row in soup.find_all(class_="nk-download-row"):
        for a in row.find_all("a", href=True):
            label = a.get_text(strip=True)
            low = label.lower()
            if not label or "pixel" in low or "mp4upload" in low:
                continue
            if label not in seen:
                seen.append(label)
    return seen


def probe_nekopoi(raw_url: str) -> dict:
    ext = _import_extractor()
    post = ext.scrape_post(raw_url)
    title = sanitize_filename(post.get("title") or "Nekopoi", 100)
    embeds = post.get("embeds") or []
    if not embeds:
        raise RuntimeError("No embed stream on Nekopoi page")

    def scrape_pixeldrain_variants(html_text: str) -> list:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        out_vars = []
        for row in soup.find_all(class_="nk-download-row"):
            name_div = row.find(class_="nk-download-name")
            name = name_div.get_text(strip=True).lower() if name_div else ""
            h = 0
            if "1080" in name: h = 1080
            elif "720" in name: h = 720
            elif "480" in name: h = 480
            elif "360" in name: h = 360

            hrefs = [a["href"] for a in row.find_all("a", href=True)
                     if "pixel" in a.get_text(strip=True).lower()]

            mp4_hrefs = [a["href"] for a in row.find_all("a", href=True)
                         if "mp4upload" in a.get_text(strip=True).lower()]

            hrefs = [u for u in hrefs if not ext.shortener_dead(u)]
            mp4_hrefs = [u for u in mp4_hrefs if not ext.shortener_dead(u)]

            if not hrefs and not mp4_hrefs:
                continue

            def _is_ouo(u: str) -> bool:
                host = (urlsplit(u).hostname or "").lower()
                return host.endswith("ouo.io") or host.endswith("ouo.press")

            chosen_pd = ""
            if hrefs:
                chosen_pd = next((u for u in hrefs if _is_ouo(u)), hrefs[0])
            chosen_mp4 = ""
            if mp4_hrefs:
                chosen_mp4 = next((u for u in mp4_hrefs if _is_ouo(u)), mp4_hrefs[0])

            if chosen_pd:
                out_vars.append({
                    "height": h or 480,
                    "bandwidth": h * 1000 if h else 480000,
                    "url": chosen_pd,
                    "alt_url": chosen_mp4,
                    "alt_type": "mp4upload_ouo" if chosen_mp4 else "",
                    "format_id": str(h or 480),
                    "type": "pixeldrain_ouo",
                })
            else:
                out_vars.append({
                    "height": h or 480,
                    "bandwidth": h * 1000 if h else 480000,
                    "url": chosen_mp4,
                    "format_id": str(h or 480),
                    "type": "mp4upload_ouo",
                })
        return out_vars

    def _resolve_pixeldrain(ouo_url: str) -> tuple[str, int]:
        bypassed = ext.resolve_shortener(ouo_url)
        if not bypassed or "pixeldrain.com/u/" not in bypassed:
            return "", 0
        pid = bypassed.split("/u/")[-1].split("?")[0].split("/")[0]
        api_url = f"https://pixeldrain.com/api/file/{pid}"
        sz = ext._probe_direct_size(api_url, {"User-Agent": ext.UA})
        return api_url, sz

    primary_err = None
    last_err = None
    hls_variants: list = []

    for emb in embeds:
        emb_host = (urlsplit(emb).hostname or "").lower()
        is_primary = any(emb_host == d or emb_host.endswith("." + d) for d in EMBED_HOST_STREAMPOI)
        if not is_primary:
            continue
        log.info("Nekopoi try HLS | embed=%s", emb)
        try:
            master = ext.probe_stream(emb, referer=raw_url)
            variants = _dedup_by_height(ext.list_resolutions(master))
            if not variants:
                raise RuntimeError("No resolution variants in Nekopoi playlist")
            for v in variants:
                v["type"] = "hls"
                v["master"] = master
            hls_variants = variants
            log.info("Nekopoi HLS ready | master=%s heights=%s", master, [v["height"] for v in variants])
            break
        except RuntimeError as e:
            log.info("Nekopoi HLS failed | embed=%s err=%s", emb_host, e)
            last_err = e
            if primary_err is None:
                primary_err = e
            continue

    dood_variants: list = []
    for emb in embeds:
        emb_host = (urlsplit(emb).hostname or "").lower()
        is_dood = any(emb_host == d or emb_host.endswith("." + d) for d in EMBED_HOST_DOOD)
        if not is_dood:
            continue
        try:
            info = ext.probe_doodstream(emb, referer=raw_url)
        except Exception as e:
            log.debug("DoodStream embed failed | embed=%s err=%r", emb, e)
            continue
        if info:
            dood_variants.append(info)

    pd_variants = scrape_pixeldrain_variants(post.get("raw_html") or "")

    by_height: dict = {}

    def _offer(v: dict, rank: int) -> None:
        h = int(v.get("height") or 0)
        if not h:
            return
        cur = by_height.get(h)
        if cur is None:
            v["_rank"] = rank
            by_height[h] = v
            return
        if rank < cur["_rank"]:
            alt_url = cur.get("url")
            alt_type = cur.get("type")
            v["_rank"] = rank
            by_height[h] = v
            cur = v
        else:
            alt_url = v.get("url")
            alt_type = v.get("type")
        if alt_url and not cur.get("alt_url"):
            cur["alt_url"] = alt_url
            cur["alt_type"] = alt_type

    for v in dood_variants:
        _offer(v, 0)
    for v in hls_variants:
        _offer(v, 1)
    for v in pd_variants:
        _offer(v, 2)

    if by_height:
        merged = []
        for v in by_height.values():
            v.pop("_rank", None)
            merged.append(v)
        variants = _dedup_by_height(merged)
        res_list = [
            {
                "height": int(v["height"]),
                "format_id": v["format_id"],
                "has_audio": True,
                "filesize": 0,
                "total_size": int(v.get("size") or 0),
            }
            for v in variants
        ]
        kinds = {v.get("type") for v in variants}
        if "doodstream_direct" in kinds and ("pixeldrain_ouo" in kinds or "mp4upload_ouo" in kinds):
            master_tag = "doodstream+pixeldrain_fallback"
        elif "doodstream_direct" in kinds:
            master_tag = "doodstream_fallback"
        elif "hls" in kinds and "pixeldrain_ouo" in kinds:
            master_tag = "hls+pixeldrain_fallback"
        elif "hls" in kinds:
            master_tag = "hls"
        elif "pixeldrain_ouo" in kinds:
            master_tag = "pixeldrain_fallback"
        else:
            master_tag = "mp4upload_fallback"
        log.info(
            "Nekopoi sources merged | master=%s heights=%s",
            master_tag, [(v["height"], v.get("type")) for v in variants],
        )
        probe = {
            "title": title,
            "thumbnail": post.get("thumbnail") or "",
            "master": master_tag,
            "variants": variants,
            "res_list": res_list,
        }
        cache_probe(raw_url, probe)
        return probe

    if primary_err is None:
        hosts = _list_download_hosts(post.get("raw_html") or "")
        names = ", ".join(hosts) if hosts else "none detected"
        raise RuntimeError(
            "This Nekopoi post has no supported download path: "
            "no HLS (streampoi), DoodStream (playmogo), Pixeldrain, or Mp4Upload. "
            f"Available hosts in post ({names}) are not supported by the scraper."
        )
    raise primary_err or last_err or RuntimeError("Failed to fetch Nekopoi stream from all embeds & download links")


def _pick_variant(variants: list, format_id: str | None) -> dict:
    if format_id:
        for v in variants:
            if str(v.get("format_id")) == str(format_id):
                return v
        for v in variants:
            if str(int(v.get("height") or 0)) == str(format_id):
                return v
    return max(variants, key=lambda v: (int(v.get("height") or 0), int(v.get("bandwidth") or 0)))


async def nekopoi_download(
    raw_url,
    fmt_key,
    bot,
    chat_id,
    status_msg_id,
    format_id: str | None = None,
    has_audio: bool = False,
    metadata_ready: bool = False,
    known_size: int = 0,
    engine: str | None = None,
):
    del has_audio, known_size, engine
    ext = _import_extractor()
    work_dir = os.path.join(TMP_DIR, f"neko_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    final_path = None
    try:
        probe = peek_probe(raw_url)
        if not probe:
            if not metadata_ready:
                await ext.safe_edit_status(bot, chat_id, status_msg_id, "<b>Scraping Nekopoi...</b>")
            probe = await asyncio.to_thread(probe_nekopoi, raw_url)

        variants = probe.get("variants") or []
        if not variants:
            raise RuntimeError("No stream variants on Nekopoi page")
        chosen = _pick_variant(variants, format_id)
        title = sanitize_filename(probe.get("title") or "Nekopoi", 100)
        label = f"{int(chosen.get('height') or 0)}p"
        log.info(
            "Nekopoi download start | title=%r label=%s fmt=%s format_id=%s variants=%s src=%s",
            title, label, fmt_key, format_id, [v.get("height") for v in variants], chosen.get("type"),
        )

        candidates: list[dict] = []
        for v in [chosen, *[x for x in variants if x is not chosen and int(x.get("height") or 0) == int(chosen.get("height") or 0)]]:
            if v and v not in candidates:
                candidates.append(v)
        alt = chosen.get("alt_url")
        if alt:
            candidates.append({
                "type": chosen.get("alt_type") or "pixeldrain_ouo",
                "url": alt,
                "height": chosen.get("height"),
                "size": 0,
            })

        final_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_nekopoi.mp4")
        last_err: Exception | None = None
        for cand in candidates:
            ctype = cand.get("type") or ""
            log.info("Nekopoi attempt src=%s url=%s", ctype, cand.get("url"))
            try:
                if ctype == "doodstream_direct":
                    headers = {
                        "User-Agent": ext.UA,
                        "Referer": cand.get("referer") or "",
                    }
                    await ext.download_direct_mp4(
                        cand["url"], final_path, bot, chat_id, status_msg_id,
                        title, label, headers, total_size=int(cand.get("size") or 0),
                    )
                elif ctype in ("pixeldrain_ouo", "mp4upload_ouo"):
                    direct_url = ""
                    src_headers: dict = {"User-Agent": ext.UA}
                    src_size = 0
                    bypassed = await asyncio.to_thread(ext.resolve_shortener, cand["url"])
                    if bypassed and "pixeldrain.com/u/" in bypassed:
                        pid = bypassed.split("/u/")[-1].split("?")[0].split("/")[0]
                        direct_url = f"https://pixeldrain.com/api/file/{pid}"
                        src_headers = {"User-Agent": ext.UA}
                        src_size = await asyncio.to_thread(ext._probe_direct_size, direct_url, {"User-Agent": ext.UA})
                        log.info("Nekopoi Pixeldrain ready | pid=%s size=%s", pid, src_size)
                    elif bypassed and "mp4upload.com" in bypassed:
                        cdn, _sz = await asyncio.to_thread(ext.mp4upload_to_direct_cdn, bypassed)
                        if cdn:
                            direct_url = cdn
                            src_headers = {"User-Agent": ext.UA, "Referer": "https://www.mp4upload.com/"}
                            src_size = int(_sz or 0)
                            log.info("Nekopoi Mp4Upload ready | cdn=%s size=%s", cdn, _sz)
                    if not direct_url:
                        raise RuntimeError(f"Shortener did not resolve to a file host ({bypassed})")
                    await ext.download_direct_mp4(
                        direct_url, final_path, bot, chat_id, status_msg_id,
                        title, label, src_headers, total_size=src_size,
                    )
                else:
                    await ext.download_variant(
                        cand["url"], final_path, bot, chat_id, status_msg_id, title, label,
                    )
                size = os.path.getsize(final_path)
                if size > MAX_TG_SIZE:
                    raise FileSizeLimitExceeded("Video exceeds 2GB limit. Download canceled.")
                log.info(
                    "Nekopoi download success | title=%r label=%s src=%s size=%.2fMB",
                    title, label, ctype, size / 1024 / 1024,
                )
                return {"path": final_path, "title": title, "remux_done": True}
            except FileSizeLimitExceeded:
                raise
            except Exception as e:
                last_err = e
                log.warning("Nekopoi attempt failed | src=%s err=%r", ctype, e)
                if os.path.exists(final_path):
                    try:
                        os.remove(final_path)
                    except OSError:
                        pass
                continue

        raise last_err or RuntimeError("Failed to download Nekopoi video from every source")
    except FileSizeLimitExceeded:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    except Exception:
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
