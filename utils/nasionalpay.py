import asyncio
import hmac
import hashlib
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import aiohttp

from utils.config import NASIONALPAY_BASE_URL, NASIONALPAY_STORE_KEY

log = logging.getLogger(__name__)

API_PREFIX = "/api/payment"
CREATE_TIMEOUT = 25
CHECK_TIMEOUT = 15

MIN_AMOUNT = 100
MAX_AMOUNT = 10_000_000

RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526}
CREATE_ATTEMPTS = 5
CHECK_ATTEMPTS = 3
BASE_BACKOFF = 0.5

WIB = timezone(timedelta(hours=7))

PAID_STATUSES = {"SUCCESS", "PAID", "SETTLED"}
FAILED_STATUSES = {"FAILED", "EXPIRED", "CANCELLED", "CANCELED", "DENIED", "REVERSED"}


class NasionalPayError(RuntimeError):

    def __init__(self, message: str, *, status: int | None = None, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


def configured() -> bool:
    return bool(NASIONALPAY_STORE_KEY)


def _store_key() -> str:
    if not NASIONALPAY_STORE_KEY:
        raise NasionalPayError("NASIONALPAY_STORE_KEY is not set.")
    return NASIONALPAY_STORE_KEY


def _to_int(value) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return 0


def normalize_status(raw) -> str:
    text = str(raw or "").strip().upper()
    if text in PAID_STATUSES:
        return "PAID"
    if text in FAILED_STATUSES:
        return text
    if not text:
        return "PENDING"
    return "PENDING"


def _parse_wib(value) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=WIB).timestamp()
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=WIB)
        return dt.timestamp()
    except Exception:
        return None


async def _request(path: str, payload: dict, *, timeout: int = CHECK_TIMEOUT, attempts: int = CHECK_ATTEMPTS) -> dict:
    url = f"{NASIONALPAY_BASE_URL}{API_PREFIX}{path}"
    last_err: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(url, json=payload) as resp:
                    status = resp.status
                    text = await resp.text()

                    if status in RETRY_STATUSES:
                        last_err = NasionalPayError(
                            f"NasionalPay gateway unavailable (HTTP {status}).", status=status
                        )
                        if attempt < attempts:
                            delay = BASE_BACKOFF * (1.7 ** (attempt - 1)) + random.uniform(0.1, 0.4)
                            log.info(
                                "NasionalPay HTTP %s, retrying (%d/%d) in %.2fs | path=%s",
                                status, attempt, attempts, delay, path,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise last_err

                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        raise NasionalPayError(
                            f"Unexpected NasionalPay response (HTTP {status}): {text[:200]}",
                            status=status,
                        )

                    if not isinstance(body, dict):
                        raise NasionalPayError(
                            f"Unexpected NasionalPay payload (HTTP {status}).", status=status
                        )

                    if status >= 400 or not body.get("success", False):
                        raise NasionalPayError(
                            str(body.get("message") or f"NasionalPay error HTTP {status}"),
                            status=status,
                            data=body.get("data") if isinstance(body.get("data"), dict) else {},
                        )

                    return body
        except NasionalPayError:
            raise
        except Exception as e:
            last_err = NasionalPayError(f"NasionalPay network error: {e}")
            if attempt < attempts:
                delay = BASE_BACKOFF * (1.7 ** (attempt - 1)) + random.uniform(0.1, 0.4)
                log.info("NasionalPay network error (%s), retrying (%d/%d) in %.2fs", e, attempt, attempts, delay)
                await asyncio.sleep(delay)
                continue
            raise last_err

    raise last_err or NasionalPayError("NasionalPay request failed after retries.")


async def create_qris(amount: int, *, channel: str = "QRIS", username: str = "customer") -> dict:
    try:
        amount = int(amount)
    except Exception:
        raise NasionalPayError("Invalid amount.")
    if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
        raise NasionalPayError(f"Amount must be between {MIN_AMOUNT} and {MAX_AMOUNT}.")

    payload = {
        "key": _store_key(),
        "channel": channel,
        "amount": amount,
        "player_username": str(username)[:64] or "customer",
    }
    body = await _request("/generate", payload, timeout=CREATE_TIMEOUT, attempts=CREATE_ATTEMPTS)
    data = body.get("data") or {}

    trx_id = str(data.get("transaction_id") or "").strip()
    if not trx_id:
        raise NasionalPayError("NasionalPay did not return transaction_id.", data=data)

    created_at = _parse_wib(data.get("created_at")) or time.time()
    expires_at = _parse_wib(data.get("expired_at"))

    return {
        "trx_id": trx_id,
        "qris_id": trx_id,
        "store_name": data.get("store_name"),
        "channel": str(data.get("type") or channel).upper(),
        "amount": _to_int(data.get("amount")) or amount,
        "fee_amount": _to_int(data.get("fee_mdr")),
        "net_amount": _to_int(data.get("nett_amount")),
        "status": normalize_status("pending"),
        "created_at": created_at,
        "expires_at": expires_at,
        "qris_string": data.get("qris_data"),
        "qr_url": str(data.get("qris_image") or "").strip() or None,
        "payment_url": data.get("payment_url"),
        "instruction": data.get("instruction"),
    }


async def check_payment(
    trx_id: str | None = None,
    qris_id: str | None = None,
    *,
    attempts: int = CHECK_ATTEMPTS,
) -> dict:
    identifier = (trx_id or qris_id or "").strip()
    if not identifier:
        raise NasionalPayError("check_payment needs trx_id.")

    body = await _request(
        "/status",
        {"key": _store_key(), "transaction_id": identifier},
        attempts=attempts,
    )
    data = body.get("data") or {}
    status = normalize_status(data.get("status"))

    return {
        "paid": status == "PAID",
        "status": status,
        "raw_status": str(data.get("status") or "").lower(),
        "trx_id": str(data.get("transaction_id") or identifier),
        "qris_id": str(data.get("transaction_id") or identifier),
        "amount": _to_int(data.get("amount")),
        "created_at": _parse_wib(data.get("created_at")),
        "channel": str(data.get("type") or "").upper(),
        "transaction": data,
    }


async def balance() -> dict:
    body = await _request("/merchantbalance", {"key": _store_key()})
    data = body.get("data") or {}
    return {
        "store_name": data.get("store_name"),
        "balance": _to_int(data.get("balance")),
        "store_fee": data.get("store_fee"),
        "level": data.get("level"),
        "status": data.get("status"),
    }


def verify_webhook(raw_body: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False
    try:
        expected = hmac.new(
            _store_key().encode("utf-8"),
            raw_body if isinstance(raw_body, (bytes, bytearray)) else str(raw_body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    except NasionalPayError:
        return False
    return hmac.compare_digest(expected, str(signature_header).strip().lower())
