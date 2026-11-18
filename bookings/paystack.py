"""Thin Paystack client — provider I/O only, no database, no models.

Deliberately stdlib (urllib): the surface is one endpoint today, and the
dependency list stays honest. Amounts are integer kobo, which is exactly
Paystack's unit — no conversion, no floats, no rounding.

Propagates PaystackError for every failure mode (missing key, network,
HTTP error, business rejection) so callers have one exception to catch
and can decide the business consequence themselves.
"""
import json
import urllib.error
import urllib.request

from django.conf import settings

API_BASE = "https://api.paystack.co"


class PaystackError(Exception):
    """Any failure talking to Paystack, or a rejected transaction."""


def initialize_transaction(*, email, amount_kobo, reference, metadata=None, timeout=10) -> dict:
    """Start a charge. Returns Paystack's data dict (authorization_url etc.)."""
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaystackError("PAYSTACK_SECRET_KEY is not configured.")

    payload = {
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "metadata": metadata or {},
    }
    request = urllib.request.Request(
        f"{API_BASE}/transaction/initialize",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PaystackError(f"Paystack HTTP {exc.code} on initialize") from exc
    except urllib.error.URLError as exc:
        raise PaystackError(f"Paystack unreachable: {exc.reason}") from exc

    if not body.get("status"):
        raise PaystackError(f"Paystack rejected the transaction: {body.get('message', 'unknown error')}")
    return body["data"]
