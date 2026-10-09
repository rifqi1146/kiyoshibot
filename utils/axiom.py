"""Axiom Payment QRIS client (https://portal.axiomcreative.xyz/docs).

FLOW SCRAPER / API:
1. `create_qris(amount)` -> POST /create-qris with header `x-api-key`.
   Returns the invoice: qris_id, trx_id, amount, fee_amount, net_amount,
   status, created_at, expires_at, qr_url, qris_string.
2. `check_payment(qris_id=None, trx_id=None)` -> POST /check-payment.
   Response shape: {success, paid, status, data, transaction?}.
3. `payment_status(qris_id)` -> GET /api/qr-status/<qris_id> (public, no key).
   Used as read-only fallback when check-payment fails (429/503/network).

Rules from the docs that this client enforces:
- The API key never leaves the backend (only sent as `x-api-key` header).
- `amount` must be an integer 1..10_000_000.
- Never treat a failed check as "unpaid": on 429/503/network the caller keeps
  the last known status, so each function raises instead of returning a
  fabricated PENDING.
- The provider decides QR expiry; we always read `expires_at` from the response.

RETRY:
The Axiom origin sits behind a load balancer whose backends flap: a request can
hit an edge node whose upstream origin fails the TLS handshake and returns
HTTP 525 while the next request on the same host returns 200 (measured ~17/30
success on a single burst). Cloudflare 520/521/522/523/524/525/526 and 503 are
therefore retried with a short backoff before the error is surfaced.
"""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

import aiohttp

from utils.config import AXIOM_API_KEY, AXIOM_BASE_URL

log = logging.getLogger(__name__)

CREATE_TIMEOUT = 20
CHECK_TIMEOUT = 15

MIN_AMOUNT = 1
MAX_AMOUNT = 10_000_000

# Gateway origin failures worth retrying (Cloudflare edge <-> origin errors).
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526}
MAX_ATTEMPTS = 5
BASE_BACKOFF = 0.4


class AxiomError(RuntimeError):
    """Raised when the gateway cannot be reached or answers with an error."""

    def __init__(self, message: str, *, status: int | None = None, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


def configured() -> bool:
    return bool(AXIOM_API_KEY)


def _headers() -> dict:
    if not AXIOM_API_KEY:
        raise AxiomError("AXIOM_API_KEY is not set.")
    return {"x-api-key": AXIOM_API_KEY, "Accept": "application/json"}


def _parse_iso(value) -> float | None:
    """Parse an ISO 8601 timestamp (e.g. 2026-10-08T12:15:00.000Z) to epoch."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


async def _request(
    method: str,
    path: str,
    *,
    json_body=None,
    timeout: int = CHECK_TIMEOUT,
    attempts: int = MAX_ATTEMPTS,
) -> dict:
    url = f"{AXIOM_BASE_URL}{path}"
    headers = _headers()
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    last_err: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    status = resp.status
                    text = await resp.text()

                    # Cloudflare edge-to-origin flapping (525/520/522/503/etc) or 429 rate limit
                    if status in RETRY_STATUSES:
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else (
                            BASE_BACKOFF * (1.6 ** (attempt - 1)) + random.uniform(0.1, 0.3)
                        )
                        last_err = AxiomError(
                            f"Axiom gateway unavailable (HTTP {status}).",
                            status=status,
                        )
                        if attempt < attempts:
                            log.info(
                                "Axiom gateway HTTP %s, retrying (%d/%d) in %.2fs | path=%s",
                                status, attempt, attempts, delay, path,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise last_err

                    if status >= 500:
                        raise AxiomError(
                            f"Axiom gateway unavailable (HTTP {status}).",
                            status=status,
                        )

                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        raise AxiomError(
                            f"Unexpected Axiom response (HTTP {status}): {text[:200]}",
                            status=status,
                        )

                    if status >= 400:
                        raise AxiomError(
                            str(data.get("message") or f"Axiom error HTTP {status}"),
                            status=status,
                            data=data.get("data") or {},
                        )
                    return data
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last_err = AxiomError(f"Axiom network error: {e}")
            if attempt < attempts:
                delay = BASE_BACKOFF * (1.6 ** (attempt - 1)) + random.uniform(0.1, 0.3)
                log.info(
                    "Axiom network error (%s), retrying (%d/%d) in %.2fs",
                    e, attempt, attempts, delay,
                )
                await asyncio.sleep(delay)
                continue
            raise last_err

    raise last_err or AxiomError("Axiom request failed after retries.")


async def create_qris(amount: int) -> dict:
    """Create a QRIS invoice. Returns a normalized dict (never a raw body)."""
    try:
        amount = int(amount)
    except Exception:
        raise AxiomError("Invalid amount.")
    if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
        raise AxiomError(f"Amount must be between {MIN_AMOUNT} and {MAX_AMOUNT}.")

    body = await _request("POST", "/create-qris", json_body={"amount": amount}, timeout=CREATE_TIMEOUT)

    if not body.get("success"):
        raise AxiomError(str(body.get("message") or "Failed to create QRIS."), data=body.get("data") or {})

    data = body.get("data") or {}
    qris_id = data.get("qris_id")
    qr_url = data.get("qr_url") or (f"{AXIOM_BASE_URL}/qr/{qris_id}" if qris_id else None)
    if not qris_id or not qr_url:
        raise AxiomError("Axiom did not return qris_id/qr_url.", data=data)

    created_at = _parse_iso(data.get("created_at")) or time.time()
    return {
        "qris_id": qris_id,
        "trx_id": data.get("trx_id"),
        "amount": int(data.get("amount") or amount),
        "fee_rate_bps": int(data.get("fee_rate_bps") or 0),
        "fee_amount": int(data.get("fee_amount") or 0),
        "net_amount": int(data.get("net_amount") or 0),
        "status": str(data.get("status") or "PENDING").upper(),
        "created_at": created_at,
        "expires_at": _parse_iso(data.get("expires_at")),
        "qr_url": qr_url,
        "qris_string": data.get("qris_string"),
    }


async def check_payment(
    qris_id: str | None = None,
    trx_id: str | None = None,
    *,
    attempts: int = 3,
) -> dict:
    """POST /check-payment -> {paid, status, data, transaction?}.

    Raises AxiomError on 429/503/network. Callers must keep the last known
    status when this raises, never assume the user did not pay.
    """
    if not qris_id and not trx_id:
        raise AxiomError("check_payment needs qris_id or trx_id.")

    payload = {}
    if qris_id:
        payload["qris_id"] = qris_id
    if trx_id:
        payload["trx_id"] = trx_id

    body = await _request("POST", "/check-payment", json_body=payload, attempts=attempts)

    if not body.get("success"):
        raise AxiomError(str(body.get("message") or "Failed to check payment."))

    data = body.get("data") or {}
    return {
        "paid": bool(body.get("paid")),
        "status": str(body.get("status") or data.get("status") or "PENDING").upper(),
        "qris_id": data.get("qris_id") or qris_id,
        "trx_id": data.get("trx_id") or trx_id,
        "amount": int(data.get("amount") or 0),
        "net_amount": int(data.get("net_amount") or 0),
        "expires_at": _parse_iso(data.get("expires_at")),
        "qr_url": data.get("qr_url"),
        "transaction": body.get("transaction"),
    }


async def payment_status(qris_id: str) -> dict:
    """GET /api/qr-status/<qris_id> -> {paid, status, expires_at}.

    Public read-only endpoint (no API key). Used only to cross-check when the
    authenticated check fails. Raises AxiomError on 404/503/network.
    """
    body = await _request("GET", f"/api/qr-status/{qris_id}", timeout=CHECK_TIMEOUT)
    if not body.get("success"):
        raise AxiomError(str(body.get("message") or "Failed to read payment status."))
    data = body.get("data") or {}
    return {
        "paid": bool(body.get("paid")),
        "status": str(body.get("status") or data.get("status") or "PENDING").upper(),
        "expires_at": _parse_iso(data.get("expires_at")),
    }
