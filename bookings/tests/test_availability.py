"""Availability behavior: grid math, buffers, time off, split shifts,
and the group-session listing shape."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from psycopg.types.range import TimestamptzRange

from bookings import availability
from bookings.models import Booking, ClassSession, TimeOff
from bookings.tests.factories import (
    future_weekday,
    make_business,
    make_resource,
    make_service,
    make_user,
    make_working_hours,
)

pytestmark = pytest.mark.django_db

TZ = ZoneInfo("Africa/Lagos")


def dt(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


def make_booking(business, resource, service, start, end):
    return Booking.objects.create(
        business=business,
        resource=resource,
        service=service,
        customer=make_user(),
        start_at=start,
        end_at=end,
        status=Booking.Status.CONFIRMED,
        price_kobo=0,
        deposit_kobo=0,
    )


@pytest.fixture
def day():
    # Wednesday in the business timezone, strictly in the future.
    return future_weekday(2, TZ)


@pytest.fixture
def business():
    return make_business()


@pytest.fixture
def resource(business):
    return make_resource(business, name="Ada")


@pytest.fixture
def service(business, resource, day):
    service = make_service(business, [resource], duration_minutes=30)
    make_working_hours(resource, weekday=day.weekday(), start=time(9), end=time(12))
    return service


def starts_of(service, day, **kwargs):
    return [slot["start_at"] for slot in availability.slots_for_day(service, day, **kwargs)]


def test_offers_15_minute_grid_inside_working_hours(service, day):
    starts = starts_of(service, day)
    assert len(starts) == 11  # 09:00 .. 11:30 on a 15-min grid
    assert starts[0] == dt(day, 9)
    assert starts[-1] == dt(day, 11, 30)


def test_confirmed_booking_removes_overlapping_slots(business, resource, service, day):
    make_booking(business, resource, service, dt(day, 10), dt(day, 10, 30))
    starts = starts_of(service, day)

    assert dt(day, 10) not in starts
    assert dt(day, 10, 15) not in starts  # would overlap 10:00-10:30
    assert dt(day, 9, 30) in starts       # ends exactly when the booking starts
    assert dt(day, 10, 30) in starts      # starts exactly when the booking ends


def test_buffer_blocks_the_slot_after_the_job(business, resource, day):
    # This test builds its own service, so it also owns the working hours
    # (the `service` fixture is deliberately not requested).
    make_working_hours(resource, weekday=day.weekday(), start=time(9), end=time(12))
    service = make_service(business, [resource], duration_minutes=30, buffer_minutes=15)
    make_booking(business, resource, service, dt(day, 9), dt(day, 9, 30))
    # Blocked range is [09:00, 09:45): the 09:30 slot must be gone, 09:45 free.
    starts = starts_of(service, day)
    assert dt(day, 9, 30) not in starts
    assert dt(day, 9, 45) in starts


def test_time_off_removes_slots(resource, service, day):
    TimeOff.objects.create(resource=resource, period=TimestamptzRange(dt(day, 9), dt(day, 10)))
    starts = starts_of(service, day)
    assert dt(day, 9) not in starts
    assert dt(day, 9, 30) not in starts
    assert dt(day, 10) in starts


def test_split_shifts_merge_so_slots_can_straddle_boundaries(business, resource, day):
    make_working_hours(resource, weekday=day.weekday(), start=time(9), end=time(10))
    make_working_hours(resource, weekday=day.weekday(), start=time(10), end=time(11))
    service = make_service(business, [resource], duration_minutes=30)

    starts = starts_of(service, day)
    assert dt(day, 9, 30) in starts  # only possible when 9-10 and 10-11 are merged
    assert starts[-1] == dt(day, 10, 30)


def test_availability_supports_many_resources_and_filters(business, day):
    ada = make_resource(business, name="Ada")
    ben = make_resource(business, name="Ben")
    service = make_service(business, [ada, ben], duration_minutes=30)
    for resource in (ada, ben):
        make_working_hours(resource, weekday=day.weekday(), start=time(9), end=time(12))

    assert len(starts_of(service, day)) == 22  # both resources
    assert len(starts_of(service, day, resource_id=ben.pk)) == 11


def test_slot_is_offered_is_advisory_and_bounds_aware(resource, service, day):
    assert availability.slot_is_offered(service, dt(day, 10), resource.pk) is True
    assert availability.slot_is_offered(service, dt(day, 8), resource.pk) is False   # before opening
    assert availability.slot_is_offered(service, dt(day, 11, 45), resource.pk) is False  # wouldn't fit

    TimeOff.objects.create(resource=resource, period=TimestamptzRange(dt(day, 10), dt(day, 10, 30)))
    assert availability.slot_is_offered(service, dt(day, 10), resource.pk) is False


def test_group_availability_lists_sessions_with_seats_left(business, resource, day):
    group_service = make_service(business, [resource], capacity=6, duration_minutes=60)
    open_session = ClassSession.objects.create(
        service=group_service,
        resource=resource,
        start_at=dt(day, 17),
        end_at=dt(day, 18),
        capacity=5,
        seats_taken=2,
    )
    ClassSession.objects.create(
        service=group_service,
        resource=resource,
        start_at=dt(day, 18),
        end_at=dt(day, 19),
        capacity=5,
        is_cancelled=True,
    )

    sessions = availability.sessions_for_day(group_service, day)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == open_session.pk
    assert sessions[0]["seats_left"] == 3

    # The two shapes never mix.
    assert availability.slots_for_day(group_service, day) == []
    assert availability.sessions_for_day(make_service(business, [resource]), day) == []
