"""Thin Paystack client — provider I/O only, no database, no models.

Deliberately stdlib (urllib): the surface is small, and the dependency
list stays honest. Amounts are integer kobo, which is exactly Paystack's
unit — no conversion, no floats, no rounding.

Propagates PaystackError for every failure mode (missing key, network,
HTTP error, business rejection) so callers have one exception to catch
and can decide the business consequence themselves. The HTTP status is
preserved because callers treat 409 (duplicate refund reference)
differently from a 500.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

API_BASE = "https://api.paystack.co"


class PaystackError(Exception):
    """Any failure talking to Paystack, or a rejected transaction."""

    def __init__(self, message, *, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def _request(method, path, payload=None, timeout=10) -> dict:
    """One HTTP round trip. Returns Paystack's `data` field."""
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaystackError("PAYSTACK_SECRET_KEY is not configured.")

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            error_payload = {}
        raise PaystackError(
            f"Paystack HTTP {exc.code} on {path}",
            status_code=exc.code,
            payload=error_payload,
        ) from exc
    except urllib.error.URLError as exc:
        raise PaystackError(f"Paystack unreachable: {exc.reason}") from exc

    if not parsed.get("status"):
        raise PaystackError(
            f"Paystack rejected the request: {parsed.get('message', 'unknown error')}",
            payload=parsed,
        )
    return parsed["data"]


def initialize_transaction(
    *, email, amount_kobo, reference, metadata=None, callback_url=None, timeout=10
) -> dict:
    """Start a charge. Returns Paystack's data dict (authorization_url etc.)."""
    payload = {
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "metadata": metadata or {},
    }
    if callback_url:
        # Where Paystack sends the customer after paying. It appends
        # ?trxref=...&reference=... to this URL.
        payload["callback_url"] = callback_url
    return _request("POST", "/transaction/initialize", payload, timeout=timeout)


def verify_transaction(reference, timeout=10) -> dict:
    """Fetch the authoritative state of a charge from Paystack.

    The webhook is the primary signal, but it can be delayed or dropped.
    The return page and the reconciliation task call this to close that
    gap. Returns Paystack's `data` dict; `data["status"]` is one of
    success / failed / abandoned / ongoing / pending / processing / queued.
    """
    return _request("GET", f"/transaction/verify/{urllib.parse.quote(reference, safe='')}", timeout=timeout)


def create_refund(*, transaction_reference, amount_kobo, reference, timeout=10) -> dict:
    """Queue a refund of a successful charge.

    `reference` is ours and is unique per refund attempt. Paystack rejects
    a reused reference with 409; in that case the refund already exists
    (e.g. a retry after a timeout), so fetch and return it instead of
    treating the call as failed.
    """
    try:
        return _request(
            "POST",
            "/refund",
            {
                "transaction": transaction_reference,
                "amount": amount_kobo,
                "reference": reference,
            },
            timeout=timeout,
        )
    except PaystackError as exc:
        if exc.status_code != 409:
            raise
        existing = _request(
            "GET",
            f"/refund?reference={urllib.parse.quote(reference)}",
            timeout=timeout,
        )
        for item in existing or []:
            if item.get("reference") == reference:
                return item
        raise
