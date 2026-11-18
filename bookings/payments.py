"""Payment records, provider calls and applying a confirmed charge.

Split of responsibilities:
- build_deposit_payment() creates the Payment row. Called INSIDE the hold
  transaction, because the row belongs to the hold atomically.
- request_deposit() performs the Paystack network call. It must run
  AFTER the hold transaction commits — remote I/O never happens while
  database locks are held.
- lock_deposit_by_reference() and apply_charge_success() are the ONE
  implementation of "the money arrived". The webhook handler and the
  verify/reconciliation path both call them, so a charge cannot be
  applied by two slightly different code paths.

verify_and_apply() is the recovery path for a delayed or dropped webhook:
ask Paystack for the authoritative state, then apply it under the same
locks and the same global order (session -> booking -> payment).
"""
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import ledger, paystack, refunds
from .constraints import is_exclusion_violation
from .models import Booking, ClassSession, Payment

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """Base class for payment-state problems."""


class PaymentNotFound(PaymentError):
    """No deposit payment exists for the given reference."""


class AmountMismatch(PaymentError):
    """The provider says a different amount was paid than we requested."""

    def __init__(self, expected_kobo, received_kobo):
        self.expected_kobo = expected_kobo
        self.received_kobo = received_kobo
        super().__init__(
            f"Amount mismatch: expected {expected_kobo}, got {received_kobo}."
        )


def _reference() -> str:
    # Unique by construction (uuid4); Payment.reference's unique
    # constraint is the backstop if entropy ever fails.
    return f"bk_{uuid.uuid4().hex}"


def build_deposit_payment(booking) -> Payment:
    """Create the deposit Payment row for a booking. DB-only, no network."""
    return Payment.objects.create(
        booking=booking,
        kind=Payment.Kind.DEPOSIT,
        reference=_reference(),
        amount_kobo=booking.deposit_kobo,
    )


def request_deposit(payment) -> dict:
    """Initialize the deposit with Paystack and store the provider response.

    Raises paystack.PaystackError on any provider failure; the caller
    decides how to unwind the booking.
    """
    options = {"metadata": {"booking_id": str(payment.booking_id)}}
    if settings.PAYSTACK_CALLBACK_URL:
        # Only sent when configured: an empty callback_url is not the same
        # as "no callback" to Paystack.
        options["callback_url"] = settings.PAYSTACK_CALLBACK_URL

    data = paystack.initialize_transaction(
        email=payment.booking.customer.email,
        amount_kobo=payment.amount_kobo,
        reference=payment.reference,
        **options,
    )
    payment.paystack_response = data
    payment.save(update_fields=["paystack_response"])
    return data


# --------------------------------------------------------------------------
# Shared charge application (webhook + verify)
# --------------------------------------------------------------------------
def lock_deposit_by_reference(reference):
    """Lock the booking and its deposit, in the global order.

    Session first (group bookings), then booking, then payment. lifecycle's
    cancel_session holds the session lock while cancelling its bookings, so
    every path that touches seats must agree on this order.
    """
    snapshot = (
        Payment.objects.filter(reference=reference, kind=Payment.Kind.DEPOSIT)
        .values("id", "booking_id")
        .first()
    )
    if snapshot is None:
        raise PaymentNotFound(f"No deposit payment for reference {reference}.")

    session_id = Booking.objects.values_list("session_id", flat=True).get(
        pk=snapshot["booking_id"]
    )
    if session_id is not None:
        ClassSession.objects.select_for_update().get(pk=session_id)

    booking = Booking.objects.select_for_update().get(pk=snapshot["booking_id"])
    payment = Payment.objects.select_for_update().get(pk=snapshot["id"])
    return booking, payment


def apply_charge_success(booking, payment, provider_data) -> Payment:
    """Mark the deposit paid, credit the ledger and move the booking.

    The caller must already hold the locks from lock_deposit_by_reference.
    Idempotent: a repeated delivery returns without a second ledger row,
    and a deposit whose refund already completed stays REFUNDED.
    """
    if payment.status in (Payment.Status.SUCCESS, Payment.Status.REFUNDED):
        return payment

    amount = _amount(provider_data)
    if amount != payment.amount_kobo:
        raise AmountMismatch(payment.amount_kobo, amount)

    payment.status = Payment.Status.SUCCESS
    payment.paid_at = timezone.now()
    payment.paystack_response = provider_data
    payment.save(update_fields=["status", "paid_at", "paystack_response"])

    ledger.credit_deposit(payment, booking)
    _confirm_or_refund(booking, payment)
    return payment


def mark_charge_failed(booking, payment, provider_data) -> Payment:
    """A charge definitively failed: expire the hold and free its seat.

    A late failure never undoes a successful or refunded charge — those are
    the provider's racing webhooks, and success wins.
    """
    if payment.status in (Payment.Status.SUCCESS, Payment.Status.REFUNDED):
        return payment

    payment.status = Payment.Status.FAILED
    payment.paystack_response = provider_data
    payment.save(update_fields=["status", "paystack_response"])

    if booking.status == Booking.Status.PENDING_PAYMENT:
        booking.status = Booking.Status.EXPIRED
        booking.save()
        if booking.session_id:
            ClassSession.release_seats(booking.session_id, booking.party_size)
    return payment


def verify_and_apply(payment_id) -> Payment:
    """Ask Paystack for the true state, then apply it. Recovery path.

    Network I/O happens before any lock. Only terminal states are applied;
    pending/ongoing is left alone for the next attempt.
    """
    snapshot = (
        Payment.objects.filter(pk=payment_id, kind=Payment.Kind.DEPOSIT)
        .values("reference")
        .first()
    )
    if snapshot is None:
        raise PaymentNotFound(f"Payment {payment_id} is not a deposit.")

    data = paystack.verify_transaction(snapshot["reference"])
    status = (data or {}).get("status")

    if status not in {"success", "failed", "abandoned"}:
        return Payment.objects.get(pk=payment_id)

    with transaction.atomic():
        booking, payment = lock_deposit_by_reference(snapshot["reference"])
        if status == "success":
            apply_charge_success(booking, payment, data)
        else:
            mark_charge_failed(booking, payment, data)
    return payment


# --------------------------------------------------------------------------
# Booking consequences of a successful charge
# --------------------------------------------------------------------------
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
    """Create the refund row; send it once the enclosing transaction commits."""
    try:
        refunds.queue_refund(booking)
    except refunds.RefundError:
        # The deposit is real money the platform holds but cannot return
        # automatically. Leave the payment SUCCESS and let ops reconcile;
        # raising would roll back the confirmation and lose the payment
        # record, which is worse.
        logger.exception("Could not queue refund for booking %s", booking.pk)


def _amount(provider_data):
    """Integer kobo from a provider payload, or None if absent/invalid."""
    value = (provider_data or {}).get("amount")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pending_deposit_ids(older_than_minutes=10, limit=100):
    """Payments worth re-checking: still INITIATED after the grace period."""
    cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
    return list(
        Payment.objects.filter(
            kind=Payment.Kind.DEPOSIT,
            status=Payment.Status.INITIATED,
            created_at__lte=cutoff,
        )
        .order_by("created_at")
        .values_list("pk", flat=True)[:limit]
    )
