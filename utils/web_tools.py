import asyncio
import inspect
import json
import logging

import aiohttp

from utils import config as app_config
from utils.http import get_http_session

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Cari informasi nyata di internet (Google). WAJIB panggil tool ini "
            "sebelum menjawab kalau kamu tidak tahu, ragu, atau pertanyaannya "
            "menyangkut berita, peristiwa, tokoh, politik, harga, skor, cuaca, "
            "jadwal, produk baru, atau fakta apa pun yang bisa berubah setelah "
            "pengetahuanmu. Jangan menebak dan jangan melabeli sesuatu sebagai "
            "hoax tanpa mengecek dulu lewat tool ini."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Kata kunci pencarian yang ringkas dan spesifik.",
                }
            },
            "required": ["query"],
        },
    },
}

SEARCH_TOOL_HINT = (
    "[Tool Pencarian Internet]\n"
    "Kamu punya tool `web_search`. Kalau kamu tidak tahu jawabannya, tidak yakin, "
    "atau pertanyaannya soal berita, tokoh, politik, kejadian terkini, harga, skor, "
    "cuaca, jadwal, atau fakta apa pun yang bisa berubah: panggil `web_search` dulu "
    "sebelum menjawab. Jangan menebak, jangan mengarang, dan jangan menganggap "
    "sesuatu hoax tanpa mengeceknya. Setelah hasilnya ada, jawab langsung seolah "
    "kamu memang tahu — jangan bilang 'berdasarkan hasil pencarian' atau "
    "'aku baru googling'."
)


class _Reset:
    __slots__ = ()


RESET = _Reset()


def search_backend_available() -> bool:
    return bool(app_config.FIRECRAWL_API_KEY) or bool(app_config.JINA_API_KEY)


async def run_web_search(query: str, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return ""

    log.info("Web search start | q=%s", query)

    engine = "firecrawl"
    try:
        from utils.search_settings import get_search_engine

        engine = get_search_engine()
    except Exception:
        pass

    if search_backend_available():
        try:
            if engine == "jina":
                from utils.jina import web_search as _search
            else:
                from utils.firecrawl import web_search as _search
            text = await _search(query, max_results=max_results)
            if text and text.strip():
                log.info("Web search done | q=%s engine=%s chars=%s", query, engine, len(text))
                return text.strip()
        except Exception as e:
            log.warning("Web search (%s) failed | q=%s err=%r", engine, query, e)

    try:
        from handlers.gsearch import google_search

        ok, results = await google_search(query, limit=max_results)
        if ok and results:
            text = "\n".join(
                f"- {r.get('title', '')}\n  {r.get('snippet', '')}\n  Sumber: {r.get('link', '')}"
                for r in results
            )
            log.info("Web search done | q=%s engine=google results=%s", query, len(results))
            return text
    except Exception as e:
        log.warning("Google search fallback failed | q=%s err=%r", query, e)

    log.warning("Web search empty | q=%s", query)
    return ""


def _call_name(call: dict) -> str:
    fn = call.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(call.get("name") or "web_search")


def _call_arguments(call: dict) -> str:
    fn = call.get("function")
    if isinstance(fn, dict):
        return str(fn.get("arguments") or "{}")
    return str(call.get("arguments") or "{}")


def _call_query(call: dict) -> str:
    try:
        args = json.loads(_call_arguments(call) or "{}")
    except Exception:
        return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get("query") or args.get("q") or "").strip()


def _normalize_calls(raw: list[dict]) -> list[dict]:
    out = []
    for idx, call in enumerate(raw or []):
        out.append(
            {
                "id": call.get("id") or f"call_{idx}",
                "type": "function",
                "function": {
                    "name": _call_name(call),
                    "arguments": _call_arguments(call),
                },
            }
        )
    return out


async def _execute_one(call: dict) -> dict:
    query = _call_query(call)
    try:
        result = await run_web_search(query) if query else ""
    except Exception as e:
        log.warning("Tool execution failed | query=%s err=%r", query, e)
        result = ""
    if len(result) > 5000:
        result = result[:5000] + "\n..."
    return {
        "role": "tool",
        "tool_call_id": call["id"],
        "content": result or f"Tidak ada hasil pencarian untuk: {query}",
    }


async def _execute_calls(calls: list[dict]) -> list[dict]:
    if not calls:
        return []
    if len(calls) == 1:
        return [await _execute_one(calls[0])]
    return list(await asyncio.gather(*(_execute_one(c) for c in calls)))


async def _stream_events(session, endpoint, headers, payload, timeout, usage_out=None):
    async with session.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as r:
        if r.status != 200:
            err_msg = ""
            try:
                err_data = await r.json(content_type=None)
                if isinstance(err_data, dict):
                    err_msg = (err_data.get("error") or {}).get("message") or str(err_data)
            except Exception:
                err_msg = await r.text()
            raise RuntimeError(f"HTTP {r.status}: {err_msg or 'Request error'}")

        buffer = b""
        calls: dict[int, dict] = {}
        async for chunk in r.content.iter_any():
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                try:
                    obj = json.loads(data.decode("utf-8"))
                except Exception:
                    continue

                if usage_out is not None and obj.get("usage"):
                    u = obj["usage"]
                    try:
                        prompt = int(u.get("prompt_tokens") or 0)
                        completion = int(u.get("completion_tokens") or 0)
                        total = int(u.get("total_tokens") or (prompt + completion))
                    except (TypeError, ValueError):
                        prompt = completion = total = 0
                    if total > 0:
                        usage_out["prompt_tokens"] = prompt
                        usage_out["completion_tokens"] = completion
                        usage_out["total_tokens"] = total

                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield "text", content
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

        if calls:
            yield "tool_calls", [calls[i] for i in sorted(calls)]


_DSML_MARKERS = ("<｜｜DSML｜｜", "<||DSML||>", "｜｜DSML｜｜")
_DSML_HOLD = 12


class _DsmlFilter:
    __slots__ = ("buf", "blocked", "hit")

    def __init__(self):
        self.buf = ""
        self.blocked = False
        self.hit = False

    def feed(self, chunk: str) -> str:
        if self.blocked:
            return ""
        self.buf += chunk or ""
        for marker in _DSML_MARKERS:
            idx = self.buf.find(marker)
            if idx >= 0:
                self.blocked = True
                self.hit = True
                out = self.buf[:idx]
                self.buf = ""
                return out
        if len(self.buf) <= _DSML_HOLD:
            return ""
        out = self.buf[:-_DSML_HOLD]
        self.buf = self.buf[-_DSML_HOLD:]
        return out

    def flush(self) -> str:
        if self.blocked:
            self.buf = ""
            return ""
        out = self.buf
        self.buf = ""
        return out


def strip_dsml(text: str) -> str:
    value = str(text or "")
    for marker in _DSML_MARKERS:
        idx = value.find(marker)
        if idx >= 0:
            value = value[:idx]
    return value.strip()


async def stream_completion(
    messages: list[dict],
    *,
    endpoint: str,
    headers: dict,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    usage_out: dict | None = None,
    extra: dict | None = None,
    use_tools: bool = True,
    max_rounds: int = 2,
):
    if not search_backend_available():
        use_tools = False

    session = get_http_session()
    if inspect.isawaitable(session):
        session = await session

    current = list(messages)
    tool_rounds = max(1, max_rounds - 1) if use_tools else 0
    searched = False

    for round_idx in range(tool_rounds):
        payload = {
            "model": model,
            "messages": current,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "stream": True,
            "tools": [WEB_SEARCH_TOOL],
            "tool_choice": "auto",
        }
        if extra:
            payload.update(extra)

        round_usage: dict = {}
        text = ""
        pending = None
        gate = _DsmlFilter()
        async for kind, value in _stream_events(
            session, endpoint, headers, payload, timeout, round_usage
        ):
            if kind == "text":
                text += value
                safe = gate.feed(value)
                if safe:
                    yield safe
            else:
                pending = value

        tail = gate.flush()
        if tail:
            yield tail

        if usage_out is not None:
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if round_usage.get(key):
                    usage_out[key] = usage_out.get(key, 0) + int(round_usage[key])

        if pending:
            calls = _normalize_calls(pending)
            log.info(
                "Search tool called | model=%s round=%s queries=%s",
                model,
                round_idx + 1,
                [_call_query(c) for c in calls],
            )
            current.append(
                {"role": "assistant", "content": text or None, "tool_calls": calls}
            )
            current.extend(await _execute_calls(calls))
            searched = True
            yield RESET
            continue

        if gate.hit:
            log.warning(
                "Model emitted DSML text instead of tool calls | model=%s round=%s",
                model, round_idx + 1,
            )
            current.append(
                {
                    "role": "user",
                    "content": (
                        "Jangan menulis pemanggilan tool sebagai teks. "
                        "Kalau perlu mencari, panggil tool web_search yang asli; "
                        "kalau tidak perlu, jawab langsung ke user sekarang."
                    ),
                }
            )
            continue

        return

    if searched:
        current.append(
            {
                "role": "user",
                "content": "Sekarang jawab langsung ke user pakai hasil pencarian di atas.",
            }
        )
        yield RESET

    payload = {
        "model": model,
        "messages": current,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
        "stream": True,
    }
    if extra:
        payload.update(extra)
    if searched:
        payload["tool_choice"] = "none"

    gate = _DsmlFilter()
    async for kind, value in _stream_events(
        session, endpoint, headers, payload, timeout, usage_out
    ):
        if kind == "text":
            safe = gate.feed(value)
            if safe:
                yield safe
    tail = gate.flush()
    if tail:
        yield tail


async def complete_with_search(
    messages: list[dict],
    *,
    endpoint: str,
    headers: dict,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    usage_out: dict | None = None,
    extra: dict | None = None,
    use_tools: bool = True,
) -> str:
    parts: list[str] = []
    async for piece in stream_completion(
        messages,
        endpoint=endpoint,
        headers=headers,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        usage_out=usage_out,
        extra=extra,
        use_tools=use_tools,
    ):
        if piece is RESET:
            parts = []
            continue
        parts.append(piece)
    return "".join(parts).strip()
