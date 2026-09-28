import re
import json
import base64

_PACKER_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)",
    re.S,
)

# Base62 Dean Edwards: 0-9, a-z, A-Z
_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _to_base(num: int, base: int) -> str:
    if num == 0:
        return "0"
    out = ""
    while num:
        out = _BASE62[num % base] + out
        num //= base
    return out


def _unpack_eval(packed: str, radix: int, count: int, words: list) -> str:
    """Unpack Dean Edwards packer (mendukung radix hingga 62)."""
    for i in range(count - 1, -1, -1):
        if i < len(words) and words[i]:
            packed = re.sub(
                r"\b" + re.escape(_to_base(i, radix)) + r"\b",
                lambda m, v=words[i]: v,
                packed,
            )
    return packed


def _deobfuscate_voe_json(html_text: str):
    """Bongkar obfuscation script JSON dari player VOE (seperti miaw.lol)
    menggunakan ROT13 -> replace patterns -> b64 -> shift(3) -> reverse -> b64 -> JSON.
    """
    m = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return None, None
    try:
        arr = json.loads(m.group(1).strip())
        if not (isinstance(arr, list) and arr and isinstance(arr[0], str)):
            return None, None
        obf = arr[0]
        out = []
        for ch in obf:
            o = ord(ch)
            if 65 <= o <= 90:
                out.append(chr(((o - 65 + 13) % 26) + 65))
            elif 97 <= o <= 122:
                out.append(chr(((o - 97 + 13) % 26) + 97))
            else:
                out.append(ch)
        s1 = "".join(out)
        for p in ['@$', '^^', '~@', '%?', '*~', '!!', '#&']:
            s1 = s1.replace(p, '')
        s3 = base64.b64decode(s1 + '=' * ((4 - len(s1) % 4) % 4)).decode('utf-8', 'replace')
        s4 = "".join(chr(ord(c) - 3) for c in s3)
        s5 = s4[::-1]
        s6 = base64.b64decode(s5 + '=' * ((4 - len(s5) % 4) % 4)).decode('utf-8', 'replace')
        cfg = json.loads(s6)
        return cfg.get("source"), cfg.get("direct_access_url")
    except Exception:
        return None, None
