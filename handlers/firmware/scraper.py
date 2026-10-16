import logging
import re
import time
from curl_cffi import requests as curl_requests

from .constants import (
    BASE_URL,
    DETAIL_TTL,
    DEVICES_TTL,
    DEVICES_URL,
    HTTP_TIMEOUT,
    MAX_DEVICE_MATCHES,
    MAX_RESULTS,
    UA,
)
from .state import DEVICES_CACHE, MIRRORS_CACHE

log = logging.getLogger(__name__)

_session = None

REGION_HINTS = (
    "Indonesia",
    "China",
    "India",
    "Russia",
    "EEA",
    "Europe",
    "Taiwan",
    "Turkey",
    "Japan",
    "South Korea",
    "Singapore",
    "Global",
)

_BTN_RE = re.compile(
    r"window\.open\('(?P<url>[^']+)',\s*'_blank'\);\">(?P<body>.*?)</button>",
    re.S,
)
_TYPE_RE = re.compile(r"<h5><b>Type:\s*</b>\s*(?P<type>[A-Za-z]+)</h5>", re.S)


def _http():
    global _session
    if _session is None:
        _session = curl_requests.Session(impersonate="chrome")
    return _session


def _get(url: str) -> str:
    try:
        r = _http().get(url, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log.warning("HTTP %s untuk %s", r.status_code, url)
            return ""
        return r.text
    except Exception as e:
        log.warning("HTTP error %s | err=%r", url, e)
        return ""


def _region_from(name: str) -> str:
    for reg in REGION_HINTS:
        if reg.lower() in (name or "").lower():
            return "Europe" if reg in ("EEA", "Europe") else reg
    return "Global"


def fetch_devices_map(force: bool = False) -> dict[str, str]:
    now = time.time()
    cached = DEVICES_CACHE.get("map")
    if not force and cached and (now - DEVICES_CACHE.get("ts", 0) < DEVICES_TTL):
        return cached

    text = _get(DEVICES_URL)
    if not text:
        return cached or {}

    devs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        codename, name = line.split(":", 1)
        c = codename.strip().lower()
        n = name.strip().strip("'\"")
        if c and n:
            devs[c] = n

    if devs:
        DEVICES_CACHE["ts"] = now
        DEVICES_CACHE["map"] = devs
        DEVICES_CACHE["lower_map"] = {c: n.lower() for c, n in devs.items()}
        log.info("Cached %d Xiaomi devices from firmware_devices.yml", len(devs))

    return devs or cached or {}


def resolve_device(query: str) -> tuple[str, str, list[tuple[str, str]]]:
    devs = fetch_devices_map()
    if not devs:
        return "", "", []

    q = (query or "").strip().lower()
    if not q:
        return "", "", []

    if q in devs:
        return q, devs[q], []

    for c, n in devs.items():
        if n.lower() == q:
            return c, n, []

    tokens = [t for t in re.split(r"[\s/_\-]+", q) if t]
    matches: list[tuple[str, str]] = []
    for c, n in devs.items():
        n_lower = n.lower()
        if all(t in n_lower or t in c for t in tokens):
            matches.append((c, n))

    if len(matches) == 1:
        c, n = matches[0]
        return c, n, []

    if matches:
        return "", "", matches[:MAX_DEVICE_MATCHES]

    return "", "", []


def _parse_archive_table(html: str, section: str, seen: set) -> list[dict]:
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    if not body:
        return []

    items: list[dict] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S):
        cells = [
            re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        ]
        href = re.findall(r'href=["\']([^"\']+)["\']', tr)
        if len(cells) < 7 or not href:
            continue

        device_name, branch, rom_type, version, android, size, date = cells[:7]
        detail_path = href[0]

        key = (version, rom_type, detail_path)
        if key in seen:
            continue
        seen.add(key)

        items.append(
            {
                "device_name": device_name,
                "region": _region_from(device_name),
                "branch": branch,
                "type": rom_type,
                "version": version,
                "android": android,
                "size": size,
                "date": date,
                "detail_path": detail_path,
                "section": section,
            }
        )
    return items


def fetch_rom_archive(codename: str) -> list[dict]:
    c = (codename or "").strip().lower()
    if not c:
        return []

    items: list[dict] = []
    seen: set = set()
    for section in ("hyperos", "miui"):
        html = _get(f"{BASE_URL}/archive/{section}/{c}/")
        if html:
            items.extend(_parse_archive_table(html, section, seen))

    items.sort(key=lambda x: x.get("date", ""), reverse=True)
    log.info("Firmware archive %s | total=%d", c, len(items))
    return items[:MAX_RESULTS]


def fetch_rom_mirrors(detail_path: str) -> dict:
    if not detail_path:
        return {}

    now = time.time()
    cached = MIRRORS_CACHE.get(detail_path)
    if cached and now - cached.get("ts", 0) < DETAIL_TTL:
        return cached.get("blocks", {})

    url = detail_path if detail_path.startswith("http") else f"{BASE_URL}{detail_path}"
    html = _get(url)
    if not html:
        return {}

    blocks: dict[str, dict] = {}
    type_iter = list(_TYPE_RE.finditer(html))
    for i, tm in enumerate(type_iter):
        start = tm.start()
        end = type_iter[i + 1].start() if i + 1 < len(type_iter) else len(html)
        chunk = html[start:end]

        rtype = tm.group("type").strip().lower()
        if rtype not in ("recovery", "fastboot"):
            continue

        size = re.search(r"<h5><b>Size:\s*</b>\s*([^<]+)</h5>", chunk)
        md5 = re.search(r'id="md5"[^>]*>([0-9a-fA-F]+)<', chunk)
        fname = re.search(r'id="filename"[^>]*>([^<]+)<', chunk)

        block = {
            "size": size.group(1).strip() if size else "-",
            "md5": md5.group(1) if md5 else "-",
            "filename": fname.group(1).strip() if fname else "-",
            "mirrors": [],
            "incremental": None,
        }

        for btn in _BTN_RE.finditer(chunk):
            burl = btn.group("url")
            label = re.sub(r"<[^>]+>", "", btn.group("body")).strip()
            host = burl.split("/")[2] if "//" in burl else "-"
            entry = {"url": burl, "host": host, "label": label}
            if label.lower().startswith("incremental"):
                block["incremental"] = entry
            else:
                block["mirrors"].append(entry)

        if block["mirrors"] or block["incremental"] or block["filename"] != "-":
            blocks[rtype] = block

    if blocks:
        MIRRORS_CACHE[detail_path] = {"ts": now, "blocks": blocks}

    return blocks
