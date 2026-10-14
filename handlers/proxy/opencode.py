#!/usr/bin/env python3
# language: Python 3.12, file: opencode.py, target: Linux (stdlib only)
"""
OpenCode free tier -> OpenAI-compatible proxy.

Menyajikan endpoint /v1/models dan /v1/chat/completions (stream & non-stream)
yang di-backing opencode.ai free tier (Bearer public, tanpa API key).

KONTRAK REQUEST (v2, berlaku sejak 2026-10-14) — semua ini WAJIB, meleset satu
saja dibalas 403 FreeTierError:
  1. Endpoint   : /inference/v1/chat/completions  (BUKAN /zen/v1 lagi)
  2. User-Agent : opencode/<channel>/<version>/<client>, mis. opencode/latest/2.0.26/cli
                  Bentuk v1 ("opencode/1.18.31") DITOLAK.
  3. Session    : x-opencode-session DAN x-opencode-session-id (+ x-session-affinity,
                  x-session-id). Salah satu saja DITOLAK. Format kanonik:
                  ses_ + 12 hex + 14 base62, prefix = ~(timeMs * 0x1000 + counter)
                  dengan counter kecil (0-16 lolos, >=100 ditolak).
  4. Request id : header x-opencode-request TIDAK BOLEH ADA (v1 dulu wajib, kini ditolak).
  5. Tools      : definisi `read` dan `shell` milik OpenCode, deskripsi + parameter
                  PERSIS. Stub/rename DITOLAK. `tool_choice: "none"` DITOLAK.
  6. Streaming  : upstream hanya melayani stream:true. Kontrak ini selalu memaksa
                  stream ke upstream lalu meng-agregasi sendiri untuk klien non-stream.
  7. Kuota      : dihitung per session. Satu session kanonik dipakai ulang sampai TTL;
                  menerbitkan session baru tiap request memicu 429 FreeUsageLimitError.

Lane (model -> endpoint upstream):
  - chat      : /inference/v1/chat/completions  (default)
  - responses : /inference/v1/responses         (muse-spark-*-contributor-free)
  - excluded  : jev-* (systemone)               -> 400, bukan lane chat

Pemakaian:
  python3 opencode.py                 # 127.0.0.1:20130
  python3 opencode.py --port 8080 --host 0.0.0.0
"""
import argparse
import json
import logging
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("oc-proxy")

UPSTREAM = "https://opencode.ai"
CHAT_URL = UPSTREAM + "/inference/v1/chat/completions"
RESPONSES_URL = UPSTREAM + "/inference/v1/responses"
MODELS_URL = UPSTREAM + "/zen/v1/models"

# UA v2: opencode/<channel>/<version>/<client>. Bentuk v1 ditolak upstream.
OPENCODE_VERSION = os.getenv("OPENCODE_VERSION", "2.0.26").strip() or "2.0.26"
UA = f"opencode/latest/{OPENCODE_VERSION}/cli"
UA_V2_RE = re.compile(r"^opencode/[a-z0-9-]+/(\d+)\.\d+\.\d+/[a-z0-9-]+$", re.I)

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


# ── Definisi tool fingerprint (verbatim OpenCode v2.0.26) ─────────────────────
# Upstream membandingkan deskripsi & parameter, jadi JANGAN diubah kata-katanya.
FINGERPRINT_TOOL_DEFINITIONS = [
    {
        "name": "read",
        "description": (
            "Read the contents of a file or directory. Supports text files, images, and PDFs. "
            "Images and PDFs are presented directly to the model. Each text line is prefixed by "
            "its 1-based line number as <line>: <content>. The prefix is for reference and is not "
            "part of the file content. Directory entries are returned one per line. Use offset and "
            "limit to read large files or directories in sections. Prefer one larger read over many "
            "small slices, and use grep to find specific content in large files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory to read"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "The line or directory entry to start reading from (1-based)",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "The maximum number of lines or directory entries to read (defaults to and capped at 2000)",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "shell",
        "description": (
            "Execute a shell command and return its output. Commands run on Linux using bash. "
            "Quote file paths containing spaces or special characters. Prefer dedicated tools over "
            "shell commands when possible. When output is large, the full result is saved to a file "
            "and a truncated preview is returned. Rely on automatic truncation unless filtering the "
            "output is more useful. Commands accept an optional timeout, background commands have no "
            "timeout by default. Background commands return immediately, and you will be notified "
            "when they complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command string to execute"},
                "workdir": {
                    "type": "string",
                    "description": (
                        "Working directory to execute the command in. Defaults to the current working "
                        "directory. When possible, avoid changing directories in the command and set "
                        "the working directory here instead."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Timeout in milliseconds. Set to 0 to disable the timeout. Defaults to 120000 "
                        "for foreground commands. Background commands have no timeout by default."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run the command in the background and return immediately (useful for dev "
                        "servers and long-running builds). You do not need to use '&' at the end of "
                        "the command when using this parameter. You will be notified when it "
                        "completes. DO NOT poll for completion."
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]

PLACEHOLDER_NOTE = (
    "Do not call the `read` or `shell` tool; it is a placeholder that does nothing. "
    "Use the other tools you have been given instead."
)


def _fingerprint_tools(flat=False):
    """Bangun definisi tool fingerprint sesuai bentuk lane (chat vs responses)."""
    out = []
    for spec in FINGERPRINT_TOOL_DEFINITIONS:
        if flat:
            out.append({
                "type": "function",
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            })
        else:
            out.append({
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            })
    return out


def _tool_name(tool):
    if not isinstance(tool, dict):
        return ""
    if isinstance(tool.get("name"), str) and tool["name"].strip():
        return tool["name"].strip()
    fn = tool.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"].strip()
    return ""


def _is_verbatim_fingerprint(tool, name):
    """True kalau tool sudah memakai definisi fingerprint persis milik OpenCode."""
    spec = next((s for s in FINGERPRINT_TOOL_DEFINITIONS if s["name"] == name), None)
    if spec is None:
        return False
    holder = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    if not isinstance(holder, dict):
        return False
    if holder.get("description") != spec["description"]:
        return False
    try:
        return json.dumps(holder.get("parameters"), sort_keys=True) == json.dumps(spec["parameters"], sort_keys=True)
    except Exception:
        return False


def apply_fingerprint_chat(body):
    """Pasang fingerprint tool untuk lane chat. Return rename map.

    Aturan upstream: `read`/`shell` harus ada dengan definisi verbatim. Kalau
    klien sudah mendeklarasikan nama itu dengan definisi berbeda, pindahkan ke
    alias `<name>_client` supaya tidak bentrok, lalu pulihkan di response.
    `tool_choice: "none"` DITOLAK, jadi dibuang bila ada.
    """
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    rename = {}
    out = []
    seen = set()
    taken = {_tool_name(t) for t in tools}

    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        name = _tool_name(tool)
        if name not in ("read", "shell"):
            out.append(tool)
            continue
        if name in seen:
            continue  # duplikat nama fingerprint ditolak upstream
        seen.add(name)
        if _is_verbatim_fingerprint(tool, name):
            out.append(tool)
            continue
        alias = f"{name}_client"
        i = 2
        while alias in taken:
            alias = f"{name}_client{i}"
            i += 1
        taken.add(alias)
        rename[alias] = name
        clone = json.loads(json.dumps(tool))
        if isinstance(clone.get("function"), dict):
            clone["function"]["name"] = alias
        elif "name" in clone:
            clone["name"] = alias
        out.append(clone)

    # Tambahkan fingerprint yang belum ada
    for spec in FINGERPRINT_TOOL_DEFINITIONS:
        if spec["name"] in seen:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        })
        seen.add(spec["name"])

    body["tools"] = out

    # tool_choice "none" ditolak; hapus agar upstream memakai default.
    if body.get("tool_choice") == "none":
        body.pop("tool_choice", None)
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        if isinstance(choice.get("name"), str) and choice["name"] in rename:
            choice["name"] = rename[choice["name"]]
        elif isinstance(choice.get("function"), dict):
            inner = choice["function"].get("name", "")
            if inner in rename:
                choice["function"]["name"] = rename[inner]

    # Beri tahu model bahwa tool fingerprint hanyalah placeholder, supaya ia
    # tidak memanggilnya. Tanpa catatan ini model benar-benar memanggil `read`.
    _inject_placeholder_note(body)
    return rename


def _inject_placeholder_note(body):
    """Sisipkan catatan placeholder ke system message (chat shape)."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return
    if body.get("system") is not None:
        return  # bentuk Claude memakai field `system`
    if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system" \
            and isinstance(msgs[0].get("content"), str):
        first = dict(msgs[0])
        first["content"] = first["content"] + "\n\n" + PLACEHOLDER_NOTE
        body["messages"] = [first] + msgs[1:]
    else:
        body["messages"] = [{"role": "system", "content": PLACEHOLDER_NOTE}] + msgs


def apply_fingerprint_flat(body):
    """Pasang fingerprint tool untuk lane Responses (bentuk flat {name})."""
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    body["tools"] = [t for t in tools if isinstance(t, dict)]
    seen = {_tool_name(t) for t in body["tools"]}
    for spec in FINGERPRINT_TOOL_DEFINITIONS:
        if spec["name"] in seen:
            continue
        body["tools"].append({
            "type": "function",
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
        })
    # Responses: default eksplisit "auto", jangan pernah "none".
    if body.get("tool_choice") == "none" or not body.get("tool_choice"):
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


# ── Chat -> Responses conversion (lane responses) ────────────────────────────
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


# ── Session store (stabil per klien; kuota upstream dihitung per sesi) ────────
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TTL = 6 * 3600
MAX_SESSIONS = 2000
_counter = {"ts": 0, "n": 0}
_counter_lock = threading.Lock()


def generate_session_id():
    """ses_ + 12 hex (prefix kanonik) + 14 base62.

    Prefix = ~(timeMs * 0x1000 + counter). Upstream men-decode ini dan menolak
    counter yang tidak kecil (>=100) dengan 403 FreeTierError.
    """
    ts = now_ms()
    with _counter_lock:
        if ts != _counter["ts"]:
            _counter["ts"] = ts
            _counter["n"] = 0
        _counter["n"] += 1
        counter = _counter["n"]
    value = (~(ts * 0x1000 + counter)) & ((1 << 48) - 1)
    return "ses_" + format(value, "012x") + "".join(secrets.choice(BASE62) for _ in range(14))


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


# ── Upstream call ─────────────────────────────────────────────────────────────
def upstream_headers(session_id, accept):
    """Header v2. CATATAN: `x-opencode-request` sengaja TIDAK dikirim.

    Upstream menolak header itu (warisan v1) dengan 403 FreeTierError.
    """
    return {
        "Authorization": "Bearer public",
        "User-Agent": UA,
        "x-opencode-client": "cli",
        "x-opencode-session": session_id,
        "x-opencode-session-id": session_id,
        "x-session-affinity": session_id,
        "x-session-id": session_id,
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


def uuid_hex():
    return secrets.token_hex(12)


# ── Handler ───────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenCodeV1/2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[oc-proxy] %s - %s\n" % (self.address_string(), fmt % args))
        sys.stderr.flush()

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
                                    "endpoint": CHAT_URL, "sessions": len(SESSIONS)})
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
            if lane == "responses":
                return self._handle_responses(model, body, session_id)
            return self._handle_chat(model, body, session_id)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:800]
            return self._json(e.code, {"error": {"message": f"upstream {e.code}: {detail}",
                                                 "type": "upstream_error"}})
        except Exception as e:
            return self._json(502, {"error": {"message": str(e), "type": "upstream_error"}})

    # lane: chat completions ---------------------------------------------------
    def _handle_chat(self, model, body, session_id):
        payload = dict(body)
        payload["model"] = model
        # Upstream hanya melayani stream:true; non-stream diagregasi di sini.
        payload["stream"] = True
        rename = apply_fingerprint_chat(payload)
        want_stream = bool(body.get("stream"))

        headers = upstream_headers(session_id, "text/event-stream")
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

        # streaming: relay chunk apa adanya, pulihkan nama tool
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
    def _handle_responses(self, model, body, session_id):
        payload = chat_to_responses(body)
        want_stream = bool(body.get("stream"))
        headers = upstream_headers(session_id, "text/event-stream")
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
                if etype == "response.output_text.delta":
                    text.append(evt.get("delta", ""))
                elif etype == "response.reasoning_summary_text.delta":
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

        # stream responses -> chunk chat.completion
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
            sys.stderr.write(f"[oc-proxy] port {port} sudah terpakai ({e}), anggap proxy eksternal aktif\n")
            return None
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="opencode-proxy")
        t.start()
        _SERVER_INSTANCE = srv
        sys.stderr.write(f"[oc-proxy] proxy thread started on http://{host}:{port}\n")
        return srv


def main():
    ap = argparse.ArgumentParser(description="OpenCode Free -> OpenAI-compatible proxy")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20130)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[oc-proxy] listening on http://{args.host}:{args.port} -> {CHAT_URL}")
    print(f"[oc-proxy] endpoints: GET /health, GET /v1/models, POST /v1/chat/completions")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
