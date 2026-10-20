import logging

log = logging.getLogger(__name__)


def _db():
    from database import vps_db

    return vps_db


def all_accounts() -> list[dict]:
    try:
        return _db().list_accounts(include_disabled=False)
    except Exception as e:
        log.error("VPS accounts read failed | err=%r", e)
        return []


def all_accounts_with_disabled() -> list[dict]:
    try:
        return _db().list_accounts(include_disabled=True)
    except Exception as e:
        log.error("VPS accounts read failed | err=%r", e)
        return []


def account_names() -> list[str]:
    return [a["name"] for a in all_accounts()]


def get_account(name: str | None) -> dict | None:
    key = (name or "").strip().lower()
    if not key:
        return None
    try:
        return _db().get_account_row(key)
    except Exception as e:
        log.error("VPS account read failed | name=%s err=%r", key, e)
        return None


def account_label(name: str | None) -> str:
    a = get_account(name)
    if a:
        return a.get("label") or a["name"]
    return str(name or "")


def account_creds(name: str | None) -> dict:
    a = get_account(name)
    if not a:
        return {}
    key = a.get("access_key_id") or ""
    secret = a.get("secret_access_key") or ""
    if not key or not secret:
        return {}
    creds = {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret}
    if a.get("session_token"):
        creds["AWS_SESSION_TOKEN"] = a["session_token"]
    return creds


def regions_for_account(name: str) -> list[str]:
    a = get_account(name)
    if not a:
        return []
    return list(a.get("regions") or [])


DEFAULT_ACCOUNT_NAME = "default"
DEFAULT_ACCOUNT_LABEL = "Default profile"


def base_account_regions() -> list[str]:
    return list(_BASE_REGIONS_CACHE.get("regions") or [])


def set_base_account_regions(regions: list[str]) -> None:
    _BASE_REGIONS_CACHE["regions"] = [str(r).strip().lower() for r in regions if str(r).strip()]


_BASE_REGIONS_CACHE: dict = {"regions": [], "enabled": True}


def base_account_enabled() -> bool:
    return bool(_BASE_REGIONS_CACHE.get("enabled", True))


def set_base_account_enabled(enabled: bool) -> None:
    _BASE_REGIONS_CACHE["enabled"] = bool(enabled)


def resolve_chain(region: str, available_regions: list[str] | None = None) -> list[str]:
    r = (region or "").strip().lower()
    if not r:
        return []

    account_list = all_accounts()
    by_name = {a["name"]: a for a in account_list}
    base_regions = base_account_regions() if base_account_enabled() else []
    if base_regions:
        by_name[DEFAULT_ACCOUNT_NAME] = {
            "name": DEFAULT_ACCOUNT_NAME,
            "label": DEFAULT_ACCOUNT_LABEL,
            "regions": base_regions,
            "priority": 900,
            "enabled": True,
            "allow_default_regions": False,
        }
    avail = set(available_regions) if available_regions is not None else None

    try:
        pref = _db().get_region_pref(r)
    except Exception:
        pref = []

    ordered: list[str] = []
    seen: set[str] = set()

    def _push(name: str) -> None:
        a = by_name.get(name)
        if not a or name in seen:
            return
        if a.get("regions") and r not in a["regions"]:
            return
        if avail is not None and not a.get("regions") and not a.get("allow_default_regions"):
            return
        seen.add(name)
        ordered.append(name)

    for name in pref:
        _push(name)

    for name in sorted(by_name, key=lambda n: (by_name[n].get("priority", 100), n)):
        _push(name)

    return ordered


def account_for_region(region: str) -> str | None:
    chain = resolve_chain(region)
    return chain[0] if chain else None


def add_account(
    name: str,
    access_key_id: str,
    secret_access_key: str,
    regions: list[str] | None = None,
    *,
    label: str = "",
    priority: int = 100,
    session_token: str = "",
    allow_default_regions: bool = False,
) -> str:
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("Account name required")
    _db().upsert_account(
        key,
        access_key_id,
        secret_access_key,
        list(regions or []),
        label=label or key,
        priority=priority,
        session_token=session_token,
        allow_default_regions=allow_default_regions,
    )
    return key


def remove_account(name: str) -> bool:
    return _db().delete_account(name)


def set_account_regions(name: str, regions: list[str]) -> bool:
    return _db().set_account_regions(name, regions)


def set_account_enabled(name: str, enabled: bool) -> bool:
    return _db().set_account_enabled(name, enabled)


def set_region_preference(region: str, chain: list[str]) -> None:
    if not (region or "").strip():
        raise ValueError("Region required")
    _db().set_region_pref(region, chain)


def mask_key(value: str) -> str:
    v = (value or "").strip()
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:4]}{'*' * (len(v) - 8)}{v[-4:]}"


def describe(include_disabled: bool = True) -> list[dict]:
    source = all_accounts_with_disabled() if include_disabled else all_accounts()
    out = []
    base = base_account_regions()
    if base:
        out.append(
            {
                "name": DEFAULT_ACCOUNT_NAME,
                "label": DEFAULT_ACCOUNT_LABEL,
                "priority": 900,
                "enabled": base_account_enabled(),
                "regions": list(base),
                "region_count": len(base),
                "allow_default_regions": False,
                "has_creds": True,
                "key_hint": "from ~/.aws/credentials",
                "is_base": True,
            }
        )
    for a in source:
        out.append(
            {
                "name": a["name"],
                "label": a.get("label") or a["name"],
                "priority": a.get("priority", 100),
                "enabled": bool(a.get("enabled", True)),
                "regions": list(a.get("regions") or []),
                "region_count": len(a.get("regions") or []),
                "allow_default_regions": a.get("allow_default_regions", False),
                "has_creds": bool(a.get("access_key_id") and a.get("secret_access_key")),
                "key_hint": mask_key(a.get("access_key_id") or ""),
            }
        )
    return out


def toggle_base_account() -> bool:
    new_state = not base_account_enabled()
    set_base_account_enabled(new_state)
    from database import vps_db

    vps_db.set_setting("base_account_enabled", "1" if new_state else "0")
    return new_state


def set_base_account_enabled_persisted(enabled: bool) -> None:
    set_base_account_enabled(enabled)
    from database import vps_db

    vps_db.set_setting("base_account_enabled", "1" if enabled else "0")


def load_base_state() -> None:
    from database import vps_db

    try:
        raw = vps_db.get_setting("base_account_enabled")
    except Exception:
        raw = None
    if raw is not None:
        set_base_account_enabled(str(raw).strip() != "0")
