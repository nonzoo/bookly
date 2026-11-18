"""Availability computation — read-side only, no writes, no locks.

Two shapes, matching the two booking modes:

- one-to-one services (capacity == 1)  -> grid slots per resource
- group services   (capacity > 1)      -> scheduled sessions with seats left

Design rule: availability is ADVISORY. It mirrors the same rules the
database enforces (working hours, time off, active bookings including
their buffers) so the API offers sensible slots, but it can never prevent
a concurrent double-book. The exclusion constraint in create_hold() is
the only enforcement point. Keeping that boundary crisp is why this
module performs no locking and no writes.

Slot math:
- Day boundaries are computed in the BUSINESS timezone; everything is
  stored/compared as aware datetimes (UTC under the hood).
- Slots start on a 15-minute grid anchored at the window start.
- A candidate slot must fit the service duration inside the working
  window, and its would-be time_range [start, end + buffer) must clear
  every busy interval (active exclusive bookings' ranges and time off).
  Testing the buffer tail here means "availability says free" implies
  "the insert won't be blocked by a buffer overlap" — one rule, not two.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone
from psycopg.types.range import TimestamptzRange

from .models import Booking, ClassSession, TimeOff, WorkingHours

SLOT_STEP_MINUTES = 15


def _business_tz(service):
    return ZoneInfo(service.business.timezone)


def _local_day_bounds(service, day: date):
    tz = _business_tz(service)
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _merged_windows(resource, day: date, tz):
    """Working-hours windows for that weekday, split shifts merged.

    Two adjacent rows (9-12 and 12-17) behave as one window, so a slot
    can straddle the boundary like a human would expect.
    """
    rows = WorkingHours.objects.filter(resource=resource, weekday=day.weekday()).order_by("start_time")
    windows = []
    for row in rows:
        start = datetime.combine(day, row.start_time, tzinfo=tz)
        end = datetime.combine(day, row.end_time, tzinfo=tz)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows


def _busy_intervals(resource, range_start, range_end):
    """Live exclusive bookings + time off overlapping the given window.

    A pending_payment row whose expires_at has passed is NOT busy: its
    hold is over even if the sweep task has not run yet. create_hold's
    in-transaction sweep expires it just before the INSERT, so treating
    it as free here is what lets a customer rebook a lapsed slot. The
    exclusion constraint still owns the final verdict.
    """
    probe = TimestamptzRange(range_start, range_end)
    now = timezone.now()
    booking_ranges = (
        Booking.objects.filter(
            resource=resource,
            is_exclusive=True,
            status__in=Booking.ACTIVE_STATUSES,
            time_range__overlap=probe,
        )
        .filter(
            ~Q(status=Booking.Status.PENDING_PAYMENT)
            | Q(expires_at__isnull=True)
            | Q(expires_at__gt=now)
        )
        .values_list("time_range", flat=True)
    )
    time_off_ranges = TimeOff.objects.filter(resource=resource, period__overlap=probe).values_list(
        "period", flat=True
    )
    return list(booking_ranges) + list(time_off_ranges)


def _overlaps(start, end, intervals):
    for interval in intervals:
        if interval.lower is None or interval.upper is None:
            continue
        # Stored ranges use [) bounds; strict comparisons match that.
        if start < interval.upper and interval.lower < end:
            return True
    return False


def slots_for_day(service, day: date, resource_id=None):
    """Grid slots for a one-to-one service on a business-local day.

    Returns dicts: {"resource_id", "start_at", "end_at"} with aware
    datetimes, sorted by start then resource.
    """
    if service.is_group:
        return []  # group availability is sessions_for_day()

    tz = _business_tz(service)
    day_start, day_end = _local_day_bounds(service, day)
    duration = timedelta(minutes=service.duration_minutes)
    buffer = timedelta(minutes=service.buffer_minutes)
    now = timezone.now()

    resources = service.resources.filter(is_active=True)
    if resource_id is not None:
        resources = resources.filter(pk=resource_id)

    slots = []
    for resource in resources.order_by("id"):
        windows = _merged_windows(resource, day, tz)
        if not windows:
            continue
        busy = _busy_intervals(resource, day_start, day_end)
        for window_start, window_end in windows:
            cursor = window_start
            while cursor + duration <= window_end:
                start = cursor
                end = start + duration
                if not _overlaps(start, end + buffer, busy) and start >= now:
                    slots.append({"resource_id": resource.pk, "start_at": start, "end_at": end})
                cursor += timedelta(minutes=SLOT_STEP_MINUTES)

    slots.sort(key=lambda slot: (slot["start_at"], slot["resource_id"]))
    return slots


def sessions_for_day(service, day: date):
    """Bookable sessions for a group service on a business-local day.

    Returns dicts with seats_left, excluding cancelled/past/full ones.
    """
    if not service.is_group:
        return []

    day_start, day_end = _local_day_bounds(service, day)
    now = timezone.now()
    sessions = (
        ClassSession.objects.filter(service=service, is_cancelled=False, start_at__gte=day_start, start_at__lt=day_end)
        .select_related("resource")
        .order_by("start_at")
    )
    result = []
    for session in sessions:
        seats_left = session.capacity - session.seats_taken
        if seats_left <= 0 or session.start_at < now:
            continue
        result.append(
            {
                "session_id": session.pk,
                "resource_id": session.resource_id,
                "start_at": session.start_at,
                "end_at": session.end_at,
                "capacity": session.capacity,
                "seats_left": seats_left,
            }
        )
    return result


def slot_is_offered(service, start_at, resource_id):
    """Advisory pre-check used by create_hold.

    True when the start is in the future, inside that resource's working
    hours for its business-local weekday, and clear of time off. Bookings
    are NOT checked here — create_hold's insert is the authority, and
    re-checking them would just duplicate the constraint's job.
    """
    if timezone.is_naive(start_at) or start_at <= timezone.now():
        return False

    resource = service.resources.filter(pk=resource_id, is_active=True).first()
    if resource is None:
        return False

    tz = _business_tz(service)
    local_day = start_at.astimezone(tz).date()
    duration = timedelta(minutes=service.duration_minutes)
    end_at = start_at + duration

    windows = _merged_windows(resource, local_day, tz)
    if not any(window_start <= start_at and end_at <= window_end for window_start, window_end in windows):
        return False

    # Time off only: bookings are deliberately not checked here (see
    # docstring) — the exclusion constraint owns that verdict.
    time_off = TimeOff.objects.filter(
        resource=resource, period__overlap=TimestamptzRange(start_at, end_at)
    ).values_list("period", flat=True)
    return not _overlaps(start_at, end_at, list(time_off))


def resource_is_free(service, resource, start_at, end_at):
    """Advisory: would the booking's full blocked range (incl. buffer)
    clear every busy interval right now? Used to pick a resource when the
    caller doesn't specify one. Not an enforcement point."""
    buffer = timedelta(minutes=service.buffer_minutes)
    busy = _busy_intervals(resource, start_at, end_at + buffer)
    return not _overlaps(start_at, end_at + buffer, busy)
