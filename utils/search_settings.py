import json
import logging
import os

log = logging.getLogger(__name__)

SETTINGS_FILE = os.path.join("data", "search_settings.json")

_ENGINES = ("firecrawl", "jina")
_DEPTHS = ("fast", "content")


def _read_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("Failed to read search settings | err=%r", e)
        return {}


def _write_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, SETTINGS_FILE)


def get_search_engine() -> str:
    val = str(_read_settings().get("search_engine") or "").strip().lower()
    if val in _ENGINES:
        return val
    env = str(os.getenv("SEARCH_ENGINE", "firecrawl")).strip().lower()
    return env if env in _ENGINES else "firecrawl"


def set_search_engine(engine: str) -> None:
    engine = (engine or "").strip().lower()
    if engine not in _ENGINES:
        raise ValueError(f"unknown engine: {engine}")
    data = _read_settings()
    data["search_engine"] = engine
    _write_settings(data)


def get_search_depth() -> str:
    val = str(_read_settings().get("search_depth") or "").strip().lower()
    if val in _DEPTHS:
        return val
    env = str(os.getenv("SEARCH_DEPTH", "fast")).strip().lower()
    return env if env in _DEPTHS else "fast"


def set_search_depth(depth: str) -> None:
    depth = (depth or "").strip().lower()
    if depth not in _DEPTHS:
        raise ValueError(f"unknown depth: {depth}")
    data = _read_settings()
    data["search_depth"] = depth
    _write_settings(data)
