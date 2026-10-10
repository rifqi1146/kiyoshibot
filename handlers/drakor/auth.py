"""Manajemen autentikasi / sesi login Drakor.id (drakorid.co).

FLOW AUTH
---------
1. Sesi disimpan di `data/drakorid_session.json` (gitignored, berisi cookies `jwtlogin`,
   `login`, `PHPSESSID`, `device_id`).
2. Sesi diperiksa berkala (`get_auth_cookies()`). Kalau belum ada atau basi
   (>24 jam), bot auto-login via `POST /login` menggunakan kredensial yang
   tersimpan (bisa dioverride lewat env `DRAKORID_EMAIL` / `DRAKORID_PASSWORD`).
3. Sesi yang terautentikasi membebaskan bot dari:
   - Batas guest ("hanya bisa download 1x sehari").
   - Warning login di halaman `/download-streaming/`.
   - Mengeluarkan direct link MP4 360p / 480p / 720p.
"""
import json
import logging
import os
import time
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .constants import BASE_URL, UA

log = logging.getLogger(__name__)

SESSION_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "drakorid_session.json")
)

# Kredensial HANYA dari .env (gitignored). Jangan pernah hardcode nilai di sini —
# repo ini publik. Kosong = jalan sebagai guest (batas 1x download/hari tetap ada).
DEFAULT_EMAIL = os.getenv("DRAKORID_EMAIL", "")
DEFAULT_PASSWORD = os.getenv("DRAKORID_PASSWORD", "")

_IN_MEMORY_COOKIES: dict = {}
_LAST_CHECK: float = 0.0


def _load_stored_session() -> dict:
    if not os.path.exists(SESSION_FILE):
        return {}
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Gagal membaca session Drakor.id | err=%r", e)
        return {}


def _save_session(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("Gagal menyimpan session Drakor.id | err=%r", e)


def login(email: str = "", password: str = "") -> dict:
    """Melakukan login HTTP ke Drakor.id dan mengembalikan dict cookies."""
    em = email or DEFAULT_EMAIL or os.getenv("DRAKORID_EMAIL", "")
    pw = password or DEFAULT_PASSWORD or os.getenv("DRAKORID_PASSWORD", "")
    if not em or not pw:
        log.debug("Drakor.id credentials not set; continuing as guest")
        return {}

    sess = curl_requests.Session(impersonate="chrome")
    try:
        r_page = sess.get(f"{BASE_URL}/login", headers={"User-Agent": UA}, timeout=15)
        soup = BeautifulSoup(r_page.text, "html.parser")
        csrf_el = soup.find("input", {"name": "csrf"})
        csrf = csrf_el.get("value") if csrf_el else ""

        resp = sess.post(
            f"{BASE_URL}/login",
            data={"email": em, "password": pw, "csrf": csrf},
            headers={"User-Agent": UA, "Referer": f"{BASE_URL}/login"},
            allow_redirects=False,
            timeout=15,
        )

        loc = resp.headers.get("location", "")
        cookies = dict(sess.cookies)
        is_ok = bool("jwtlogin" in cookies or "welcome" in loc.lower())

        if is_ok:
            log.info("Drakor.id login success | email=%s cookies=%d", em, len(cookies))
            _save_session({
                "email": em,
                "cookies": cookies,
                "updated_at": time.time(),
            })
            return cookies
        else:
            log.warning("Drakor.id login failed | email=%s status=%s loc=%s", em, resp.status_code, loc)
            return {}
    except Exception as e:
        log.warning("Drakor.id login exception | err=%r", e)
        return {}
    finally:
        try:
            sess.close()
        except Exception:
            pass


def get_auth_cookies() -> dict:
    """Mengembalikan dict cookies yang valid (cache memori -> file -> auto-login)."""
    global _IN_MEMORY_COOKIES, _LAST_CHECK
    now = time.time()

    if _IN_MEMORY_COOKIES and now - _LAST_CHECK < 3600:
        return _IN_MEMORY_COOKIES

    stored = _load_stored_session()
    stored_cookies = stored.get("cookies") or {}
    updated_at = float(stored.get("updated_at") or 0.0)

    # Re-login jika sesi sudah lebih dari 2 hari atau belum ada
    if stored_cookies and (now - updated_at < 172800) and "jwtlogin" in stored_cookies:
        _IN_MEMORY_COOKIES = stored_cookies
        _LAST_CHECK = now
        return _IN_MEMORY_COOKIES

    # Auto-login
    fresh_cookies = login()
    if fresh_cookies:
        _IN_MEMORY_COOKIES = fresh_cookies
        _LAST_CHECK = now
        return _IN_MEMORY_COOKIES

    return stored_cookies
