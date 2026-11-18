"""Class-session scheduling — the write side of group services.

A ClassSession alone is just a row in a table: nothing stops a one-to-one
booking from being made on the same resource at the same time. The
guarantee comes from a BLOCKER Booking row (`is_blocker=True`) created in
the same transaction, covering [start, end + service buffer). Because
blockers live in Booking, they are covered by the very same exclusion
constraint as customer bookings — no second overlap mechanism.

Creation flow:
1. Validate the shape (group service, active resource that offers it,
   future aware times, sensible capacity).
2. One transaction: insert ClassSession, then its blocker.
3. If the blocker loses the overlap race, the whole transaction rolls
   back and SessionSlotTaken is raised — there is never a session without
   its blocker.

Seat state is intentionally untouched here: seats_taken starts at 0 and
only moves through ClassSession.reserve_seats()/release_seats().
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from psycopg.types.range import TimestamptzRange

from .constraints import is_exclusion_violation
from .models import Booking, ClassSession, TimeOff


class SessionError(Exception):
    """Base class for session-scheduling failures."""


class InvalidSession(SessionError):
    """The requested session breaks a business rule."""


class SessionSlotTaken(SessionError):
    """The resource already hosts an overlapping booking or session."""


@dataclass
class CreatedSession:
    session: ClassSession
    blocker: Booking


def create_session(*, service, resource, start_at, end_at, capacity) -> CreatedSession:
    """Schedule one class and reserve its resource with a blocker booking."""
    if not service.is_group:
        raise InvalidSession("Only group services have class sessions.")
    if not service.is_active:
        raise InvalidSession("This service is not active.")
    if capacity < 1:
        raise InvalidSession("Capacity must be at least 1.")
    if resource.business_id != service.business_id:
        raise InvalidSession("That resource belongs to a different business.")
    if not resource.is_active:
        raise InvalidSession("That resource is not active.")
    if not service.resources.filter(pk=resource.pk).exists():
        raise InvalidSession("That resource does not offer this service.")
    if timezone.is_naive(start_at) or timezone.is_naive(end_at):
        raise InvalidSession("Session times must be timezone-aware.")
    if end_at <= start_at:
        raise InvalidSession("A session must end after it starts.")
    if start_at <= timezone.now():
        raise InvalidSession("A session must start in the future.")

    _reject_time_off(service, resource, start_at, end_at)

    try:
        with transaction.atomic():
            session = ClassSession.objects.create(
                service=service,
                resource=resource,
                start_at=start_at,
                end_at=end_at,
                capacity=capacity,
            )
            blocker = Booking(
                business=service.business,
                resource=resource,
                service=service,
                session=session,
                # The owner is the stand-in customer for an internal row.
                # Blocker rows are never charged and never appear in a
                # customer's booking list (callers filter is_blocker).
                customer=service.business.owner,
                start_at=start_at,
                end_at=end_at,
                party_size=1,
                status=Booking.Status.CONFIRMED,
                price_kobo=0,
                deposit_kobo=0,
                is_blocker=True,
                notes="Blocker for class session",
            )
            blocker.save()
    except IntegrityError as exc:
        if is_exclusion_violation(exc):
            raise SessionSlotTaken(
                "That resource is already booked for part of that time."
            ) from exc
        raise

    return CreatedSession(session=session, blocker=blocker)


def _reject_time_off(service, resource, start_at, end_at):
    """Refuse to schedule a class over the resource's time off.

    The buffer is part of the blocker's occupied range, so a time-off
    block that only touches the cleanup window still conflicts. This is a
    scheduling-time courtesy check; the exclusion constraint handles
    bookings, not TimeOff rows.
    """
    buffer = timedelta(minutes=service.buffer_minutes)
    probe = TimestamptzRange(start_at, end_at + buffer)
    if TimeOff.objects.filter(resource=resource, period__overlap=probe).exists():
        raise InvalidSession("That resource is on time off then.")
