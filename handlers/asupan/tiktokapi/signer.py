

from __future__ import annotations

import base64
import hashlib
import random
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

MASK32: Final = 0xFFFFFFFF

DYNOSAUR_PARAM: Final = "X-Dynosaur"
MS_TOKEN_PARAM: Final = "msToken"
BOGUS_PARAM: Final = "X-Bogus"
GNARLY_PARAM: Final = "X-Gnarly"

BOGUS_VALUE: Final = "1"

ALPHABET: Final = "u09tbS3UvgDEe6r-ZVMXzLpsAohTn7mdINQlW412GqBjfYiyk8JORCF5/xKHwacP"
_STANDARD: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_TO_CUSTOM: Final = str.maketrans(_STANDARD, ALPHABET)
_FROM_CUSTOM: Final = str.maketrans(ALPHABET, _STANDARD)

ENVELOPE_TAG: Final = 0x4B

CHACHA_INIT: Final = (1196819126, 600974999, 3863347763, 1451689750)

FNV_OFFSET: Final = 2166136260
FNV_PRIME: Final = 16777619

SDK_VERSION: Final = "5.3.2"
SCM_VERSION: Final = "2.0.0.561"

ENV_CODE: Final = 65
UB_CODE: Final = 8

VM_STATE_HASH: Final = 0xC46CE353

CANVAS_HASH: Final = "-1"

WEBGL_HASH: Final = "0"
COMPONENT_VERSION: Final = "0"
DEVICE_HASH: Final = "0"

PAGE: Final = "www.tiktok.com/"

CALL_SEQUENCE_START: Final = 1

EMPTY_BODY_MD5: Final = "d41d8cd98f00b204e9800998ecf8427e"

ENCODER_A: Final = (103, 1, None, 2, 1)
ENCODER_B: Final = (102, 0, 165, 1, 0)


_MUST_ESCAPE: Final = {" ": "%20", '"': "%22", "<": "%3C", ">": "%3E", "`": "%60", "#": "%23"}


def encode_query(pairs: Iterable[tuple[str, str]]) -> str:
    return "&".join(f"{_escape(key)}={_escape(value)}" for key, value in pairs)


def _escape(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch in _MUST_ESCAPE:
            out.append(_MUST_ESCAPE[ch])
        elif " " < ch <= "~":
            out.append(ch)
        else:
            out.extend(f"%{byte:02X}" for byte in ch.encode("utf-8"))
    return "".join(out)


def hash_state(text: str) -> int:
    value = FNV_OFFSET
    for byte in text.encode("utf-8"):
        step = ((value ^ byte) * FNV_PRIME) & MASK32
        value = (step + ((step * 32) & MASK32)) & MASK32
    return value


def encode_field(text: str, config: tuple[int, int, int | None, int, int]) -> bytes:
    xor_base, add_base, pre_xor, rotate, post_add = config
    size = max(len(text) + 2, 6)
    out = bytearray(size)
    for index, char in enumerate(text):
        value = (ord(char) ^ (xor_base + index)) & MASK32
        value = (value + add_base + (170 & index)) % 256
        if pre_xor is not None:
            value ^= pre_xor
        value = ((value << rotate) | (value >> (8 - rotate))) & 0xFF
        out[index] = ((value ^ 187) + post_add) % 256
    for index in range(len(text), size - 2):
        out[index] = (221 + index) & 0xFF
    out[size - 2] = 0
    out[size - 1] = len(text)
    return bytes(out)


def pack_payload(
    fields: Mapping[int, bytes],
    order: Sequence[int] | None = None,
    *,
    lead_count: bool = False,
) -> bytes:
    keys = sorted(fields) if order is None else list(order)
    body = b"".join(bytes((key, 0, len(fields[key]))) + fields[key] for key in keys)
    return bytes((len(keys),)) + body if lead_count else body


def _be(value: int, size: int) -> bytes:
    return int(value).to_bytes(size, "big")


def mix_state(timestamp: int, nonce: int, env_code: int) -> int:
    folded = ((timestamp >> 16) ^ (nonce >> 16) ^ timestamp ^ nonce) & 0xFFFF
    return folded | (env_code << 16)


def fold_checksum(values: Sequence[int | str], mode: int) -> int:
    accumulator = MASK32
    for value in values:
        if isinstance(value, str):
            number = 0 if mode == 1 else int.from_bytes(value.encode("utf-8")[:4], "big")
        else:
            number = value
        accumulator ^= number & MASK32
    return accumulator & MASK32


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & MASK32
    state[d] ^= state[a]
    state[d] = ((state[d] << 16) | (state[d] >> 16)) & MASK32
    state[c] = (state[c] + state[d]) & MASK32
    state[b] ^= state[c]
    state[b] = ((state[b] << 12) | (state[b] >> 20)) & MASK32
    state[a] = (state[a] + state[b]) & MASK32
    state[d] ^= state[a]
    state[d] = ((state[d] << 8) | (state[d] >> 24)) & MASK32
    state[c] = (state[c] + state[d]) & MASK32
    state[b] ^= state[c]
    state[b] = ((state[b] << 7) | (state[b] >> 25)) & MASK32


def _keystream(state: Sequence[int], rounds: int) -> list[int]:
    working = list(state)
    done = 0
    while done < rounds:
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        done += 1
        if done >= rounds:
            break
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 12, 13)
        _quarter_round(working, 3, 4, 13, 14)
        done += 1
    return [(working[i] + state[i]) & MASK32 for i in range(16)]


def _crypt(key: Sequence[int], rounds: int, payload: bytes) -> bytes:
    state = [*CHACHA_INIT, *(word & MASK32 for word in key)]
    length = len(payload)
    word_count = (length + 3) // 4
    words = [
        int.from_bytes(payload[4 * i : 4 * i + 4].ljust(4, b"\0"), "little")
        for i in range(word_count)
    ]
    offset = 0
    while offset + 16 < word_count:
        block = _keystream(state, rounds)
        state[12] = (state[12] + 1) & MASK32
        for i in range(16):
            words[offset + i] ^= block[i]
        offset += 16
    block = _keystream(state, rounds)
    for i in range(word_count - offset):
        words[offset + i] ^= block[i]
    return b"".join(word.to_bytes(4, "little") for word in words)[:length]


def seal(payload: bytes, key: Sequence[int]) -> str:
    rounds = (sum(word & 0xF for word in key) & 0xF) + 5
    ciphertext = _crypt(key, rounds, payload)
    key_bytes = b"".join(int(word).to_bytes(4, "little") for word in key)
    position = (sum(key_bytes) + sum(ciphertext)) % (len(ciphertext) + 1)
    spliced = ciphertext[:position] + key_bytes + ciphertext[position:]
    raw = bytes((ENVELOPE_TAG,)) + spliced
    return base64.b64encode(raw).decode("ascii").translate(_TO_CUSTOM)


KEY_WORDS: Final = 12


def unseal(token: str) -> tuple[bytes, tuple[int, ...]]:
    raw = base64.b64decode(token.translate(_FROM_CUSTOM))
    if not raw or raw[0] != ENVELOPE_TAG:
        raise ValueError("not a signature envelope")
    spliced = raw[1:]
    width = KEY_WORDS * 4
    for position in range(len(spliced) - width + 1):
        key_bytes = spliced[position : position + width]
        ciphertext = spliced[:position] + spliced[position + width :]
        if position != (sum(key_bytes) + sum(ciphertext)) % (len(ciphertext) + 1):
            continue
        key = tuple(
            int.from_bytes(key_bytes[4 * i : 4 * i + 4], "little") for i in range(KEY_WORDS)
        )
        rounds = (sum(word & 0xF for word in key) & 0xF) + 5
        return _crypt(key, rounds, ciphertext), key
    raise ValueError("no splice position satisfies the key relation")


def unpack_payload(payload: bytes, *, lead_count: bool = False) -> dict[int, bytes]:
    fields: dict[int, bytes] = {}
    offset = 1 if lead_count else 0
    while offset + 3 <= len(payload):
        key, _, length = payload[offset], payload[offset + 1], payload[offset + 2]
        fields[key] = payload[offset + 3 : offset + 3 + length]
        offset += 3 + length
    return fields


def decode_field(raw: bytes, config: tuple[int, int, int | None, int, int]) -> str:
    xor_base, add_base, pre_xor, rotate, post_add = config
    out = []
    for index in range(raw[-1]):
        value = ((raw[index] - post_add) % 256) ^ 187
        value = ((value >> rotate) | (value << (8 - rotate))) & 0xFF
        if pre_xor is not None:
            value ^= pre_xor
        value = (value - add_base - (170 & index)) % 256
        out.append(chr(value ^ (xor_base + index)))
    return "".join(out)


def dynosaur_payload(
    query: str,
    user_agent: str,
    *,
    timestamp: int,
    nonce: int,
    sequence: int = CALL_SEQUENCE_START,
    env_code: int = ENV_CODE,
    ub_code: int = UB_CODE,
) -> bytes:
    fields: dict[int, bytes] = {
        0x21: encode_field("1", ENCODER_B),
        0x22: encode_field("1", ENCODER_B),
        0x23: encode_field("0", ENCODER_A),
        0x24: encode_field(str(mix_state(timestamp, nonce, env_code)), ENCODER_A),
        0x25: encode_field(str(sequence), ENCODER_A),
        0x26: encode_field(str(env_code), ENCODER_A),
        0x27: encode_field(str(timestamp), ENCODER_A),
        0x28: encode_field(WEBGL_HASH, ENCODER_A),
        0x29: encode_field("0", ENCODER_A),
        0x2A: encode_field(SDK_VERSION, ENCODER_A),
        0x2B: _be(hash_state(""), 4),
        0x2C: encode_field(CANVAS_HASH, ENCODER_A),
        0x2D: encode_field("0", ENCODER_A),
        0x2E: _be(hash_state(query), 4),
        0x2F: encode_field(str(sequence), ENCODER_A),
        0x30: _be(hash_state(user_agent), 4),
        0x31: encode_field(SCM_VERSION, ENCODER_A),
        0x32: encode_field(COMPONENT_VERSION, ENCODER_A),
        0x33: encode_field(DEVICE_HASH, ENCODER_A),
        0x34: encode_field(str(nonce), ENCODER_A),
        0x35: encode_field(PAGE, ENCODER_A),
        0x36: encode_field(str(ub_code), ENCODER_A),
        0x37: encode_field("0", ENCODER_A),
        0x38: _be(VM_STATE_HASH, 4),
        0x20: encode_field("0", ENCODER_A),
    }
    checksum = 0
    for key in sorted(fields):
        checksum ^= fields[key][1]
    fields[0x20] = encode_field(str(checksum), ENCODER_B)
    return pack_payload(fields)


def gnarly_fields(
    signed_query: str,
    user_agent: str,
    *,
    body: bytes = b"",
    timestamp: int,
    nonce: int,
    nonce2: int,
    sequence: int = CALL_SEQUENCE_START,
    env_code: int = ENV_CODE,
    ub_code: int = UB_CODE,
) -> dict[int, bytes]:
    query_md5 = hashlib.md5(signed_query.encode("utf-8")).hexdigest()
    body_md5 = hashlib.md5(body).hexdigest()
    agent_md5 = hashlib.md5(user_agent.encode("utf-8")).hexdigest()
    mixed = mix_state(timestamp, nonce, env_code)
    covered: list[int | str] = [
        0,
        env_code,
        ub_code,
        query_md5,
        body_md5,
        agent_md5,
        timestamp,
        0,
        nonce,
        SDK_VERSION,
        SCM_VERSION,
        CALL_SEQUENCE_START,
        sequence,
        sequence,
        mixed,
        nonce2,
    ]
    first = fold_checksum(covered, 2)
    second = fold_checksum([*covered, first], 1)
    return {
        0x00: _be(second, 4),
        0x01: _be(env_code, 2),
        0x02: _be(ub_code, 2),
        0x03: query_md5.encode("ascii"),
        0x04: body_md5.encode("ascii"),
        0x05: agent_md5.encode("ascii"),
        0x06: _be(timestamp, 4),
        0x08: _be(nonce, 4),
        0x09: SDK_VERSION.encode("ascii"),
        0x0A: SCM_VERSION.encode("ascii"),
        0x0B: _be(CALL_SEQUENCE_START, 2),
        0x0C: _be(sequence, 2),
        0x0D: _be(sequence, 2),
        0x0E: _be(mixed, 4),
        0x0F: _be(nonce2, 4),
        0x10: _be(first, 4),
    }


def gnarly_payload(
    signed_query: str,
    user_agent: str,
    *,
    body: bytes = b"",
    timestamp: int,
    nonce: int,
    nonce2: int,
    sequence: int = CALL_SEQUENCE_START,
    order: Sequence[int] | None = None,
    env_code: int = ENV_CODE,
    ub_code: int = UB_CODE,
) -> bytes:
    fields = gnarly_fields(
        signed_query,
        user_agent,
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        nonce2=nonce2,
        sequence=sequence,
        env_code=env_code,
        ub_code=ub_code,
    )
    return pack_payload(fields, order, lead_count=True)


def _clock_nonce() -> int:
    return int(time.time() * 1_000_000) & MASK32


def _random_key(rng: random.Random) -> tuple[int, ...]:
    return tuple(rng.getrandbits(32) for _ in range(12))


def sign(
    pairs: Sequence[tuple[str, str]],
    user_agent: str,
    *,
    ms_token: str = "",
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: int | None = None,
    nonce2: int | None = None,
    sequence: int = CALL_SEQUENCE_START,
    rng: random.Random | None = None,
    key: Sequence[int] | None = None,
    key2: Sequence[int] | None = None,
) -> tuple[str, dict[str, str]]:
    source = rng or random.Random()
    stamp = int(time.time()) if timestamp is None else int(timestamp)
    first_nonce = _clock_nonce() if nonce is None else int(nonce)
    second_nonce = (~_clock_nonce()) & MASK32 if nonce2 is None else int(nonce2)

    query = encode_query(pairs)
    dynosaur = seal(
        dynosaur_payload(query, user_agent, timestamp=stamp, nonce=first_nonce, sequence=sequence),
        key if key is not None else _random_key(source),
    )
    sealed_query = f"{query}&{DYNOSAUR_PARAM}={dynosaur}&{MS_TOKEN_PARAM}={ms_token}"
    gnarly = seal(
        gnarly_payload(
            sealed_query,
            user_agent,
            body=body,
            timestamp=stamp,
            nonce=first_nonce,
            nonce2=second_nonce,
            sequence=sequence,
        ),
        key2 if key2 is not None else _random_key(source),
    )
    parameters = {
        DYNOSAUR_PARAM: dynosaur,
        MS_TOKEN_PARAM: ms_token,
        BOGUS_PARAM: BOGUS_VALUE,
        GNARLY_PARAM: gnarly,
    }
    signed = f"{sealed_query}&{BOGUS_PARAM}={BOGUS_VALUE}&{GNARLY_PARAM}={gnarly}"
    return signed, parameters


def pick_ms_token(cookies: Mapping[str, str] | None) -> str:
    return (cookies or {}).get(MS_TOKEN_PARAM) or ""


__all__ = [
    "ALPHABET",
    "BOGUS_PARAM",
    "BOGUS_VALUE",
    "CALL_SEQUENCE_START",
    "DYNOSAUR_PARAM",
    "ENCODER_A",
    "ENCODER_B",
    "ENV_CODE",
    "GNARLY_PARAM",
    "MS_TOKEN_PARAM",
    "SCM_VERSION",
    "SDK_VERSION",
    "UB_CODE",
    "decode_field",
    "dynosaur_payload",
    "encode_field",
    "encode_query",
    "fold_checksum",
    "gnarly_fields",
    "gnarly_payload",
    "hash_state",
    "mix_state",
    "pack_payload",
    "pick_ms_token",
    "seal",
    "sign",
    "unpack_payload",
    "unseal",
]
