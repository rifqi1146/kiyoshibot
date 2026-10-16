from . import parsers as P
from .fetch import api, country_order, eu_country, status_of

SORTS = {"relevance": 0, "likes": 1}
PUBLISHED = {"day": 1, "week": 7, "month": 30, "3months": 90, "6months": 180}


def _split_cursor(cursor):
    if not cursor:
        return 0, ""
    offset, _, search_id = str(cursor).partition(".")
    return int(offset), search_id


def _next_cursor(payload, search_id):
    if not payload.get("has_more"):
        return None
    offset = payload.get("cursor")
    if offset in (None, ""):
        return None
    search_id = search_id or _search_id(payload)
    return f"{offset}.{search_id}" if search_id else str(offset)


def _search_id(payload):
    for value in ((payload.get("log_pb") or {}).get("impr_id"), (payload.get("extra") or {}).get("logid"),
                  payload.get("rid")):
        if isinstance(value, str) and value:
            return value
    return ""


def _search(path, query, cursor, *, countries, offset_key="offset", extra=None):
    offset, search_id = _split_cursor(cursor)
    params = {"keyword": query, offset_key: offset, "from_page": "search"}
    if search_id:
        params["search_id"] = search_id
    params.update(extra or {})
    payload = api(path, params, countries=countries, boot=True,
                  referer="https://www.tiktok.com/search", label=f"search for {query!r}")
    return payload, _next_cursor(payload, search_id)


def search_videos(query, country=None, sort="relevance", published=None, cursor=None):
    extra = {"count": 20}
    if sort != "relevance" or published:
        extra.update({"is_filter_search": 1, "sort_type": SORTS[sort], "publish_time": PUBLISHED.get(published, 0)})
    payload, next_cursor = _search("/api/search/item/full/", query, cursor,
                                   countries=country_order(country, "us"), extra=extra)
    videos = P.videos(payload.get("item_list"))
    return {"query": query, "sort": sort, "published": published, "video_count": len(videos), "videos": videos,
            "next_cursor": next_cursor, "has_more": next_cursor is not None}


def search_users(query, cursor=None):
    payload, next_cursor = _search("/api/search/user/full/", query, cursor,
                                   countries=(eu_country(),), offset_key="cursor")
    users = P.users(payload.get("user_list"), P.search_user)
    return {"query": query, "user_count": len(users), "users": users,
            "next_cursor": next_cursor, "has_more": next_cursor is not None}


def search_top(query, country=None, cursor=None):
    payload, next_cursor = _search("/api/search/general/full/", query, cursor,
                                   countries=country_order(country, eu_country()))
    videos, users = P.top_results(payload.get("data"))
    return {"query": query, "user_count": len(users), "users": users, "video_count": len(videos), "videos": videos,
            "next_cursor": next_cursor, "has_more": next_cursor is not None}


def search_live(query, country=None, cursor=None):
    payload, next_cursor = _search("/api/search/live/full/", query, cursor,
                                   countries=country_order(country, eu_country()), extra={"count": 20})
    rooms = [room for room in (P.search_live_room(row) for row in payload.get("data") or []) if room]
    return {"query": query, "room_count": len(rooms), "rooms": rooms,
            "next_cursor": next_cursor, "has_more": next_cursor is not None}


def get_suggestions(query, country=None):
    payload = api("/api/search/general/preview/", {"keyword": query}, country=country, signed=False)
    suggestions = P.suggestions(payload.get("sug_list")) if status_of(payload) == 0 else []
    return {"query": query, "suggestion_count": len(suggestions), "suggestions": suggestions}
