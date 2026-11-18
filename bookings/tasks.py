"""Celery tasks: the global janitor behind the booking lifecycle.

The sweep inside create_hold only helps the resource (or session) being
booked at that moment. This task is the slow, global net: every beat tick
it expires unpaid holds whose deadline passed and releases the seats they
were sitting on. Without it, a customer who closes the payment tab would
block a one-to-one slot (exclusion constraint) or group seats forever.

Concurrency contract, identical to every other seat-touching service:
- lock order is session -> booking. A booking's session row is locked
  first, so the sweep can never deadlock with create_hold, webhooks or
  lifecycle.
- after locking, the row is re-checked: a hold that got paid between the
  candidate query and the lock is left alone.
- skip_locked=True: a row another transaction is working on (typically a
  webhook applying a charge) belongs to that transaction. The next tick
  retries it if it is still unpaid.
- seats are released only on the transition out of pending_payment, so
  re-running the task cannot over-release.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from . import payments, paystack, refunds
from .models import Booking, ClassSession, Payment

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
# Bounded work per tick: a large backlog drains over the next ticks
# instead of one worker hogging a connection for minutes.
MAX_PASSES = 20


@shared_task
def expire_overdue_holds(batch_size=BATCH_SIZE, max_passes=MAX_PASSES) -> int:
    """Expire lapsed unpaid holds, batch by batch. Returns the count expired."""
    now = timezone.now()
    total = 0

    for _ in range(max_passes):
        candidate_ids = list(
            Booking.objects.filter(
                status=Booking.Status.PENDING_PAYMENT,
                expires_at__lte=now,
            )
            .order_by("expires_at", "id")
            .values_list("id", flat=True)[:batch_size]
        )
        if not candidate_ids:
            break

        expired_this_pass = sum(
            1 for booking_id in candidate_ids if _expire_hold(booking_id, now)
        )
        total += expired_this_pass

        if expired_this_pass == 0:
            # Whatever is left is locked elsewhere (or was just resolved
            # by another transaction); let the next beat tick look again.
            break

    if total:
        logger.info("Expired %s overdue booking hold(s)", total)
    return total


def _expire_hold(booking_id, now) -> bool:
    """Expire one hold. True only when this call performed the transition."""
    # Read the session id without a lock: the lock must be session first.
    session_id = Booking.objects.values_list("session_id", flat=True).get(
        pk=booking_id
    )

    with transaction.atomic():
        if session_id is not None:
            # filter().first(): the session row cannot vanish (PROTECT),
            # but this avoids a hard error if it is mid-delete in an
            # exotic migration path.
            ClassSession.objects.select_for_update().filter(pk=session_id).first()

        booking = (
            Booking.objects.select_for_update(skip_locked=True)
            .filter(
                pk=booking_id,
                status=Booking.Status.PENDING_PAYMENT,
                expires_at__lte=now,
            )
            .first()
        )
        if booking is None:
            return False

        if booking.session_id:
            ClassSession.release_seats(booking.session_id, booking.party_size)

        booking.status = Booking.Status.EXPIRED
        booking.save()  # save() clears expires_at for non-pending states

    return True


# --------------------------------------------------------------------------
# Payment reconciliation
# --------------------------------------------------------------------------
@shared_task
def reconcile_pending_payments(older_than_minutes=10, limit=100) -> int:
    """Re-verify deposits whose webhook never arrived.

    The webhook is the primary confirmation, but delivery can be delayed
    or dropped. Every deposit still INITIATED after the grace period is
    checked with Paystack through the same apply path the webhook uses, so
    both routes converge on identical state.

    Only terminal provider states are applied; pending/ongoing stays
    INITIATED for the next tick.
    """
    settled = 0
    for payment_id in payments.pending_deposit_ids(older_than_minutes, limit):
        try:
            payment = payments.verify_and_apply(payment_id)
        except payments.AmountMismatch:
            # Real money discrepancy: never guess. Ops reconciles manually.
            logger.error("Amount mismatch while reconciling payment %s", payment_id)
            continue
        except payments.PaymentNotFound:
            logger.warning("Payment %s disappeared during reconciliation", payment_id)
            continue
        except paystack.PaystackError as exc:
            # Provider outage: leave the row alone for the next tick.
            logger.warning("Could not verify payment %s: %s", payment_id, exc)
            continue

        if payment.status != Payment.Status.INITIATED:
            settled += 1

    if settled:
        logger.info("Reconciled %s pending payment(s)", settled)
    return settled


@shared_task
def retry_stuck_refunds(older_than_minutes=15, limit=100) -> int:
    """Re-send refunds that never reached Paystack.

    queue_refund sends on commit; if that HTTP call fails the row stays
    INITIATED and nothing else retries it. create_refund is idempotent by
    reference (a reused reference returns the existing refund), so a
    retry can never double-refund.
    """
    cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
    refund_ids = list(
        Payment.objects.filter(
            kind=Payment.Kind.REFUND,
            status=Payment.Status.INITIATED,
            created_at__lte=cutoff,
        )
        .order_by("created_at")
        .values_list("pk", flat=True)[:limit]
    )

    resolved = 0
    for refund_id in refund_ids:
        try:
            refund = refunds.send_refund(refund_id)
        except refunds.RefundError as exc:
            logger.warning("Refund %s is still stuck: %s", refund_id, exc)
            continue
        if refund.status != Payment.Status.INITIATED:
            resolved += 1

    if resolved:
        logger.info("Moved %s stuck refund(s) to a terminal state", resolved)
    return resolved
