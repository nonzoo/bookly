"""Session scheduling and blocker behavior.

The blocker is the whole point: a group class must reserve its resource
through the same exclusion constraint that protects one-to-one bookings.
These tests pin creation atomicity, the resource/time shape, and the two
directions of interaction (class blocks a one-to-one slot; an existing
booking blocks a class).
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from psycopg.types.range import TimestamptzRange

from bookings import availability, holds, sessions
from bookings.models import Booking, ClassSession, Resource, TimeOff
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


@pytest.fixture
def day():
    return future_weekday(2, TZ)


@pytest.fixture
def business():
    return make_business()


@pytest.fixture
def room(business):
    return make_resource(business, kind=Resource.Kind.ROOM, name="Studio", max_party_size=10)


@pytest.fixture
def group_service(business, room):
    return make_service(
        business,
        [room],
        capacity=6,
        duration_minutes=60,
        buffer_minutes=10,
        price_kobo=200_000,
    )


@pytest.fixture(autouse=True)
def fake_paystack(monkeypatch):
    def fake_initialize(*, email, amount_kobo, reference, metadata=None, timeout=10):
        return {"authorization_url": f"https://paystack.test/{reference}", "reference": reference}

    monkeypatch.setattr("bookings.paystack.initialize_transaction", fake_initialize)


def make_session(group_service, room, day, start_hour=17, end_hour=18, **overrides):
    defaults = {
        "service": group_service,
        "resource": room,
        "start_at": dt(day, start_hour),
        "end_at": dt(day, end_hour),
        "capacity": 6,
    }
    defaults.update(overrides)
    return sessions.create_session(**defaults)


def test_create_session_creates_one_confirmed_blocker(group_service, room, business, day):
    start, end = dt(day, 17), dt(day, 18)
    created = make_session(group_service, room, day, capacity=4)

    session = created.session
    blocker = created.blocker
    blocker.refresh_from_db()

    assert session.capacity == 4
    assert session.seats_taken == 0
    assert ClassSession.objects.count() == 1

    assert blocker.is_blocker is True
    assert blocker.is_exclusive is True          # derived in Booking.save()
    assert blocker.status == Booking.Status.CONFIRMED
    assert blocker.expires_at is None
    assert blocker.customer_id == business.owner_id
    assert blocker.session_id == session.pk
    assert blocker.resource_id == room.pk
    assert blocker.service_id == group_service.pk
    assert blocker.start_at == start
    assert blocker.end_at == end
    assert blocker.price_kobo == 0
    assert blocker.deposit_kobo == 0
    # Buffer is part of the reserved range, exactly like a customer job.
    assert blocker.time_range.lower == start
    assert blocker.time_range.upper == end + timedelta(minutes=group_service.buffer_minutes)


def test_class_blocker_removes_one_to_one_availability(group_service, room, business, day):
    make_working_hours(room, weekday=day.weekday(), start=time(9), end=time(20))
    one_to_one = make_service(business, [room], duration_minutes=30)

    starts = [slot["start_at"] for slot in availability.slots_for_day(one_to_one, day)]
    assert dt(day, 17) in starts

    make_session(group_service, room, day)  # 17:00-18:00

    starts = [slot["start_at"] for slot in availability.slots_for_day(one_to_one, day)]
    assert dt(day, 16, 30) in starts   # ends exactly when class starts
    assert dt(day, 17) not in starts
    assert dt(day, 17, 30) not in starts
    assert dt(day, 18, 15) in starts   # 10-minute buffer ends at 18:10


def test_one_to_one_hold_cannot_overlap_a_class_blocker(group_service, room, business, day):
    make_working_hours(room, weekday=day.weekday(), start=time(9), end=time(20))
    one_to_one = make_service(business, [room], duration_minutes=30)
    make_session(group_service, room, day)

    with pytest.raises(holds.SlotTaken):
        holds.create_hold(
            service=one_to_one,
            customer=make_user(),
            start_at=dt(day, 17),
            resource_id=room.pk,
        )

    # Outside the class, the same resource is bookable again.
    result = holds.create_hold(
        service=one_to_one,
        customer=make_user(),
        start_at=dt(day, 18, 15),
        resource_id=room.pk,
    )
    assert result.booking.status == Booking.Status.PENDING_PAYMENT


def test_existing_booking_blocks_session_creation(group_service, room, business, day):
    one_to_one = make_service(business, [room], duration_minutes=60)
    Booking.objects.create(
        business=business,
        resource=room,
        service=one_to_one,
        customer=make_user(),
        start_at=dt(day, 17),
        end_at=dt(day, 18),
        status=Booking.Status.CONFIRMED,
        price_kobo=0,
        deposit_kobo=0,
    )

    with pytest.raises(sessions.SessionSlotTaken):
        make_session(group_service, room, day)

    assert ClassSession.objects.count() == 0
    assert Booking.objects.filter(is_blocker=True).count() == 0


def test_session_creation_is_atomic_on_overlap(group_service, room, day):
    make_session(group_service, room, day)  # 17:00-18:00 (+10m buffer)

    with pytest.raises(sessions.SessionSlotTaken):
        make_session(group_service, room, day, start_hour=18, end_hour=19)

    # The loser left no orphan session or blocker behind.
    assert ClassSession.objects.count() == 1
    assert Booking.objects.filter(is_blocker=True).count() == 1


def test_back_to_back_sessions_are_allowed(group_service, room, day):
    # With no buffer, the first blocker is [17:00, 18:00) and the second
    # starts exactly at its upper bound: half-open ranges make that legal.
    group_service.buffer_minutes = 0
    group_service.save()

    first = make_session(group_service, room, day, start_hour=17, end_hour=18)
    second = make_session(group_service, room, day, start_hour=18, end_hour=19)

    assert first.session.pk != second.session.pk
    assert ClassSession.objects.count() == 2
    assert Booking.objects.filter(is_blocker=True).count() == 2


def test_session_times_must_be_aware_future_and_ordered(group_service, room, day):
    naive_start = datetime.combine(day, time(17))
    with pytest.raises(sessions.InvalidSession):
        make_session(group_service, room, day, start_at=naive_start)

    with pytest.raises(sessions.InvalidSession):
        make_session(group_service, room, day, start_at=timezone.now() - timedelta(hours=1))

    with pytest.raises(sessions.InvalidSession):
        make_session(group_service, room, day, start_at=dt(day, 18), end_at=dt(day, 17))


def test_one_to_one_service_cannot_have_sessions(business, room, day):
    service = make_service(business, [room], duration_minutes=30)
    with pytest.raises(sessions.InvalidSession):
        sessions.create_session(
            service=service,
            resource=room,
            start_at=dt(day, 17),
            end_at=dt(day, 18),
            capacity=5,
        )


def test_resource_must_offer_the_service_and_be_active(business, room, day):
    other_room = make_resource(business, kind=Resource.Kind.ROOM, name="Other", max_party_size=10)
    service = make_service(business, [room], capacity=6, duration_minutes=60)

    with pytest.raises(sessions.InvalidSession):
        sessions.create_session(
            service=service,
            resource=other_room,
            start_at=dt(day, 17),
            end_at=dt(day, 18),
            capacity=5,
        )

    room.is_active = False
    room.save()
    with pytest.raises(sessions.InvalidSession):
        sessions.create_session(
            service=service,
            resource=room,
            start_at=dt(day, 17),
            end_at=dt(day, 18),
            capacity=5,
        )


def test_session_cannot_be_scheduled_over_time_off(group_service, room, day):
    TimeOff.objects.create(
        resource=room,
        period=TimestamptzRange(dt(day, 18, 5), dt(day, 19)),
    )
    # The class itself ends at 18:00, but its 10-minute buffer touches
    # the time-off block; that still conflicts.
    with pytest.raises(sessions.InvalidSession):
        make_session(group_service, room, day)
