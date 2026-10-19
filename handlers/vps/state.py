import asyncio

_CHECK_LOCKS: dict[str, asyncio.Lock] = {}

_PROVISIONING: dict[str, bool] = {}

_PRICE_EDIT: dict[int, dict] = {}

_SCAN_CACHE: dict = {"result": None, "ts": 0.0}

_SCAN_LOCK = asyncio.Lock()


def get_lock(key: str) -> asyncio.Lock:
    lock = _CHECK_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CHECK_LOCKS[key] = lock
    return lock


def drop_lock(key: str):
    _CHECK_LOCKS.pop(key, None)


def mark_provisioning(short_id: str):
    _PROVISIONING[short_id] = True


def is_provisioning(short_id: str) -> bool:
    return _PROVISIONING.get(short_id, False)


def clear_provisioning(short_id: str):
    _PROVISIONING.pop(short_id, None)


def set_price_edit(user_id: int, tier: str, field: str):
    _PRICE_EDIT[int(user_id)] = {"tier": tier, "field": field}


def get_price_edit(user_id: int) -> dict | None:
    return _PRICE_EDIT.get(int(user_id))


def clear_price_edit(user_id: int):
    _PRICE_EDIT.pop(int(user_id), None)


def get_scan_lock() -> asyncio.Lock:
    return _SCAN_LOCK


def get_scan_cache() -> tuple:
    return _SCAN_CACHE.get("result"), float(_SCAN_CACHE.get("ts") or 0.0)


def set_scan_cache(result: dict, ts: float):
    _SCAN_CACHE["result"] = result
    _SCAN_CACHE["ts"] = float(ts)


def clear_scan_cache():
    _SCAN_CACHE["result"] = None
    _SCAN_CACHE["ts"] = 0.0


def prune():
    if len(_CHECK_LOCKS) > 512:
        for k in list(_CHECK_LOCKS.keys())[:256]:
            if not _CHECK_LOCKS[k].locked():
                _CHECK_LOCKS.pop(k, None)
    if len(_PRICE_EDIT) > 64:
        for k in list(_PRICE_EDIT.keys())[:32]:
            _PRICE_EDIT.pop(k, None)
