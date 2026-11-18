"""Inbound Paystack webhooks: store first, verify, process exactly once.

Pipeline:
1. Parse the JSON body (for the event key only).
2. Persist the WebhookEvent BEFORE processing. The unique
   (provider, event_key) constraint is the dedup gate.
3. Verify the signature over the RAW bytes. Invalid events stay stored
   for audit but are never processed.
4. Process inline under SELECT ... FOR UPDATE on the event row, so two
   simultaneous deliveries of the same event serialize: the second sees
   processed_at and returns.

Handlers own the financial state transitions. They use the shared lock
order booking -> payment, and the ledger helpers are idempotent, so a
retry after a crash cannot double-credit.

Failure policy:
- RetryableWebhookError (e.g. payment row not visible yet) leaves the
  event unprocessed; the view returns 500 and Paystack retries.
- PermanentWebhookError (e.g. amount mismatch) marks the event processed
  with an error note; the view returns 200 so Paystack stops retrying.
- Unknown event types are acknowledged and ignored.
"""
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import ledger, refunds
from .constraints import is_exclusion_violation, violated_constraint
from .models import Booking, ClassSession, Payment, WebhookEvent

logger = logging.getLogger(__name__)

PROVIDER = "paystack"
SIGNATURE_TOLERANCE_SECONDS = 300


class InvalidWebhook(Exception):
    """The request is not a JSON webhook we can store."""


class RetryableWebhookError(Exception):
    """Transient failure: leave the event unprocessed so Paystack retries."""


class PermanentWebhookError(Exception):
    """Permanent failure: acknowledge the event but record the problem."""


@dataclass
class WebhookResult:
    event: WebhookEvent
    http_status: int


# --------------------------------------------------------------------------
# Signature verification (raw bytes only)
# --------------------------------------------------------------------------
def verify_paystack_signature(raw_body: bytes, headers, *, now=None) -> bool:
    """Verify either Paystack signature scheme.

    - New: `Paystack-Signature: t=<unix>,v1=<whk_...>:<hex>`, HMAC-SHA512
      over b"<t>." + raw_body with PAYSTACK_WEBHOOK_SECRET. Timestamp must
      be within 300 seconds to stop replays.
    - Legacy: `x-paystack-signature`, HMAC-SHA512 of raw_body with
      PAYSTACK_SECRET_KEY (no timestamp).
    """
    current_time = int(time.time()) if now is None else now

    new_header = headers.get("Paystack-Signature") or headers.get("paystack-signature")
    if new_header and settings.PAYSTACK_WEBHOOK_SECRET:
        if _verify_timestamped(raw_body, new_header, settings.PAYSTACK_WEBHOOK_SECRET, current_time):
            return True

    legacy = headers.get("X-Paystack-Signature") or headers.get("x-paystack-signature")
    if legacy and settings.PAYSTACK_SECRET_KEY:
        expected = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
            raw_body,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected, legacy.strip().lower())

    return False


def _verify_timestamped(raw_body, header, secret, now) -> bool:
    timestamp = None
    signatures = []
    for part in header.split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if key == "t":
            if timestamp is not None or not value.isdigit():
                return False
            timestamp = int(value)
        elif key == "v1":
            _key_id, separator, signature = value.partition(":")
            if separator and signature:
                signatures.append(signature)

    if timestamp is None or not signatures:
        return False
    if abs(now - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        return False

    signed = str(timestamp).encode("ascii") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha512).hexdigest()
    return any(hmac.compare_digest(expected, signature.lower()) for signature in signatures)


# --------------------------------------------------------------------------
# Receive: store first, then process
# --------------------------------------------------------------------------
def receive(raw_body: bytes, headers) -> WebhookResult:
    payload = _parse_payload(raw_body)
    event_type = payload.get("event")
    if not isinstance(event_type, str) or not event_type:
        raise InvalidWebhook("Webhook has no event type.")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    signature_valid = verify_paystack_signature(raw_body, headers)
    event_key = _build_event_key(event_type, data, payload)

    try:
        # Savepoint: the duplicate-key IntegrityError must not poison an
        # enclosing transaction (tests and any caller-side atomic block).
        with transaction.atomic():
            event = WebhookEvent.objects.create(
                provider=PROVIDER,
                event_key=event_key,
                event_type=event_type,
                payload=payload,
                signature_valid=signature_valid,
            )
        created = True
    except IntegrityError as exc:
        if violated_constraint(exc) != "uniq_webhook_event":
            raise
        event = WebhookEvent.objects.get(provider=PROVIDER, event_key=event_key)
        created = False

    if not signature_valid:
        if not event.signature_valid:
            _set_error(event, "Invalid signature")
        return WebhookResult(event=event, http_status=401)

    if not created:
        if event.processed_at:
            return WebhookResult(event=event, http_status=200)
        if not event.signature_valid:
            # A valid retry after an invalid request pre-created this key.
            event.signature_valid = True
            event.error = ""
            event.save(update_fields=["signature_valid", "error"])

    event = process_event(event.pk)
    return WebhookResult(event=event, http_status=200 if event.processed_at else 500)


def process_event(event_id) -> WebhookEvent:
    """Process one stored event under its row lock."""
    with transaction.atomic():
        event = WebhookEvent.objects.select_for_update().get(pk=event_id)
        if event.processed_at or not event.signature_valid:
            return event

        handler = _HANDLERS.get(event.event_type)
        if handler is None:
            _mark_processed(event, error=f"ignored: {event.event_type}")
            return event

        try:
            # Savepoint: a handler failure rolls back only its own writes,
            # while the event row lock (and the error note below) survive.
            with transaction.atomic():
                handler(event)
                event.processed_at = timezone.now()
                event.error = ""
                event.save(update_fields=["processed_at", "error"])
        except PermanentWebhookError as exc:
            _mark_processed(event, error=str(exc))
            logger.warning("Permanent webhook failure %s: %s", event.event_key, exc)
        except Exception as exc:  # retryable or unexpected
            event.error = f"{type(exc).__name__}: {exc}"[:2000]
            WebhookEvent.objects.filter(pk=event.pk).update(error=event.error)
            logger.exception("Webhook processing failed %s", event.event_key)

    return event


def _mark_processed(event, *, error=""):
    event.processed_at = timezone.now()
    event.error = error[:2000]
    event.save(update_fields=["processed_at", "error"])


def _set_error(event, message):
    WebhookEvent.objects.filter(pk=event.pk).update(error=message[:2000])


# --------------------------------------------------------------------------
# Charge handlers
# --------------------------------------------------------------------------
def _handle_charge_success(event):
    data = event.payload.get("data") or {}
    reference = data.get("reference")
    if not reference:
        raise PermanentWebhookError("charge.success has no reference.")

    booking, payment = _lock_deposit(reference)
    if payment.status == Payment.Status.SUCCESS:
        return  # already applied; ledger is idempotent too
    if data.get("status") != "success":
        raise PermanentWebhookError("charge.success payload is not successful.")

    amount = _amount(data)
    if amount != payment.amount_kobo:
        raise PermanentWebhookError(
            f"Amount mismatch for {reference}: expected {payment.amount_kobo}, got {amount}."
        )

    payment.status = Payment.Status.SUCCESS
    payment.paid_at = timezone.now()
    payment.paystack_response = data
    payment.save(update_fields=["status", "paid_at", "paystack_response"])

    ledger.credit_deposit(payment, booking)
    _confirm_or_refund(booking, payment)


def _handle_charge_failed(event):
    data = event.payload.get("data") or {}
    reference = data.get("reference")
    if not reference:
        raise PermanentWebhookError("charge.failed has no reference.")

    booking, payment = _lock_deposit(reference)
    if payment.status in (Payment.Status.SUCCESS, Payment.Status.REFUNDED):
        return  # a late failure never undoes a successful charge

    payment.status = Payment.Status.FAILED
    payment.paystack_response = data
    payment.save(update_fields=["status", "paystack_response"])

    if booking.status == Booking.Status.PENDING_PAYMENT:
        booking.status = Booking.Status.EXPIRED
        booking.save()
        if booking.session_id:
            ClassSession.release_seats(booking.session_id, booking.party_size)


def _lock_deposit(reference):
    """Lock booking first, then its deposit payment (global lock order)."""
    snapshot = Payment.objects.filter(
        reference=reference, kind=Payment.Kind.DEPOSIT
    ).values("id", "booking_id").first()
    if snapshot is None:
        raise RetryableWebhookError(f"No deposit payment for reference {reference}.")

    # Session first for group bookings: lifecycle's cancel_session holds
    # the session lock while cancelling its bookings, and both must use
    # the same order or the two paths can deadlock.
    session_id = Booking.objects.values_list("session_id", flat=True).get(pk=snapshot["booking_id"])
    if session_id is not None:
        ClassSession.objects.select_for_update().get(pk=session_id)

    booking = Booking.objects.select_for_update().get(pk=snapshot["booking_id"])
    payment = Payment.objects.select_for_update().get(pk=snapshot["id"])
    return booking, payment


def _confirm_or_refund(booking, payment):
    now = timezone.now()

    if booking.status == Booking.Status.CONFIRMED:
        return

    if booking.status == Booking.Status.PENDING_PAYMENT:
        if booking.start_at <= now:
            _queue_refund(booking)
            return
        booking.status = Booking.Status.CONFIRMED
        booking.save()
        return

    if booking.status == Booking.Status.EXPIRED:
        if booking.start_at <= now:
            _queue_refund(booking)
            return
        if _restore_expired(booking):
            return
        _queue_refund(booking)
        return

    # Cancelled, no-show, completed: money arrived for a booking that is
    # no longer deliverable, so give it back.
    _queue_refund(booking)


def _restore_expired(booking) -> bool:
    """Try to turn an expired hold back into a confirmed booking.

    Group seats were released on expiry, so they must be reserved again;
    one-to-one bookings rely on the exclusion constraint to reject the
    restore when the slot has since been taken. The savepoint makes
    either failure leave no partial state.
    """
    try:
        with transaction.atomic():
            if booking.session_id:
                if not ClassSession.reserve_seats(booking.session_id, booking.party_size):
                    return False
            booking.status = Booking.Status.CONFIRMED
            booking.save()
        return True
    except IntegrityError as exc:
        if is_exclusion_violation(exc):
            return False
        raise


def _queue_refund(booking):
    """Create the refund row and send it after the webhook transaction commits."""
    try:
        refunds.queue_refund(booking)
    except refunds.RefundError as exc:
        raise PermanentWebhookError(f"Refund could not be created: {exc}") from exc


# --------------------------------------------------------------------------
# Refund handlers
# --------------------------------------------------------------------------
def _handle_refund_processed(event):
    data = event.payload.get("data") or {}
    refund_id = _resolve_refund_payment_id(data)
    try:
        refunds.mark_refund_processed(
            refund_id,
            amount_kobo=_amount(data),
            provider_data=data,
        )
    except refunds.RefundError as exc:
        raise PermanentWebhookError(str(exc)) from exc


def _handle_refund_failed(event):
    data = event.payload.get("data") or {}
    refund_id = _resolve_refund_payment_id(data)
    try:
        refunds.mark_refund_failed(refund_id, provider_data=data)
    except refunds.RefundError as exc:
        raise PermanentWebhookError(str(exc)) from exc


def _handle_refund_progress(event):
    data = event.payload.get("data") or {}
    refund_id = _resolve_refund_payment_id(data)
    try:
        refunds.record_refund_progress(refund_id, provider_data=data)
    except refunds.RefundError as exc:
        raise PermanentWebhookError(str(exc)) from exc


def _resolve_refund_payment_id(data):
    """Match a refund event to our Payment(kind=REFUND) row.

    Prefer the refund reference we sent; fall back to the original
    transaction reference, which is always present in refund events.
    """
    refund_reference = data.get("refund_reference")
    if refund_reference:
        refund_id = Payment.objects.filter(
            kind=Payment.Kind.REFUND, reference=refund_reference
        ).values_list("id", flat=True).first()
        if refund_id is not None:
            return refund_id

    transaction_reference = data.get("transaction_reference")
    if transaction_reference:
        booking_id = Payment.objects.filter(
            kind=Payment.Kind.DEPOSIT, reference=transaction_reference
        ).values_list("booking_id", flat=True).first()
        if booking_id is not None:
            refund_id = Payment.objects.filter(
                kind=Payment.Kind.REFUND, booking_id=booking_id
            ).order_by("-id").values_list("id", flat=True).first()
            if refund_id is not None:
                return refund_id

    raise RetryableWebhookError("No matching refund payment for this event.")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_payload(raw_body):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidWebhook("Body is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidWebhook("Body must be a JSON object.")
    return payload


def _build_event_key(event_type, data, payload) -> str:
    reference = None
    if event_type.startswith("refund."):
        reference = (
            data.get("refund_reference")
            or data.get("transaction_reference")
            or data.get("reference")
            or data.get("id")
        )
    elif event_type.startswith("transfer."):
        reference = data.get("reference") or data.get("transfer_code") or data.get("id")
    else:
        reference = data.get("reference") or data.get("id")

    if reference is None:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        reference = digest[:32]

    return f"{event_type}:{reference}"[:200]


def _amount(data):
    value = data.get("amount")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PermanentWebhookError("Webhook amount is not an integer.") from exc


_HANDLERS = {
    "charge.success": _handle_charge_success,
    "charge.failed": _handle_charge_failed,
    "refund.pending": _handle_refund_progress,
    "refund.processing": _handle_refund_progress,
    "refund.needs-attention": _handle_refund_progress,
    "refund.processed": _handle_refund_processed,
    "refund.failed": _handle_refund_failed,
}
