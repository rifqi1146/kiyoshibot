"""Helper, cookies, debugging, dan utilitas internal TikTok downloader."""
import os
import re
import time
import uuid
import json
import base64
import hashlib
import asyncio
import logging
from telegram.error import RetryAfter

from handlers.dl.constants import TMP_DIR
from handlers.dl.progress import render_progress_text, edit_status
from utils.config import LOG_CHAT_ID

from .constants import (
    WEB_HEADERS,
    TIKTOK_COOKIES_PATH,
    TIKTOK_COOKIE_DOMAINS,
    DEBUG_TIKTOK_LOG,
    DEBUG_TIKTOK_DUMP,
    TIKTOK_PROGRESS,
    MODERN_SSR_RE,
)

log = logging.getLogger(__name__)

_TIKTOK_COOKIE_HEADER_CACHE = None


def _decode_tt_base64(val: str) -> bytes:
    padding = (4 - len(val) % 4) % 4
    return base64.b64decode(val + ("=" * padding))


def _solve_tt_challenge(html_text: str) -> str:
    wci_match = re.search(r'(?is)<[^>]+\bid="wci"[^>]*\bclass="([^"]*)"', html_text)
    cs_match = re.search(r'(?is)<[^>]+\bid="cs"[^>]*\bclass="([^"]*)"', html_text)
    rci_match = re.search(r'(?is)<[^>]+\bid="rci"[^>]*\bclass="([^"]*)"', html_text)
    rs_match = re.search(r'(?is)<[^>]+\bid="rs"[^>]*\bclass="([^"]*)"', html_text)

    if not wci_match or not cs_match:
        return ""

    chal_name = wci_match.group(1).strip()
    chal_enc = cs_match.group(1).strip()

    try:
        chal_bytes = _decode_tt_base64(chal_enc)
        chal_data = json.loads(chal_bytes)

        v = chal_data.get("v", {})
        base_val = _decode_tt_base64(v.get("a", ""))
        expected_digest = _decode_tt_base64(v.get("c", ""))

        solution = ""
        for i in range(1_000_000 + 1):
            candidate = base_val + str(i).encode('utf-8')
            if hashlib.sha256(candidate).digest() == expected_digest:
                solution = str(i)
                break

        if not solution:
            return ""

        chal_data["d"] = base64.b64encode(solution.encode('utf-8')).decode('utf-8')
        chal_cookie_val = base64.b64encode(
            json.dumps(chal_data, separators=(',', ':')).encode('utf-8')
        ).decode('utf-8')

        cookies = [f"{chal_name}={chal_cookie_val}"]

        if rci_match and rs_match:
            rci = rci_match.group(1).strip()
            rs = rs_match.group(1).strip()
            if rci:
                cookies.append(f"{rci}={rs}")

        return "; ".join(cookies)
    except Exception as e:
        log.warning("Failed to solve TikTok challenge | err=%r", e)
        return ""


def _ttdbg(msg: str, *args):
    if DEBUG_TIKTOK_LOG:
        log.warning("TTDBG | " + msg, *args)


def _safe_remove_file(path: str | None, label: str):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("TikTok temp deleted | label=%s file=%s", label, os.path.basename(path))
    except Exception as e:
        log.warning("Failed to delete TikTok temp | label=%s path=%s err=%r", label, path, e)


async def _kill_process(proc, label: str):
    if not proc or proc.returncode is not None:
        return
    try:
        proc.kill()
        await proc.wait()
        log.warning("%s process killed", label)
    except ProcessLookupError:
        return
    except Exception as e:
        log.warning("Failed to kill %s process | err=%r", label, e)


def _write_debug_file(prefix: str, content: str | bytes, ext: str = "txt") -> str:
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        path = os.path.join(TMP_DIR, f"{prefix}_{uuid.uuid4().hex}.{ext}")
        if isinstance(content, (bytes, bytearray)):
            with open(path, "wb") as f:
                f.write(content)
        else:
            with open(path, "w", encoding="utf-8", errors="ignore") as f:
                f.write(content)
        _ttdbg("debug file written | path=%s", path)
        return path
    except Exception as e:
        _ttdbg("debug file write failed | prefix=%s err=%r", prefix, e)
        return ""


def _truncate_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return "." * limit
    return text[:limit - 3].rstrip() + "..."


def _load_tiktok_cookie_header(path: str) -> str:
    global _TIKTOK_COOKIE_HEADER_CACHE
    if _TIKTOK_COOKIE_HEADER_CACHE is not None:
        return _TIKTOK_COOKIE_HEADER_CACHE
    if not path or not os.path.exists(path):
        _ttdbg("tiktok cookie file not found | path=%s", path)
        _TIKTOK_COOKIE_HEADER_CACHE = ""
        return _TIKTOK_COOKIE_HEADER_CACHE
    pairs = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain = (parts[0] or "").strip().lower()
                    name = (parts[5] or "").strip()
                    value = (parts[6] or "").strip()
                    if name and any(d in domain for d in TIKTOK_COOKIE_DOMAINS):
                        pairs.append(f"{name}={value}")
                    continue
                if "=" in line and "\t" not in line and not line.lower().startswith(("http://", "https://")):
                    name, value = line.split("=", 1)
                    name = name.strip()
                    value = value.strip()
                    if name:
                        pairs.append(f"{name}={value}")
        _TIKTOK_COOKIE_HEADER_CACHE = "; ".join(pairs)
        _ttdbg("tiktok cookie loaded | path=%s pairs=%s", path, len(pairs))
        return _TIKTOK_COOKIE_HEADER_CACHE
    except Exception as e:
        _ttdbg("tiktok cookie load failed | path=%s err=%r", path, e)
        _TIKTOK_COOKIE_HEADER_CACHE = ""
        return _TIKTOK_COOKIE_HEADER_CACHE


def _build_tiktok_headers(referer: str | None = None, extra_cookie: str | None = None) -> dict:
    headers = dict(WEB_HEADERS)
    if referer:
        headers["Referer"] = referer
    cookie_parts = []
    file_cookie = _load_tiktok_cookie_header(TIKTOK_COOKIES_PATH)
    if file_cookie:
        cookie_parts.append(file_cookie)
    if extra_cookie:
        cookie_parts.append(extra_cookie)
    if cookie_parts:
        headers["Cookie"] = "; ".join(x for x in cookie_parts if x)
    return headers


def _merge_cookie_headers(*cookie_values: str) -> str:
    jar = {}
    for cookie_value in cookie_values:
        text = str(cookie_value or "").strip()
        if not text:
            continue
        for part in text.split(";"):
            kv = part.strip()
            if not kv or "=" not in kv:
                continue
            name, value = kv.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name:
                jar[name] = value
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _cookie_header(cookies: list[dict] | None) -> str:
    if not cookies:
        return ""
    parts = []
    for c in cookies:
        name = str((c or {}).get("name") or "").strip()
        value = str((c or {}).get("value") or "").strip()
        if name:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _cookies_from_header(cookie_header: str) -> list[dict]:
    out = []
    for part in str(cookie_header or "").split(";"):
        kv = part.strip()
        if not kv or "=" not in kv:
            continue
        name, value = kv.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            out.append({"name": name, "value": value})
    return out


def _purge_tiktok_session_cookies(session):
    """Hapus cookie TikTok dari cookie jar sesi bersama.

    Cookie sesi (tt_chain_token, msToken, dll) membuat TikTok mengarahkan
    permintaan media ke CDN `webapp-prime` yang menolak dengan HTTP 403.
    Unduhan video/audio harus memakai jar bersih agar diarahkan ke CDN
    `web-newkey` yang dapat diakses.
    """
    try:
        jar = getattr(session, "cookie_jar", None)
        if jar is None:
            return
        for domain in ("tiktok.com", "www.tiktok.com", ".tiktok.com", "tiktokv.com", ".tiktokv.com"):
            try:
                jar.clear_domain(domain)
            except Exception:
                pass
    except Exception as e:
        _ttdbg("purge tiktok cookies failed | err=%r", e)


def _int_meta(*values) -> int:
    for value in values:
        try:
            if value is None:
                continue
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0


def _duration_meta(*values) -> int:
    duration = _int_meta(*values)
    if duration > 3600:
        return max(int(round(duration / 1000)), 0)
    return max(duration, 0)


def _prioritize_video_urls(urls: list[str]) -> list[str]:
    """Urutkan URL video: play/CDN biasanya bisa diakses; CDN `webapp-prime`
    sering menolak (403) ketika cookie sesi TikTok ikut terkirim."""
    play = [u for u in urls if "aweme/v1/play" in u]
    cdn_ok = [u for u in urls if "aweme/v1/play" not in u and "tiktokcdn.com" in u]
    others = [u for u in urls if u not in play and u not in cdn_ok and "webapp-prime" not in u]
    prime = [u for u in urls if "webapp-prime" in u]
    return play + cdn_ok + others + prime


def _extract_debug_markers(html_text: str) -> dict:
    text = html_text or ""
    low = text.lower()
    return {
        "has_universal": "__UNIVERSAL_DATA_FOR_REHYDRATION__" in text,
        "has_sigi": "SIGI_STATE" in text,
        "has_next": "__NEXT_DATA__" in text,
        "has_item_module": "ItemModule" in text,
        "has_default_scope": "__DEFAULT_SCOPE__" in text,
        "has_video_path": "/video/" in text,
        "has_login": "login" in low,
        "has_verify": "verify" in low,
        "has_captcha": "captcha" in low,
        "has_robot": "robot" in low,
        "has_unusual": "unusual" in low,
        "has_modern_ssr": "__MODERN_SSR_DATA__" in text,
        "has_4d": "tiktok_4d_playback" in low,
    }


def _detect_weird_tiktok_page(html_text: str, final_url: str = "") -> str:
    text = html_text or ""
    low = text.lower()
    final = (final_url or "").lower()
    has_data = (
        "__UNIVERSAL_DATA_FOR_REHYDRATION__" in text
        or "SIGI_STATE" in text
        or "__NEXT_DATA__" in text
    )
    if "/player/v1/" in final:
        return "player_v1_url"
    if "/login" in final:
        return "login_url"
    if not has_data and ("captcha" in low or "verify" in low or "robot" in low or "unusual" in low):
        return "captcha_or_verify"
    if not has_data and ("tiktok_4d_playback" in low or "__MODERN_SSR_DATA__" in text):
        try:
            m = MODERN_SSR_RE.search(text)
            if m:
                ssr = json.loads(m.group(1))
                if isinstance(ssr, dict) and not (ssr.get("data") or {}):
                    return "modern_ssr_empty"
        except Exception:
            return "modern_ssr_shell"
        return "modern_shell"
    if not has_data and '<title data-react-helmet="true"></title>' in text:
        return "empty_shell"
    return ""


def _dump_script_tags(html_text: str) -> str:
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html_text or "", re.S | re.I)
    chunks = []
    for i, s in enumerate(scripts[:80], 1):
        s = (s or "").strip()
        if s:
            chunks.append(f"===== SCRIPT {i} =====\n{s[:6000]}\n")
    return "\n\n".join(chunks)


async def _send_debug_file(bot, path: str, caption: str):
    try:
        chat_id = int(LOG_CHAT_ID)
    except Exception:
        _ttdbg("invalid LOG_CHAT_ID | value=%r", LOG_CHAT_ID)
        return
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                caption=_truncate_text(caption, 1024),
                disable_notification=True,
            )
        _ttdbg("debug file sent | chat_id=%s path=%s", chat_id, path)
    except Exception as e:
        _ttdbg("failed sending debug file | chat_id=%s path=%s err=%r", chat_id, path, e)


async def _dump_tiktok_debug(
    bot,
    label: str,
    request_url: str,
    final_url: str,
    status: int,
    headers: dict,
    html_text: str,
    extra: dict | None = None,
):
    if not DEBUG_TIKTOK_DUMP:
        return
    markers = _extract_debug_markers(html_text)
    meta = {
        "label": label,
        "request_url": request_url,
        "final_url": final_url,
        "status": status,
        "headers": dict(headers or {}),
        "markers": markers,
        "extra": extra or {},
        "body_preview": (html_text or "")[:5000],
    }
    meta_path = _write_debug_file(f"tiktok_{label}_meta", json.dumps(meta, ensure_ascii=False, indent=2), "json")
    html_path = _write_debug_file(f"tiktok_{label}_body", html_text or "", "html")
    scripts_path = _write_debug_file(f"tiktok_{label}_scripts", _dump_script_tags(html_text or "") or "no script tags", "txt")
    await _send_debug_file(bot, meta_path, f"[TTDBG] {label} meta")
    await _send_debug_file(bot, html_path, f"[TTDBG] {label} html")
    await _send_debug_file(bot, scripts_path, f"[TTDBG] {label} scripts")
    _ttdbg("dump saved | label=%s status=%s final=%s markers=%s", label, status, final_url, markers)


async def _safe_edit_progress(
    bot,
    chat_id,
    status_msg_id,
    title: str,
    downloaded: int,
    total: int = 0,
    speed_bps: float = 0.0,
    eta_seconds: float | None = None,
):
    if not TIKTOK_PROGRESS:
        return
    if not status_msg_id:
        return
    text = render_progress_text(title, downloaded=downloaded, total=total, speed_bps=speed_bps, eta_seconds=eta_seconds)
    await edit_status(bot, chat_id, status_msg_id, text, label="TikTok")


async def _safe_edit_status(bot, chat_id, status_msg_id, text: str, min_interval: float = 1.2):
    if not status_msg_id:
        return
    cache = getattr(bot, "_status_edit_cache", {})
    key = (chat_id, status_msg_id)
    now = time.monotonic()
    prev = cache.get(key) or {}
    if prev.get("text") == text:
        return
    if now - prev.get("ts", 0) < min_interval:
        return
    for _ in range(2):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            cache[key] = {"text": text, "ts": time.monotonic()}
            setattr(bot, "_status_edit_cache", cache)
            return
        except RetryAfter as e:
            wait_time = max(int(getattr(e, "retry_after", 1)), 1)
            await asyncio.sleep(wait_time)
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return
            log.warning("Failed to edit status | chat_id=%s message_id=%s err=%s", chat_id, status_msg_id, e)
            return
