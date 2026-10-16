from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

STORE_PATH = Path(os.getenv("CODEBUDDY_ACCOUNTS_PATH", "data/codebuddy_accounts.json"))

_LOCK = threading.RLock()
_CACHE: dict | None = None
_CACHE_MTIME = 0.0


def _empty() -> dict:
    return {"accounts": [], "cursor": 0}


def _read() -> dict:
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        try:
            mtime = STORE_PATH.stat().st_mtime
        except OSError:
            mtime = 0.0
        if _CACHE is not None and mtime == _CACHE_MTIME:
            return _CACHE
        data = _empty()
        if STORE_PATH.exists():
            try:
                raw = json.loads(STORE_PATH.read_text(encoding="utf-8") or "{}")
                if isinstance(raw, dict):
                    accounts = raw.get("accounts")
                    if isinstance(accounts, list):
                        data["accounts"] = [a for a in accounts if isinstance(a, dict)]
                    cursor = raw.get("cursor")
                    if isinstance(cursor, int) and cursor >= 0:
                        data["cursor"] = cursor
            except Exception as e:
                log.warning("CodeBuddy account store rusak | err=%r", e)
        _CACHE = data
        _CACHE_MTIME = mtime
        return data


def _write(data: dict) -> None:
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, STORE_PATH)
        _CACHE = data
        try:
            _CACHE_MTIME = STORE_PATH.stat().st_mtime
        except OSError:
            _CACHE_MTIME = 0.0


def decode_claims(token: str) -> dict:
    token = (token or "").strip()
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    pad = len(payload) % 4
    if pad:
        payload += "=" * (4 - pad)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def token_expiry(token: str) -> int:
    try:
        return int(decode_claims(token).get("exp") or 0)
    except Exception:
        return 0


def account_email(refresh_token: str, access_token: str = "") -> str:
    for tok in (access_token, refresh_token):
        claims = decode_claims(tok)
        email = str(claims.get("email") or "").strip()
        if email:
            return email
        username = str(claims.get("preferred_username") or "").strip()
        if username:
            return username
    return ""


def resolve_email(key: str, access_token: str) -> bool:
    at = (access_token or "").strip()
    email = account_email("", at)
    if not email:
        return False
    with _LOCK:
        data = dict(_read())
        accounts = list(data.get("accounts") or [])
        for idx, acc in enumerate(accounts):
            if acc.get("key") == key:
                accounts[idx] = {**acc, "email": email, "label": email, "access_token": at,
                                 "access_exp": token_expiry(at)}
                data["accounts"] = accounts
                _write(data)
                return True
    return False


def account_label(refresh_token: str, access_token: str = "") -> str:
    email = account_email(refresh_token, access_token)
    if email:
        return email
    sub = ""
    for tok in (access_token, refresh_token):
        claims = decode_claims(tok)
        sub = str(claims.get("sub") or "").strip()
        if sub:
            break
    return sub or "unknown"


def _account_key(refresh_token: str, access_token: str) -> str:
    for tok in (access_token, refresh_token):
        claims = decode_claims(tok)
        sub = str(claims.get("sub") or "").strip()
        if sub:
            return sub
    return (access_token or refresh_token or "")[:40]


def _normalise_entry(entry: dict) -> dict:
    rt = str(entry.get("refresh_token") or "").strip()
    at = str(entry.get("access_token") or "").strip()
    if not rt and not at:
        return {}
    return {
        "key": entry.get("key") or _account_key(rt, at),
        "label": entry.get("label") or account_label(rt, at),
        "email": entry.get("email") or account_email(rt, at),
        "refresh_token": rt,
        "access_token": at,
        "refresh_exp": entry.get("refresh_exp") or token_expiry(rt),
        "access_exp": entry.get("access_exp") or token_expiry(at),
        "added_at": entry.get("added_at") or int(time.time()),
        "disabled": bool(entry.get("disabled")),
    }


def list_accounts(include_disabled: bool = True) -> list[dict]:
    accounts = _read().get("accounts") or []
    if include_disabled:
        return list(accounts)
    return [a for a in accounts if not a.get("disabled")]


def add_account(refresh_token: str, access_token: str = "") -> tuple[bool, str, dict]:
    rt = (refresh_token or "").strip()
    at = (access_token or "").strip()
    if not rt and not at:
        return False, "Token kosong.", {}

    entry = _normalise_entry({"refresh_token": rt, "access_token": at})
    if not entry:
        return False, "Token tidak valid.", {}

    with _LOCK:
        data = dict(_read())
        accounts = list(data.get("accounts") or [])
        for idx, acc in enumerate(accounts):
            if acc.get("key") == entry["key"]:
                merged = dict(acc)
                if rt:
                    merged["refresh_token"] = rt
                    merged["refresh_exp"] = entry["refresh_exp"]
                if at:
                    merged["access_token"] = at
                    merged["access_exp"] = entry["access_exp"]
                email = entry.get("email") or merged.get("email")
                if email:
                    merged["email"] = email
                    merged["label"] = email
                merged["disabled"] = False
                accounts[idx] = merged
                data["accounts"] = accounts
                _write(data)
                return True, "Akun sudah ada, token diperbarui.", merged

        accounts.append(entry)
        data["accounts"] = accounts
        _write(data)
        return True, "Akun ditambahkan.", entry


def remove_account(match: str) -> tuple[bool, str]:
    match = (match or "").strip().lower()
    if not match:
        return False, "Sebutkan email, sub, atau nomor urut akun."
    with _LOCK:
        data = dict(_read())
        accounts = list(data.get("accounts") or [])
        if not accounts:
            return False, "Belum ada akun tersimpan."

        if match.isdigit():
            idx = int(match) - 1
            if 0 <= idx < len(accounts):
                removed = accounts.pop(idx)
                data["accounts"] = accounts
                data["cursor"] = 0
                _write(data)
                return True, f"Akun dihapus: {removed.get('label') or removed.get('key')}"

        for idx, acc in enumerate(accounts):
            haystack = " ".join(
                str(acc.get(f) or "").lower()
                for f in ("label", "email", "key")
            )
            if match in haystack:
                removed = accounts.pop(idx)
                data["accounts"] = accounts
                data["cursor"] = 0
                _write(data)
                return True, f"Akun dihapus: {removed.get('label') or removed.get('key')}"

    return False, f"Tidak ada akun yang cocok dengan '{match}'."


def clear_accounts() -> int:
    with _LOCK:
        data = _read()
        count = len(data.get("accounts") or [])
        _write(_empty())
        return count


def set_disabled(match: str, disabled: bool) -> tuple[bool, str]:
    match = (match or "").strip().lower()
    with _LOCK:
        data = dict(_read())
        accounts = list(data.get("accounts") or [])
        if match.isdigit():
            idx = int(match) - 1
            if 0 <= idx < len(accounts):
                accounts[idx] = {**accounts[idx], "disabled": disabled}
                data["accounts"] = accounts
                _write(data)
                return True, accounts[idx].get("label") or accounts[idx].get("key")
        for idx, acc in enumerate(accounts):
            haystack = " ".join(str(acc.get(f) or "").lower() for f in ("label", "email", "key"))
            if match in haystack:
                accounts[idx] = {**acc, "disabled": disabled}
                data["accounts"] = accounts
                _write(data)
                return True, acc.get("label") or acc.get("key")
    return False, "Akun tidak ditemukan."


def update_tokens(key: str, refresh_token: str = "", access_token: str = "", refresh_exp: int = 0) -> None:
    with _LOCK:
        data = dict(_read())
        accounts = list(data.get("accounts") or [])
        for idx, acc in enumerate(accounts):
            if acc.get("key") == key:
                merged = dict(acc)
                if refresh_token:
                    merged["refresh_token"] = refresh_token
                    merged["refresh_exp"] = refresh_exp or token_expiry(refresh_token)
                if access_token:
                    merged["access_token"] = access_token
                    merged["access_exp"] = token_expiry(access_token)
                    email = account_email("", access_token)
                    if email:
                        merged["email"] = email
                        merged["label"] = email
                accounts[idx] = merged
                data["accounts"] = accounts
                _write(data)
                return


def all_accounts() -> list[dict]:
    return list_accounts(include_disabled=False)


def repair_labels() -> int:
    with _LOCK:
        data = dict(_read())
        accounts = list(data.get("accounts") or [])
        changed = 0
        for idx, acc in enumerate(accounts):
            if acc.get("email"):
                continue
            email = account_email("", str(acc.get("access_token") or ""))
            if email:
                accounts[idx] = {**acc, "email": email, "label": email}
                changed += 1
        if changed:
            data["accounts"] = accounts
            _write(data)
        return changed


def rotate_cursor(total: int) -> int:
    if total <= 0:
        return 0
    with _LOCK:
        data = dict(_read())
        cursor = int(data.get("cursor") or 0) % total
        data["cursor"] = (cursor + 1) % total
        _write(data)
        return cursor


def peek_cursor() -> int:
    return int(_read().get("cursor") or 0)


def reset_cursor() -> None:
    with _LOCK:
        data = dict(_read())
        data["cursor"] = 0
        _write(data)


def summary() -> list[dict]:
    out = []
    now = int(time.time())
    for idx, acc in enumerate(all_accounts(), start=1):
        refresh_exp = int(acc.get("refresh_exp") or 0)
        access_exp = int(acc.get("access_exp") or 0)
        out.append({
            "index": idx,
            "label": acc.get("label") or acc.get("key") or "unknown",
            "email": acc.get("email") or "",
            "key": acc.get("key") or "",
            "has_refresh": bool(acc.get("refresh_token")),
            "has_access": bool(acc.get("access_token")),
            "refresh_expired": bool(refresh_exp and refresh_exp <= now),
            "refresh_exp": refresh_exp,
            "access_exp": access_exp,
            "disabled": bool(acc.get("disabled")),
        })
    return out
