"""Behavioral tests for the hold service.

These are sequential (the parallel races have their own suites): each
test pins one rule — snapshotting, zero-deposit confirmation, the expiry
sweeps, seat release, and the provider-failure unwind.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from bookings import holds
from bookings.models import Booking, ClassSession, Payment, Resource
from bookings.paystack import PaystackError
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


@pytest.fixture
def day():
    return future_weekday(2, TZ)


def dt(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


@pytest.fixture(autouse=True)
def fake_paystack(monkeypatch):
    calls = []

    def fake_initialize(*, email, amount_kobo, reference, metadata=None, timeout=10):
        calls.append({"email": email, "amount_kobo": amount_kobo, "reference": reference})
        return {"authorization_url": f"https://paystack.test/{reference}", "reference": reference}

    monkeypatch.setattr("bookings.paystack.initialize_transaction", fake_initialize)
    return calls


@pytest.fixture
def business():
    return make_business()


@pytest.fixture
def resource(business, day):
    resource = make_resource(business, name="Ada")
    make_working_hours(resource, weekday=day.weekday(), start=time(9), end=time(17))
    return resource


@pytest.fixture
def service(business, resource):
    return make_service(business, [resource], duration_minutes=30)


@pytest.fixture
def customer():
    return make_user()


@pytest.fixture
def start(day):
    return dt(day, 10)


def test_exclusive_hold_snapshots_price_and_starts_payment(service, customer, start, fake_paystack):
    result = holds.create_hold(service=service, customer=customer, start_at=start)

    booking = result.booking
    assert booking.status == Booking.Status.PENDING_PAYMENT
    assert booking.expires_at is not None
    assert booking.price_kobo == 500_000
    assert booking.deposit_kobo == 100_000
    assert booking.time_range.lower == start
    assert booking.is_exclusive is True

    assert result.payment.kind == Payment.Kind.DEPOSIT
    assert result.payment.amount_kobo == 100_000
    assert result.authorization_url.startswith("https://paystack.test/")
    assert fake_paystack[0]["amount_kobo"] == 100_000


def test_zero_deposit_confirms_without_payment(business, service, customer, start):
    business.deposit_percent = 0
    business.save()

    result = holds.create_hold(service=service, customer=customer, start_at=start)

    assert result.booking.status == Booking.Status.CONFIRMED
    assert result.booking.expires_at is None
    assert result.payment is None
    assert Payment.objects.count() == 0


def test_second_hold_on_same_slot_is_refused(service, customer, start):
    holds.create_hold(service=service, customer=customer, start_at=start)
    with pytest.raises(holds.SlotTaken):
        holds.create_hold(service=service, customer=customer, start_at=start)


def test_expired_hold_is_swept_and_slot_reopens(service, customer, start):
    first = holds.create_hold(service=service, customer=customer, start_at=start)
    # Force the hold past its deadline without waiting for the sweep task.
    Booking.objects.filter(pk=first.booking.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    second = holds.create_hold(service=service, customer=customer, start_at=start)

    first.booking.refresh_from_db()
    assert first.booking.status == Booking.Status.EXPIRED
    assert second.booking.status == Booking.Status.PENDING_PAYMENT


def test_address_is_required_only_for_home_services(service, customer, start):
    service.requires_address = True
    service.save()

    with pytest.raises(holds.InvalidBooking):
        holds.create_hold(service=service, customer=customer, start_at=start)

    result = holds.create_hold(
        service=service, customer=customer, start_at=start, address="12 Marina, Lagos"
    )
    assert result.booking.address == "12 Marina, Lagos"


def test_party_size_cannot_exceed_resource_capacity(business, day, customer):
    room = make_resource(business, kind=Resource.Kind.ROOM, max_party_size=4)
    make_working_hours(room, weekday=day.weekday(), start=time(9), end=time(17))
    service = make_service(business, [room], duration_minutes=30)

    with pytest.raises(holds.InvalidBooking):
        holds.create_hold(
            service=service, customer=customer, start_at=dt(day, 10), resource_id=room.pk, party_size=5
        )


def test_slot_outside_working_hours_is_rejected(service, customer, day):
    with pytest.raises(holds.InvalidBooking):
        holds.create_hold(service=service, customer=customer, start_at=dt(day, 8))


@pytest.fixture
def group_service(business, resource):
    return make_service(business, [resource], capacity=4, duration_minutes=60, price_kobo=200_000)


@pytest.fixture
def session(group_service, resource, day):
    return ClassSession.objects.create(
        service=group_service,
        resource=resource,
        start_at=dt(day, 17),
        end_at=dt(day, 18),
        capacity=3,
    )


def test_group_hold_reserves_seats_and_prices_per_head(group_service, session, customer):
    result = holds.create_hold(service=group_service, customer=customer, session_id=session.pk, party_size=2)

    session.refresh_from_db()
    assert session.seats_taken == 2
    assert result.booking.session_id == session.pk
    assert result.booking.price_kobo == 400_000  # ₦2,000 x 2
    assert result.booking.deposit_kobo == 80_000  # 20% of total


def test_group_hold_is_refused_when_full(group_service, session, customer):
    holds.create_hold(service=group_service, customer=customer, session_id=session.pk, party_size=3)
    with pytest.raises(holds.SessionFull):
        holds.create_hold(service=group_service, customer=customer, session_id=session.pk, party_size=1)


def test_expired_group_hold_releases_seats(group_service, session, customer):
    first = holds.create_hold(service=group_service, customer=customer, session_id=session.pk, party_size=3)
    Booking.objects.filter(pk=first.booking.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    holds.create_hold(service=group_service, customer=customer, session_id=session.pk, party_size=3)

    session.refresh_from_db()
    first.booking.refresh_from_db()
    assert first.booking.status == Booking.Status.EXPIRED
    assert session.seats_taken == 3  # old seats released, new hold took them


def test_group_session_linkage_is_validated(business, resource, customer, day):
    group_service = make_service(business, [resource], capacity=4, duration_minutes=60, price_kobo=200_000)
    other_service = make_service(business, [resource], capacity=4, duration_minutes=60, price_kobo=100_000)
    session = ClassSession.objects.create(
        service=group_service, resource=resource, start_at=dt(day, 17), end_at=dt(day, 18), capacity=3
    )

    with pytest.raises(holds.InvalidBooking):
        holds.create_hold(service=other_service, customer=customer, session_id=session.pk)
    with pytest.raises(holds.InvalidBooking):
        holds.create_hold(service=group_service, customer=customer)  # group without a session


def test_zero_deposit_group_hold_keeps_seats_and_confirms(business, group_service, session, customer):
    business.deposit_percent = 0
    business.save()

    result = holds.create_hold(service=group_service, customer=customer, session_id=session.pk, party_size=2)

    session.refresh_from_db()
    assert result.booking.status == Booking.Status.CONFIRMED
    assert result.payment is None
    assert session.seats_taken == 2


def test_provider_failure_releases_an_exclusive_slot(service, customer, start, monkeypatch):
    def boom(**kwargs):
        raise PaystackError("provider down")

    monkeypatch.setattr("bookings.paystack.initialize_transaction", boom)
    with pytest.raises(holds.PaymentInitiationFailed):
        holds.create_hold(service=service, customer=customer, start_at=start)

    booking = Booking.objects.get()
    assert booking.status == Booking.Status.EXPIRED
    assert Payment.objects.get().status == Payment.Status.FAILED

    # The slot is free again: a retry (provider back up) succeeds.
    def recovered(*, email, amount_kobo, reference, metadata=None, timeout=10):
        return {"authorization_url": "https://paystack.test/retry", "reference": reference}

    monkeypatch.setattr("bookings.paystack.initialize_transaction", recovered)
    retry = holds.create_hold(service=service, customer=customer, start_at=start)
    assert retry.booking.status == Booking.Status.PENDING_PAYMENT


def test_provider_failure_releases_group_seats(group_service, session, customer, monkeypatch):
    def boom(**kwargs):
        raise PaystackError("provider down")

    monkeypatch.setattr("bookings.paystack.initialize_transaction", boom)
    with pytest.raises(holds.PaymentInitiationFailed):
        holds.create_hold(service=group_service, customer=customer, session_id=session.pk, party_size=2)

    session.refresh_from_db()
    assert session.seats_taken == 0
    assert Booking.objects.get().status == Booking.Status.EXPIRED
