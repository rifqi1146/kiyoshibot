#!/usr/bin/env python3
# language: Python 3.12, file: opencode_v1.py, target: Linux (stdlib only)
"""
OpenCode Free -> OpenAI-compatible proxy.

Menyajikan endpoint /v1/models dan /v1/chat/completions (stream & non-stream)
yang di-backing akun gratisan https://opencode.ai (Bearer public, tanpa API key).

Teknik pemanggilan upstream (dibaca dari 9router open-sse/executors/opencode.js):
  - header fingerprint wajib : User-Agent opencode/1.18.31, x-opencode-client,
    x-opencode-session (ses_..), x-opencode-request (msg_..), x-opencode-project
  - session stabil per klien : kuota free dihitung per sesi, sesi diganti tiap
    request bikin 429 FreeUsageLimitError -> 1 sesi dipakai ulang sampai TTL
  - request id deterministik : sha256(session + teks user terakhir), biar retry
    nggak dihitung turn baru
  - fingerprint quartet tools : bash/glob/grep/read (huruf kecil) selalu ikut
    di body -> tanpa ini upstream balas 403 FreeTierError; varian kapital dari
    klien di-rename ke lowercase dan dipulangkan di response
  - force stream : non-stream ditolak upstream -> selalu stream lalu di-aggregate

Lanes (model -> endpoint upstream):
  - chat      : POST /zen/v1/chat/completions   (default, hampir semua model)
  - responses : POST /zen/v1/responses          (muse-spark-*-contributor-free)
  - excluded  : jev-* (systemone)               -> 400, bukan lane chat

Pemakaian:
  python3 opencode_v1.py                 # 127.0.0.1:20130
  python3 opencode_v1.py --port 8080 --host 0.0.0.0
"""
import argparse
import json
import secrets
import ssl
import sys
import os

# Jangan biarkan folder backend/ membajak modul standard `http` milik Python!
# Kalau script di-exec sebagai `python backend/opencode_v1.py`, sys.path[0] adalah
# folder backend/, sehingga `import http.client` di urllib.request malah load
# backend/http/ (package stream proxy) -> circular import crash.
if sys.path and os.path.basename(sys.path[0]) == "backend":
    sys.path.pop(0)

import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://opencode.ai"
CHAT_URL = UPSTREAM + "/zen/v1/chat/completions"
RESPONSES_URL = UPSTREAM + "/zen/v1/responses"
MODELS_URL = UPSTREAM + "/zen/v1/models"
UA = "opencode/1.18.31"

FINGERPRINT_TOOLS = ("bash", "glob", "grep", "read")
FREE_EXPLICIT = {"big-pickle"}  # model gratis tanpa akhiran -free
EXCLUDE_PREFIX = ("jev-",)      # kind systemone, bukan lane chat

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SSL_CTX = ssl.create_default_context()

MODELS_CACHE = {"at": 0.0, "ids": []}
MODELS_TTL = 300.0
MODELS_LOCK = threading.Lock()


def now_ms():
    return int(time.time() * 1000)


def is_free_model(mid):
    return mid.endswith("-free") or mid in FREE_EXPLICIT


def lane_for(mid):
    if any(mid.startswith(p) for p in EXCLUDE_PREFIX):
        return "excluded"
    if mid.startswith("muse-spark-") and "contributor" in mid and mid.endswith("-free"):
        return "responses"
    return "chat"


# ── id generator: mirror 9router generateSessionId / deriveRequestId ──────────
_state = {"ts": 0, "counter": 0}
_state_lock = threading.Lock()


def _time_part(value):
    # 6 byte big-endian dari value (48 bit), dipakai buat segmen hex
    value &= (1 << 48) - 1
    return format(value, "012x")


def generate_session_id():
    ts = now_ms()
    with _state_lock:
        if ts != _state["ts"]:
            _state["ts"] = ts
            _state["counter"] = 0
        _state["counter"] += 1
        current = ts * 0x1000 + _state["counter"]
    # inverse kayak aslinya biar bentuk id identik dengan CLI asli
    value = (~current) & ((1 << 48) - 1)
    return "ses_" + _time_part(value) + "".join(secrets.choice(BASE62) for _ in range(14))


def derive_request_id(session_id, last_text):
    import hashlib
    digest = hashlib.sha256(
        ("opencode-req\0" + (session_id or "") + "\0" + (last_text or "")).encode()
    ).digest()
    return "msg_" + digest[:6].hex() + "".join(BASE62[b % 62] for b in digest[6:20])


# ── session store (stabil per klien, TTL biar kuota nggak kebakar) ────────────
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TTL = 6 * 3600
MAX_SESSIONS = 2000


def session_for(identity):
    now = time.time()
    with SESSIONS_LOCK:
        entry = SESSIONS.get(identity)
        if entry and now - entry["last"] < SESSION_TTL:
            entry["last"] = now
            return entry["sid"]
        sid = generate_session_id()
        if len(SESSIONS) >= MAX_SESSIONS:
            stale = [k for k, v in SESSIONS.items() if now - v["last"] >= SESSION_TTL]
            for k in stale[: len(stale) // 2 or 1]:
                SESSIONS.pop(k, None)
        SESSIONS[identity] = {"sid": sid, "last": now}
        return sid


# ── fingerprint quartet tools (chat lane) ─────────────────────────────────────
def chat_tool(name):
    return {"type": "function", "function": {
        "name": name,
        "description": "This tool is currently unavailable and must not be used.",
        "parameters": {"type": "object", "properties": {}},
    }}


def flat_tool(name):
    return {"type": "function", "name": name,
            "description": "This tool is currently unavailable and must not be used.",
            "parameters": {"type": "object", "properties": {}}}


def tool_name(tool):
    if not isinstance(tool, dict):
        return ""
    if isinstance(tool.get("name"), str) and tool["name"].strip():
        return tool["name"].strip()
    fn = tool.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"].strip()
    return ""


def apply_fingerprint_chat(body):
    """Mirror applyFingerprintTools(body, flat=false). Return rename map."""
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    had_client_tools = bool(tools)
    rename = {}
    seen = set()
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        name = tool_name(tool)
        lower = name.lower()
        if lower in FINGERPRINT_TOOLS:
            if lower in seen:
                continue
            seen.add(lower)
            if name != lower:
                rename[lower] = name
                tool = json.loads(json.dumps(tool))
                if isinstance(tool.get("function"), dict):
                    tool["function"]["name"] = lower
                elif "name" in tool:
                    tool["name"] = lower
            out.append(tool)
        else:
            out.append(tool)
    for name in FINGERPRINT_TOOLS:
        if name not in seen:
            out.append(chat_tool(name))
            seen.add(name)
    body["tools"] = out

    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        if isinstance(choice.get("name"), str):
            key = choice["name"].lower()
            if key in rename:
                choice["name"] = rename[key]
        elif isinstance(choice.get("function"), dict):
            key = choice["function"].get("name", "").lower()
            if key in rename:
                choice["function"]["name"] = rename[key]
    elif not choice:
        if not had_client_tools:
            body["tool_choice"] = "none"
    return rename


def apply_fingerprint_flat(body):
    """Mirror applyFingerprintTools(body, flat=true) untuk lane responses."""
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    seen = set()
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool_name(tool).lower()
        if name in FINGERPRINT_TOOLS:
            if name in seen:
                continue
            seen.add(name)
            tool = json.loads(json.dumps(tool))
            if "name" in tool:
                tool["name"] = name
            elif isinstance(tool.get("function"), dict):
                tool["function"]["name"] = name
        out.append(tool)
    for name in FINGERPRINT_TOOLS:
        if name not in seen:
            out.append(flat_tool(name))
            seen.add(name)
    body["tools"] = out
    if not body.get("tool_choice"):
        body["tool_choice"] = "auto"


def restore_tool_names(payload, rename):
    """Balikin nama tool ke ejaan asli klien (response chat)."""
    if not rename or not isinstance(payload, dict):
        return payload
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload
    for choice in choices:
        for holder in ("delta", "message"):
            node = choice.get(holder) if isinstance(choice, dict) else None
            if not isinstance(node, dict):
                continue
            calls = node.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function")
                    if isinstance(fn, dict) and fn.get("name") in rename:
                        fn["name"] = rename[fn["name"]]
                    if call.get("name") in rename:
                        call["name"] = rename[call["name"]]
    return payload


def last_user_text(body):
    arr = body.get("messages") or body.get("input") or []
    if not isinstance(arr, list):
        return ""
    for msg in reversed(arr):
        if not isinstance(msg, dict) or msg.get("role") not in (None, "user"):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[-600:]
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.append(part.get("text") or part.get("input_text") or "")
            text = " ".join(parts).strip()
            if text:
                return text[-600:]
    return ""


# ── chat -> responses conversion (lane responses) ────────────────────────────
def chat_to_responses(body):
    out = {"model": body["model"], "stream": True, "store": False}
    for key in ("temperature", "top_p", "stop", "seed"):
        if body.get(key) is not None:
            out[key] = body[key]
    max_cap = body.get("max_output_tokens", body.get("max_completion_tokens", body.get("max_tokens")))
    if isinstance(max_cap, (int, float)) and max_cap and max_cap > 0:
        out["max_output_tokens"] = int(max_cap)
    reasoning_effort = body.get("reasoning_effort")
    if isinstance(reasoning_effort, str):
        out["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
    elif isinstance(body.get("reasoning"), dict):
        r = dict(body["reasoning"])
        r.setdefault("summary", "auto")
        out["reasoning"] = r

    items = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role in ("system", "developer"):
            role = "system"
        elif role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        blocks = []
        if isinstance(content, str):
            blocks = [{"type": "output_text" if role == "assistant" else "input_text", "text": content}]
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    blocks.append({"type": "output_text" if role == "assistant" else "input_text", "text": part})
                elif isinstance(part, dict):
                    ptype = part.get("type")
                    if ptype in ("text", "input_text", "output_text"):
                        blocks.append({
                            "type": "output_text" if role == "assistant" else "input_text",
                            "text": part.get("text", ""),
                        })
                    elif ptype == "image_url":
                        url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else None
                        if url:
                            blocks.append({"type": "input_image", "image_url": url})
                    elif ptype == "image":
                        url = part.get("url")
                        if url:
                            blocks.append({"type": "input_image", "image_url": url})
        if not blocks:
            continue
        items.append({"type": "message", "role": role, "content": blocks})
    if not items:
        items = [{"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": "..."}]}]
    out["input"] = items
    apply_fingerprint_flat(out)
    return out


# ── upstream call ─────────────────────────────────────────────────────────────
def upstream_headers(session_id, request_id, accept):
    return {
        "Authorization": "Bearer public",
        "User-Agent": UA,
        "x-opencode-client": "desktop",
        "x-opencode-session": session_id,
        "x-opencode-request": request_id,
        "x-opencode-project": "global",
        "Content-Type": "application/json",
        "Accept": accept,
    }


def call_upstream(url, payload, headers, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)


def sse_lines(resp):
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        if line.startswith("data:"):
            yield line[5:].strip()


def make_chat_chunk(cid, model, delta, finish=None, usage=None):
    chunk = {
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


# ── handler ───────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenCodeV1/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[oc-v1] %s\n" % (fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._json(200, {"ok": True, "upstream": UPSTREAM,
                                    "sessions": len(SESSIONS)})
        if path == "/v1/models":
            try:
                ids = self._models()
            except Exception as e:
                return self._json(502, {"error": {"message": f"upstream models: {e}",
                                                  "type": "upstream_error"}})
            data = [{"id": i, "object": "model", "created": 0, "owned_by": "opencode"}
                    for i in ids]
            return self._json(200, {"object": "list", "data": data})
        return self._json(404, {"error": {"message": "Not Found", "type": "invalid_request_error"}})

    def _models(self):
        now = time.time()
        with MODELS_LOCK:
            if now - MODELS_CACHE["at"] < MODELS_TTL and MODELS_CACHE["ids"]:
                return list(MODELS_CACHE["ids"])
        with MODELS_LOCK:
            if now - MODELS_CACHE["at"] < MODELS_TTL and MODELS_CACHE["ids"]:
                return list(MODELS_CACHE["ids"])
            req = urllib.request.Request(MODELS_URL, headers={"Accept": "application/json",
                                                              "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                raw = json.loads(resp.read())
            ids = []
            items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("models") or [])
            for item in items:
                mid = item.get("id") if isinstance(item, dict) else item
                if isinstance(mid, str) and is_free_model(mid) and lane_for(mid) != "excluded":
                    ids.append(mid)
            MODELS_CACHE["at"] = now
            MODELS_CACHE["ids"] = ids
            return list(ids)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "Not Found", "type": "invalid_request_error"}})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, {"error": {"message": "Invalid JSON body",
                                              "type": "invalid_request_error"}})
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return self._json(400, {"error": {"message": "'model' is required",
                                              "type": "invalid_request_error"}})
        lane = lane_for(model)
        if lane == "excluded":
            return self._json(400, {
                "error": {"message": f"Model {model} uses the systemone lane, not available on this endpoint",
                          "type": "invalid_request_error"}})
        try:
            identity = self.headers.get("Authorization") or f"ip:{self.client_address[0]}"
            session_id = session_for(identity)
            request_id = derive_request_id(session_id, last_user_text(body))
            if lane == "responses":
                return self._handle_responses(model, body, session_id, request_id)
            return self._handle_chat(model, body, session_id, request_id)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:800]
            return self._json(e.code, {"error": {"message": f"upstream {e.code}: {detail}",
                                                 "type": "upstream_error"}})
        except Exception as e:
            return self._json(502, {"error": {"message": str(e), "type": "upstream_error"}})

    # lane: chat completions ---------------------------------------------------
    def _handle_chat(self, model, body, session_id, request_id):
        payload = dict(body)
        payload["model"] = model
        payload["stream"] = True
        rename = apply_fingerprint_chat(payload)
        want_stream = bool(body.get("stream"))

        headers = upstream_headers(session_id, request_id, "text/event-stream")
        resp = call_upstream(CHAT_URL, payload, headers)

        if not want_stream:
            content, reasoning, tool_calls, finish, usage, cid = [], [], None, None, None, ""
            for data in sse_lines(resp):
                if not data or data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                cid = cid or chunk.get("id", "")
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                    if delta.get("content"):
                        content.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    if delta.get("tool_calls"):
                        tool_calls = tool_calls or []
                        for call in delta["tool_calls"]:
                            if len(tool_calls) <= call.get("index", 0):
                                tool_calls.append({"id": "", "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                            target = tool_calls[call.get("index", 0)]
                            if call.get("id"):
                                target["id"] = call["id"]
                            fn = call.get("function") or {}
                            if fn.get("name"):
                                target["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                target["function"]["arguments"] += fn["arguments"]
            resp.close()
            message = {"role": "assistant", "content": "".join(content)}
            if reasoning:
                message["reasoning_content"] = "".join(reasoning)
            if tool_calls:
                restore = {"choices": [{"delta": {"tool_calls": tool_calls}}]}
                message["tool_calls"] = restore_tool_names(restore, rename)["choices"][0]["delta"]["tool_calls"]
            out = {
                "id": cid or f"chatcmpl-{uuid_hex()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": finish or "stop"}],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            return self._json(200, out)

        # streaming: relay chunk apa adanya, restore nama tool
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True
        try:
            for data in sse_lines(resp):
                if data == "[DONE]":
                    self.wfile.write(b"data: [DONE]\n\n")
                    break
                try:
                    chunk = json.loads(data)
                    restore_tool_names(chunk, rename)
                    data = json.dumps(chunk, separators=(",", ":"))
                except ValueError:
                    pass
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    # lane: responses ----------------------------------------------------------
    def _handle_responses(self, model, body, session_id, request_id):
        payload = chat_to_responses(body)
        want_stream = bool(body.get("stream"))
        headers = upstream_headers(session_id, request_id, "text/event-stream")
        resp = call_upstream(RESPONSES_URL, payload, headers)

        if not want_stream:
            text, reasoning, finish, usage, rid = [], [], None, None, ""
            for data in sse_lines(resp):
                if not data or data == "[DONE]":
                    break
                try:
                    evt = json.loads(data)
                except ValueError:
                    continue
                etype = evt.get("type", "")
                if etype in ("response.output_text.delta", "response.output_text.done"):
                    if etype.endswith(".delta"):
                        text.append(evt.get("delta", ""))
                elif etype in ("response.reasoning_summary_text.delta",):
                    reasoning.append(evt.get("delta", ""))
                elif etype in ("response.completed", "response.incomplete"):
                    finish = "length" if etype.endswith("incomplete") else "stop"
                    resp_obj = evt.get("response") or {}
                    rid = resp_obj.get("id", "")
                    u = resp_obj.get("usage")
                    if u:
                        usage = {"prompt_tokens": u.get("input_tokens", 0),
                                 "completion_tokens": u.get("output_tokens", 0),
                                 "total_tokens": u.get("total_tokens", 0)}
                elif etype == "response.failed":
                    err = (evt.get("response") or {}).get("error") or evt.get("error") or {}
                    raise RuntimeError(str(err.get("message") or err)[:400])
                elif etype == "error":
                    raise RuntimeError(json.dumps(evt)[:400])
            resp.close()
            message = {"role": "assistant", "content": "".join(text)}
            if reasoning:
                message["reasoning_content"] = "".join(reasoning)
            out = {
                "id": rid or f"chatcmpl-{uuid_hex()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": finish or "stop"}],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            return self._json(200, out)

        # stream responses -> stream chat.completion.chunk
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True
        cid = f"chatcmpl-{uuid_hex()}"
        created = int(time.time())

        def emit(delta, finish=None, usage=None):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage:
                chunk["usage"] = usage
            self.wfile.write(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode())
            self.wfile.flush()

        try:
            emit({"role": "assistant", "content": ""})
            for data in sse_lines(resp):
                if not data or data == "[DONE]":
                    break
                try:
                    evt = json.loads(data)
                except ValueError:
                    continue
                etype = evt.get("type", "")
                if etype == "response.output_text.delta":
                    emit({"content": evt.get("delta", "")})
                elif etype == "response.reasoning_summary_text.delta":
                    emit({"reasoning_content": evt.get("delta", "")})
                elif etype in ("response.completed", "response.incomplete"):
                    resp_obj = evt.get("response") or {}
                    u = resp_obj.get("usage")
                    usage = None
                    if u:
                        usage = {"prompt_tokens": u.get("input_tokens", 0),
                                 "completion_tokens": u.get("output_tokens", 0),
                                 "total_tokens": u.get("total_tokens", 0)}
                    emit({}, finish="length" if etype.endswith("incomplete") else "stop", usage=usage)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()


_SERVER_INSTANCE = None
_SERVER_LOCK = threading.Lock()


def start_proxy_thread(host="127.0.0.1", port=20130):
    """Jalankan proxy di thread daemon (dipanggil saat bot.py start).

    Idempoten: jika server sudah hidup di port ini, kembalikan instance lama.
    Jika port sudah dipakai proses lain (misal proxy standalone), skip halus.
    """
    global _SERVER_INSTANCE
    with _SERVER_LOCK:
        if _SERVER_INSTANCE is not None:
            return _SERVER_INSTANCE
        try:
            srv = ThreadingHTTPServer((host, port), Handler)
        except OSError as e:
            sys.stderr.write(f"[oc-v1] port {port} sudah terpakai ({e}), anggap proxy eksternal aktif\n")
            return None
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="opencode-proxy")
        t.start()
        _SERVER_INSTANCE = srv
        sys.stderr.write(f"[oc-v1] proxy thread started on http://{host}:{port}\n")
        return srv


def uuid_hex():
    return secrets.token_hex(12)


def main():
    ap = argparse.ArgumentParser(description="OpenCode Free -> OpenAI-compatible proxy")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20130)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[oc-v1] listening on http://{args.host}:{args.port} -> {UPSTREAM}")
    print(f"[oc-v1] endpoints: GET /health, GET /v1/models, POST /v1/chat/completions")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
