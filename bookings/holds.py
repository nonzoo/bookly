"""The booking hold service — the only way customer bookings are created.

Flow (both modes):

1. Validate business rules (party size, address, session linkage).
2. One transaction:
   a. sweep that resource's / session's lapsed unpaid holds, because an
      expired row the sweep task hasn't reached yet still blocks the
      exclusion constraint (or still sits on seats);
   b. insert the booking with price/deposit snapshotted from money.py;
   c. create the Payment row (skipped when the deposit is 0 kobo).
3. After COMMIT: initialize the deposit with Paystack. Network I/O is
   deliberately outside the transaction — never hold row locks (or a
   GiST index entry) while a remote API is slow.

Concurrency: the INSERT is the enforcement point. Two racing requests
for one slot both pass every pre-check; Postgres admits exactly one and
the loser gets IntegrityError('no_overlapping_active_bookings'), which
is translated to SlotTaken. Group sessions serialize on the session row
(FOR UPDATE) and on the conditional seat UPDATE; the loser gets
SessionFull. No application-level "check then act" exists anywhere.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import availability, money, payments
from .constraints import is_exclusion_violation
from .models import Booking, ClassSession, Payment
from .paystack import PaystackError


class BookingError(Exception):
    """Base class for booking failures safe to show a customer."""


class InvalidBooking(BookingError):
    """The requested booking breaks a business rule."""


class SlotTaken(BookingError):
    """Another active booking holds an overlapping range."""


class SessionFull(BookingError):
    """Not enough seats left in the class session."""


class PaymentInitiationFailed(BookingError):
    """Paystack could not start the deposit; the hold was released."""


@dataclass
class HoldResult:
    booking: Booking
    payment: Payment | None = None
    authorization_url: str | None = None


def create_hold(
    *,
    service,
    customer,
    start_at=None,
    resource_id=None,
    session_id=None,
    party_size=1,
    address="",
    notes="",
) -> HoldResult:
    """Create an unpaid hold and start the deposit payment.

    start_at is required for one-to-one services and must be aware;
    group services ignore it (the session carries the time).
    """
    if not service.is_active:
        raise InvalidBooking("This service is not taking bookings.")
    if party_size < 1:
        raise InvalidBooking("Party size must be at least 1.")
    if service.requires_address and not address.strip():
        raise InvalidBooking("This service needs an address.")

    if service.is_group:
        return _create_group_hold(
            service=service,
            customer=customer,
            session_id=session_id,
            party_size=party_size,
            address=address,
            notes=notes,
        )
    return _create_exclusive_hold(
        service=service,
        customer=customer,
        start_at=start_at,
        resource_id=resource_id,
        party_size=party_size,
        address=address,
        notes=notes,
    )


# --------------------------------------------------------------------------
# One-to-one resources
# --------------------------------------------------------------------------

def _expire_overdue_exclusive_holds(resource_id):
    """Free lapsed holds for this resource before inserting.

    Runs inside the caller's transaction on purpose: if the INSERT then
    loses the race, the sweep rolls back with it — a failed attempt must
    not mutate state. The sweep task is the slow global net; this is the
    fast, per-resource one that closes the window where a lapsed hold
    still blocks the exclusion index.
    """
    now = timezone.now()
    Booking.objects.filter(
        resource_id=resource_id,
        is_exclusive=True,
        status=Booking.Status.PENDING_PAYMENT,
        expires_at__lte=now,
    ).update(status=Booking.Status.EXPIRED, expires_at=None, updated_at=now)


def _create_exclusive_hold(*, service, customer, start_at, resource_id, party_size, address, notes):
    if start_at is None:
        raise InvalidBooking("start_at is required for one-to-one services.")
    if timezone.is_naive(start_at):
        raise InvalidBooking("start_at must be timezone-aware.")

    end_at = start_at + timedelta(minutes=service.duration_minutes)
    resource = _resolve_resource(service, start_at, end_at, resource_id, party_size)

    if party_size > resource.max_party_size:
        raise InvalidBooking("Party size exceeds what this resource can hold.")

    price_kobo = money.booking_total_kobo(service, party_size)
    deposit = money.deposit_kobo(service, party_size)

    try:
        with transaction.atomic():
            _expire_overdue_exclusive_holds(resource.pk)
            booking = Booking(
                business=service.business,
                resource=resource,
                service=service,
                customer=customer,
                start_at=start_at,
                end_at=end_at,
                party_size=party_size,
                address=address,
                notes=notes,
                status=Booking.Status.PENDING_PAYMENT,
                price_kobo=price_kobo,
                deposit_kobo=deposit,
            )
            if deposit == 0:
                # Nothing to collect: the hold is born confirmed.
                booking.status = Booking.Status.CONFIRMED
            booking.save()
            payment = payments.build_deposit_payment(booking) if deposit > 0 else None
    except IntegrityError as exc:
        if is_exclusion_violation(exc):
            raise SlotTaken("That slot was just taken. Pick another time.") from exc
        raise

    if payment is None:
        return HoldResult(booking=booking)
    return _request_deposit(booking, payment)


def _resolve_resource(service, start_at, end_at, resource_id, party_size):
    """Pick the resource to hold.

    Explicit id: must offer the service. Omitted id: first active resource
    that (a) has working hours covering the slot, (b) is clear of time
    off, and (c) is currently free — the "any barber" case. Both paths
    are advisory; the constraint still decides the race.
    """
    resources = service.resources.filter(is_active=True).order_by("id")

    if resource_id is not None:
        resource = resources.filter(pk=resource_id).first()
        if resource is None:
            raise InvalidBooking("That resource does not offer this service.")
        if not availability.slot_is_offered(service, start_at, resource.pk):
            raise InvalidBooking("That time is outside working hours.")
        return resource

    offered = []
    for resource in resources.filter(max_party_size__gte=party_size):
        if not availability.slot_is_offered(service, start_at, resource.pk):
            continue
        offered.append(resource)
        if availability.resource_is_free(service, resource, start_at, end_at):
            return resource

    # Distinguish "nobody works then" from "everyone is already booked":
    # the latter is a losing race, and create_hold's callers treat
    # SlotTaken (not InvalidBooking) as the retryable, customer-facing
    # outcome. The insert still decides the concurrent case.
    if offered:
        raise SlotTaken("That slot was just taken. Pick another time.")
    raise InvalidBooking("No resource is available at that time.")


# --------------------------------------------------------------------------
# Group sessions
# --------------------------------------------------------------------------

def _expire_overdue_group_holds(session):
    """Release seats held by lapsed unpaid holds on this session.

    Called after the session row is locked FOR UPDATE, so seat math is
    serialized against every other seat changer.
    """
    now = timezone.now()
    expired = Booking.objects.select_for_update().filter(
        session=session,
        status=Booking.Status.PENDING_PAYMENT,
        expires_at__lte=now,
    )
    for booking in expired:
        ClassSession.release_seats(session.pk, booking.party_size)
        booking.status = Booking.Status.EXPIRED
        booking.save()


def _create_group_hold(*, service, customer, session_id, party_size, address, notes):
    if session_id is None:
        raise InvalidBooking("Group services must be booked against a session.")

    now = timezone.now()
    price_kobo = money.booking_total_kobo(service, party_size)
    deposit = money.deposit_kobo(service, party_size)

    try:
        with transaction.atomic():
            try:
                session = ClassSession.objects.select_for_update().get(pk=session_id)
            except ClassSession.DoesNotExist:
                raise InvalidBooking("That session does not exist.") from None
            if session.service_id != service.pk:
                raise InvalidBooking("That session belongs to a different service.")
            if session.is_cancelled:
                raise InvalidBooking("That session was cancelled.")
            if session.start_at <= now:
                raise InvalidBooking("That session has already started.")
            if party_size > session.capacity:
                raise SessionFull("Not enough seats left in that session.")

            _expire_overdue_group_holds(session)
            if not ClassSession.reserve_seats(session.pk, party_size):
                raise SessionFull("Not enough seats left in that session.")

            booking = Booking(
                business=service.business,
                resource=session.resource,
                service=service,
                session=session,
                customer=customer,
                start_at=session.start_at,
                end_at=session.end_at,
                party_size=party_size,
                address=address,
                notes=notes,
                status=Booking.Status.PENDING_PAYMENT,
                price_kobo=price_kobo,
                deposit_kobo=deposit,
            )
            if deposit == 0:
                booking.status = Booking.Status.CONFIRMED
            booking.save()
            payment = payments.build_deposit_payment(booking) if deposit > 0 else None
    except IntegrityError as exc:
        if is_exclusion_violation(exc):
            # Group bookings are non-exclusive, so this only fires if a
            # blocker row was inserted concurrently — same customer-facing
            # outcome either way.
            raise SlotTaken("That session was just taken. Pick another.") from exc
        raise

    if payment is None:
        return HoldResult(booking=booking)
    return _request_deposit(booking, payment)


# --------------------------------------------------------------------------
# Deposit initiation (outside the hold transaction)
# --------------------------------------------------------------------------

def _request_deposit(booking, payment):
    try:
        data = payments.request_deposit(payment)
    except PaystackError as exc:
        _abandon(booking, payment)
        raise PaymentInitiationFailed("Could not start the deposit payment. Please try again.") from exc
    return HoldResult(booking=booking, payment=payment, authorization_url=data.get("authorization_url"))


def _abandon(booking, payment):
    """Provider refused the charge: release everything the hold took."""
    with transaction.atomic():
        payment.status = Payment.Status.FAILED
        payment.save(update_fields=["status"])
        booking.status = Booking.Status.EXPIRED
        booking.save()
        if booking.session_id:
            ClassSession.release_seats(booking.session_id, booking.party_size)



