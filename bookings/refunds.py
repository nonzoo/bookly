"""Deposit refunds.

A refund is tracked as its own Payment(kind=REFUND) row. The original
deposit row stays SUCCESS until Paystack confirms the refund, then becomes
REFUNDED. Until `refund.processed` arrives the money is still held, so no
ledger debit exists yet.

Two boundaries:
- build_refund_payment() is DB-only and runs inside the caller's
  transaction (webhook processing, cancellation service).
- send_refund() performs the HTTP call. It must run after the enclosing
  transaction commits; network I/O never happens while row locks are held.

Idempotency: every refund carries our unique reference. Paystack rejects a
reused reference with 409, and create_refund() fetches the existing refund
instead, so retries after a timeout are safe.
"""
import uuid

from django.db import transaction
from django.utils import timezone

from . import ledger, paystack
from .models import Booking, Payment
from .paystack import PaystackError


class RefundError(Exception):
    """A refund could not be requested or reconciled."""


def _reference():
    return f"rf_{uuid.uuid4().hex}"


def _deposit_for(booking):
    return Payment.objects.filter(booking=booking, kind=Payment.Kind.DEPOSIT).order_by("-id").first()


def build_refund_payment(booking, *, amount_kobo=None) -> Payment:
    """Create (or return) the refund row for a booking's successful deposit.

    The caller must already hold the booking lock. One refund row per
    booking in v1: a second request returns the first instead of creating
    a duplicate refund.
    """
    deposit = _deposit_for(booking)
    if deposit is None or deposit.status != Payment.Status.SUCCESS:
        raise RefundError("There is no successful deposit to refund.")

    amount = deposit.amount_kobo if amount_kobo is None else amount_kobo
    if amount <= 0 or amount > deposit.amount_kobo:
        raise RefundError("Refund amount must be between 1 kobo and the deposit.")

    existing = Payment.objects.filter(booking=booking, kind=Payment.Kind.REFUND).order_by("-id").first()
    if existing is not None:
        if existing.amount_kobo != amount:
            raise RefundError("A different refund already exists for this booking.")
        return existing

    return Payment.objects.create(
        booking=booking,
        kind=Payment.Kind.REFUND,
        reference=_reference(),
        amount_kobo=amount,
        status=Payment.Status.INITIATED,
    )


def send_refund(refund_payment_id) -> Payment:
    """Request the refund from Paystack. Call AFTER the DB transaction commits."""
    refund = Payment.objects.select_related("booking").get(pk=refund_payment_id)
    if refund.kind != Payment.Kind.REFUND:
        raise RefundError("That payment is not a refund.")
    if refund.status != Payment.Status.INITIATED:
        return refund

    deposit = _deposit_for(refund.booking)
    if deposit is None:
        raise RefundError("The original deposit no longer exists.")

    try:
        data = paystack.create_refund(
            transaction_reference=deposit.reference,
            amount_kobo=refund.amount_kobo,
            reference=refund.reference,
        )
    except PaystackError as exc:
        Payment.objects.filter(pk=refund.pk).update(
            paystack_response={"error": str(exc), "status_code": exc.status_code}
        )
        raise RefundError("Paystack could not queue the refund; it will need a retry.") from exc

    status = (data or {}).get("status")
    if status == "processed":
        return mark_refund_processed(
            refund.pk, amount_kobo=refund.amount_kobo, provider_data=data
        )
    if status == "failed":
        return mark_refund_failed(refund.pk, provider_data=data)

    record_refund_progress(refund.pk, provider_data=data)
    return Payment.objects.get(pk=refund.pk)


def mark_refund_processed(refund_payment_id, *, amount_kobo=None, provider_data=None) -> Payment:
    """Move the money: refund row SUCCESS, deposit REFUNDED, ledger debit.

    Locks booking first, then payments — the same order every service uses.
    """
    with transaction.atomic():
        booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=refund_payment_id)
        booking = Booking.objects.select_for_update().get(pk=booking_id)
        refund = Payment.objects.select_for_update().get(pk=refund_payment_id)

        if refund.kind != Payment.Kind.REFUND:
            raise RefundError("That payment is not a refund.")
        if amount_kobo is not None and amount_kobo != refund.amount_kobo:
            raise RefundError("Refund amount does not match the requested amount.")
        if refund.status == Payment.Status.SUCCESS:
            return refund

        deposit = Payment.objects.select_for_update().filter(
            booking=booking, kind=Payment.Kind.DEPOSIT
        ).order_by("-id").first()

        refund.status = Payment.Status.SUCCESS
        refund.paid_at = refund.paid_at or timezone.now()
        if provider_data is not None:
            refund.paystack_response = provider_data
        refund.save(update_fields=["status", "paid_at", "paystack_response"])

        if deposit is not None and deposit.status == Payment.Status.SUCCESS:
            deposit.status = Payment.Status.REFUNDED
            deposit.save(update_fields=["status"])

        ledger.record_refund(refund, booking)

    return refund


def mark_refund_failed(refund_payment_id, *, provider_data=None) -> Payment:
    """Paystack says the refund failed; the deposit stays held."""
    with transaction.atomic():
        booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=refund_payment_id)
        Booking.objects.select_for_update().get(pk=booking_id)
        refund = Payment.objects.select_for_update().get(pk=refund_payment_id)

        if refund.kind != Payment.Kind.REFUND:
            raise RefundError("That payment is not a refund.")
        if refund.status == Payment.Status.SUCCESS:
            return refund  # processed wins over a late failed event

        refund.status = Payment.Status.FAILED
        if provider_data is not None:
            refund.paystack_response = provider_data
        refund.save(update_fields=["status", "paystack_response"])

    return refund


def record_refund_progress(refund_payment_id, *, provider_data=None) -> Payment:
    """Store pending/processing/needs-attention without changing status."""
    with transaction.atomic():
        booking_id = Payment.objects.values_list("booking_id", flat=True).get(pk=refund_payment_id)
        Booking.objects.select_for_update().get(pk=booking_id)
        refund = Payment.objects.select_for_update().get(pk=refund_payment_id)

        if refund.kind != Payment.Kind.REFUND:
            raise RefundError("That payment is not a refund.")
        if refund.status != Payment.Status.SUCCESS:
            refund.paystack_response = provider_data or {}
            refund.save(update_fields=["paystack_response"])

    return refund
