"""Booking lifecycle: customer/business cancellation, no-show, completion.

Policy (locked design):
- Customer cancels with at least `free_cancellation_hours` notice -> full
  deposit refund.
- Customer cancels late -> deposit forfeited. The ledger gets a 0-kobo
  `deposit_forfeited` marker; the `deposit_received` credit stays, because
  the money now belongs to the business.
- Business cancels -> full refund regardless of timing.
- No-show -> forfeit (same zero-kobo marker), release group seats.
- Completed -> no money movement; the deposit remains held for payout.

Every transition locks session (if group) -> booking -> payment, the same
order as holds/webhooks, and releases group seats exactly once.

Refunds are created inside the transaction but sent on_commit, so no
network call ever runs while row locks are held.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import ledger, refunds
from .models import Booking, ClassSession, LedgerEntry, Payment


class LifecycleError(Exception):
    """Base class for invalid booking transitions."""


class InvalidTransition(LifecycleError):
    """The booking's current state does not allow the requested action."""


@dataclass
class CancellationResult:
    booking: Booking
    refund: Payment | None = None
    forfeited: bool = False


def cancel_by_customer(booking) -> CancellationResult:
    """Cancel at the customer's request, applying the notice policy."""
    with transaction.atomic():
        booking = _lock_booking(booking.pk)
        _reject_blocker(booking)

        if booking.status in (Booking.Status.CANCELLED_BY_CUSTOMER, Booking.Status.CANCELLED_BY_BUSINESS):
            return _existing_result(booking)
        _require_status(booking, (Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED))

        now = timezone.now()
        deadline = booking.start_at - timedelta(hours=booking.free_cancellation_hours)
        free = now <= deadline
        # An unpaid hold can always be abandoned; if a charge somehow
        # already succeeded, the money goes back regardless of notice.
        refund_ok = booking.status == Booking.Status.PENDING_PAYMENT or free
        return _cancel_locked(
            booking,
            status=Booking.Status.CANCELLED_BY_CUSTOMER,
            now=now,
            refund_ok=refund_ok,
        )


def cancel_by_business(booking) -> CancellationResult:
    """Cancel on the business's initiative: always refund a paid deposit."""
    with transaction.atomic():
        booking = _lock_booking(booking.pk)
        _reject_blocker(booking)

        if booking.status in (Booking.Status.CANCELLED_BY_CUSTOMER, Booking.Status.CANCELLED_BY_BUSINESS):
            return _existing_result(booking)
        _require_status(booking, (Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED))

        return _cancel_locked(
            booking,
            status=Booking.Status.CANCELLED_BY_BUSINESS,
            now=timezone.now(),
            refund_ok=True,
        )


def mark_no_show(booking) -> Booking:
    """After the start time, forfeit the deposit and free the group seat."""
    with transaction.atomic():
        booking = _lock_booking(booking.pk)
        _reject_blocker(booking)
        _require_status(booking, (Booking.Status.CONFIRMED,))

        now = timezone.now()
        if booking.start_at > now:
            raise InvalidTransition("A no-show cannot be recorded before the start time.")

        booking.status = Booking.Status.NO_SHOW
        booking.cancelled_at = now
        booking.save()
        _release_group_seats(booking)

        deposit = _lock_successful_deposit(booking)
        if deposit is not None:
            ledger.record_forfeit(deposit, booking)

    return booking


def mark_completed(booking) -> Booking:
    """After the end time, mark the service delivered. No money moves."""
    with transaction.atomic():
        booking = _lock_booking(booking.pk)
        _reject_blocker(booking)
        _require_status(booking, (Booking.Status.CONFIRMED,))

        if booking.end_at > timezone.now():
            raise InvalidTransition("A booking cannot be completed before it ends.")

        booking.status = Booking.Status.COMPLETED
        booking.save()

    return booking


def cancel_session(session) -> list[CancellationResult]:
    """Cancel a class: blocker, every active booking, seats and refunds.

    The session row is locked first, so no new booking can slip in while
    its seats are being released. Each customer booking is then cancelled
    under the business-cancellation policy (full refund).
    """
    with transaction.atomic():
        session = ClassSession.objects.select_for_update().select_related(
            "service__business", "resource"
        ).get(pk=session.pk)

        if session.is_cancelled:
            return []

        now = timezone.now()
        session.is_cancelled = True
        session.save(update_fields=["is_cancelled"])

        blocker = Booking.objects.select_for_update().filter(
            session=session,
            is_blocker=True,
            status__in=Booking.ACTIVE_STATUSES,
        ).first()
        if blocker is not None:
            blocker.status = Booking.Status.CANCELLED_BY_BUSINESS
            blocker.cancelled_at = now
            blocker.save()

        customer_bookings = list(
            Booking.objects.select_for_update().filter(
                session=session,
                is_blocker=False,
                status__in=Booking.ACTIVE_STATUSES,
            )
        )

        results = [
            _cancel_locked(
                booking,
                status=Booking.Status.CANCELLED_BY_BUSINESS,
                now=now,
                refund_ok=True,
            )
            for booking in customer_bookings
        ]

    return results


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------
def _lock_booking(booking_id):
    """Lock session first (group), then the booking — the global order."""
    snapshot = Booking.objects.values("session_id").get(pk=booking_id)
    if snapshot["session_id"] is not None:
        ClassSession.objects.select_for_update().get(pk=snapshot["session_id"])
    # `of=("self",)` locks only the booking row: business/service are
    # read-only references, and "session" is nullable, so joining it would
    # make PostgreSQL reject FOR UPDATE on the outer join's nullable side.
    # The session row is already locked above when it exists.
    return Booking.objects.select_for_update(of=("self",)).select_related(
        "business", "service"
    ).get(pk=booking_id)


def _cancel_locked(booking, *, status, now, refund_ok) -> CancellationResult:
    booking.status = status
    booking.cancelled_at = now
    booking.save()
    _release_group_seats(booking)

    deposit = _lock_successful_deposit(booking)
    if deposit is None:
        return CancellationResult(booking=booking)

    if refund_ok:
        return CancellationResult(booking=booking, refund=refunds.queue_refund(booking))

    ledger.record_forfeit(deposit, booking)
    return CancellationResult(booking=booking, forfeited=True)


def _release_group_seats(booking):
    if booking.session_id:
        ClassSession.release_seats(booking.session_id, booking.party_size)


def _lock_successful_deposit(booking):
    return Payment.objects.select_for_update().filter(
        booking=booking,
        kind=Payment.Kind.DEPOSIT,
        status=Payment.Status.SUCCESS,
    ).order_by("-id").first()


def _existing_result(booking) -> CancellationResult:
    refund = Payment.objects.filter(
        booking=booking, kind=Payment.Kind.REFUND
    ).order_by("-id").first()
    forfeited = LedgerEntry.objects.filter(
        booking=booking, entry_type=LedgerEntry.Type.DEPOSIT_FORFEITED
    ).exists()
    return CancellationResult(booking=booking, refund=refund, forfeited=forfeited)


def _require_status(booking, allowed):
    if booking.status not in allowed:
        raise InvalidTransition(
            f"Cannot change a booking in status '{booking.status}' that way."
        )


def _reject_blocker(booking):
    if booking.is_blocker:
        raise InvalidTransition("Blocker rows are managed together with their session.")
