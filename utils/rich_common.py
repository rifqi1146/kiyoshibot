import json
import os
import aiohttp

def is_message_not_modified(error) -> bool:
    text = str(error or "").lower()
    return "not modified" in text

async def post_rich_multipart(bot, payload: dict, attach: list[tuple[str, str]]):
    if not attach:
        res = await bot.do_api_request("sendRichMessage", payload)
        if isinstance(res, dict) and res.get("error_code"):
            raise RuntimeError(res.get("description") or "sendRichMessage failed")
        return res

    from utils.http import get_http_session

    handles: list = []
    try:
        form = aiohttp.FormData()
        for key, val in payload.items():
            form.add_field(key, json.dumps(val) if isinstance(val, (dict, list)) else str(val))
        for field, fpath in attach:
            fh = open(fpath, "rb")
            handles.append(fh)
            form.add_field(
                field,
                fh,
                filename=os.path.basename(fpath),
                content_type="application/octet-stream",
            )
        session = await get_http_session()
        url = f"{bot.base_url}/sendRichMessage"
        upload_timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=1200)
        async with session.post(url, data=form, timeout=upload_timeout) as resp:
            data = await resp.json(content_type=None)
    finally:
        for fh in handles:
            try:
                fh.close()
            except Exception:
                pass
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "sendRichMessage failed")
    return data.get("result")
