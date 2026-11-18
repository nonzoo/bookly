"""Lifecycle policy tests: who gets the deposit back, and when.

The matrix:
- customer, enough notice        -> refund
- customer, late                 -> forfeit marker (0 kobo)
- business                       -> refund always
- no-show                        -> forfeit marker + seat release
- completed                      -> no money moves
Plus the class-wide cancellation path, which must release the blocker,
every seat and every paid customer's deposit exactly once.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from bookings import holds, ledger, lifecycle, sessions
from bookings.models import Booking, ClassSession, LedgerEntry, Payment
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


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    def fake_initialize(*, email, amount_kobo, reference, metadata=None, timeout=10):
        return {"authorization_url": f"https://paystack.test/{reference}", "reference": reference}

    def fake_refund(*, transaction_reference, amount_kobo, reference, timeout=10):
        return {"status": "pending", "reference": reference, "amount": str(amount_kobo)}

    monkeypatch.setattr("bookings.paystack.initialize_transaction", fake_initialize)
    monkeypatch.setattr("bookings.paystack.create_refund", fake_refund)


@pytest.fixture
def day():
    return future_weekday(2, TZ)


@pytest.fixture
def business():
    return make_business()


@pytest.fixture
def resource(business, day):
    resource = make_resource(business, name="Ada")
    make_working_hours(resource, weekday=day.weekday(), start=time(9), end=time(20))
    return resource


@pytest.fixture
def service(business, resource):
    return make_service(business, [resource], duration_minutes=30)


@pytest.fixture
def customer():
    return make_user()


@pytest.fixture
def held(service, customer, day, business, request):
    # The window is snapshotted onto the booking when it is created, so a
    # test that needs a different policy must set it BEFORE the hold
    # exists. @pytest.mark.parametrize("held", [N], indirect=True) does.
    if hasattr(request, "param"):
        business.free_cancellation_hours = request.param
        business.save(update_fields=["free_cancellation_hours"])
    start_at = datetime.combine(day, time(10), tzinfo=TZ)
    return holds.create_hold(service=service, customer=customer, start_at=start_at)


def mark_paid(held):
    Payment.objects.filter(pk=held.payment.pk).update(status=Payment.Status.SUCCESS)
    held.payment.refresh_from_db()
    held.booking.status = Booking.Status.CONFIRMED
    held.booking.save()
    ledger.credit_deposit(held.payment, held.booking)
    held.booking.refresh_from_db()
    return held


@pytest.mark.parametrize("held", [0], indirect=True)
def test_free_cancellation_refunds_the_deposit(held):
    mark_paid(held)

    result = lifecycle.cancel_by_customer(held.booking)

    held.booking.refresh_from_db()
    assert held.booking.status == Booking.Status.CANCELLED_BY_CUSTOMER
    assert held.booking.cancelled_at is not None
    assert result.forfeited is False
    assert result.refund is not None
    assert result.refund.kind == Payment.Kind.REFUND
    assert result.refund.status == Payment.Status.INITIATED
    assert result.refund.amount_kobo == held.payment.amount_kobo

    assert LedgerEntry.objects.filter(
        payment=held.payment, entry_type=LedgerEntry.Type.DEPOSIT_RECEIVED
    ).exists()
    assert not LedgerEntry.objects.filter(entry_type=LedgerEntry.Type.DEPOSIT_FORFEITED).exists()


@pytest.mark.parametrize("held", [216], indirect=True)  # 9 days: every fixture slot is "late"
def test_late_cancellation_forfeits_the_deposit(held):
    mark_paid(held)

    result = lifecycle.cancel_by_customer(held.booking)

    assert result.refund is None
    assert result.forfeited is True
    marker = LedgerEntry.objects.get(
        payment=held.payment, entry_type=LedgerEntry.Type.DEPOSIT_FORFEITED
    )
    assert marker.amount_kobo == 0
    assert not Payment.objects.filter(kind=Payment.Kind.REFUND).exists()

    # Idempotent: a second call does not create a second marker.
    again = lifecycle.cancel_by_customer(held.booking)
    assert again.forfeited is True
    assert LedgerEntry.objects.filter(entry_type=LedgerEntry.Type.DEPOSIT_FORFEITED).count() == 1


def test_policy_edits_do_not_rewrite_existing_bookings(business, held):
    """The customer keeps the window that was in force when they booked."""
    business.free_cancellation_hours = 216  # 9 days: would forfeit if read live
    business.save(update_fields=["free_cancellation_hours"])
    mark_paid(held)

    result = lifecycle.cancel_by_customer(held.booking)

    assert result.refund is not None
    assert result.forfeited is False
    assert held.booking.free_cancellation_hours == 24


def test_cancelling_an_unpaid_hold_frees_the_slot(held, service, customer):
    result = lifecycle.cancel_by_customer(held.booking)

    assert result.refund is None
    assert result.forfeited is False
    held.booking.refresh_from_db()
    assert held.booking.status == Booking.Status.CANCELLED_BY_CUSTOMER

    # The slot is immediately rebookable.
    replacement = holds.create_hold(
        service=service,
        customer=customer,
        start_at=held.booking.start_at,
        resource_id=held.booking.resource_id,
    )
    assert replacement.booking.status == Booking.Status.PENDING_PAYMENT


@pytest.mark.parametrize("held", [216], indirect=True)
def test_business_cancellation_refunds_even_late(held):
    mark_paid(held)

    result = lifecycle.cancel_by_business(held.booking)

    held.booking.refresh_from_db()
    assert held.booking.status == Booking.Status.CANCELLED_BY_BUSINESS
    assert result.refund is not None
    assert result.refund.amount_kobo == held.payment.amount_kobo


def test_zero_deposit_cancellation_moves_no_money(business, service, customer, day):
    business.deposit_percent = 0
    business.save()
    start_at = datetime.combine(day, time(10), tzinfo=TZ)
    held = holds.create_hold(service=service, customer=customer, start_at=start_at)
    assert held.booking.status == Booking.Status.CONFIRMED
    assert held.payment is None

    result = lifecycle.cancel_by_customer(held.booking)

    assert result.refund is None
    assert result.forfeited is False
    assert LedgerEntry.objects.count() == 0
    assert Payment.objects.count() == 0


def test_no_show_forfeits_and_releases_group_seats(business, resource, day):
    group_service = make_service(business, [resource], capacity=6, duration_minutes=60, price_kobo=200_000)
    created = sessions.create_session(
        service=group_service,
        resource=resource,
        start_at=datetime.combine(day, time(17), tzinfo=TZ),
        end_at=datetime.combine(day, time(18), tzinfo=TZ),
        capacity=6,
    )
    held = holds.create_hold(
        service=group_service, customer=make_user(), session_id=created.session.pk, party_size=2
    )
    mark_paid(held)

    # Move the session into the past; the customer never showed.
    past_start = timezone.now() - timedelta(hours=2)
    ClassSession.objects.filter(pk=created.session.pk).update(
        start_at=past_start, end_at=past_start + timedelta(hours=1)
    )
    Booking.objects.filter(pk=held.booking.pk).update(
        start_at=past_start, end_at=past_start + timedelta(hours=1)
    )
    created.session.refresh_from_db()

    result = lifecycle.mark_no_show(held.booking)

    created.session.refresh_from_db()
    assert result.status == Booking.Status.NO_SHOW
    assert created.session.seats_taken == 0
    assert LedgerEntry.objects.filter(
        payment=held.payment, entry_type=LedgerEntry.Type.DEPOSIT_FORFEITED
    ).exists()
    assert not Payment.objects.filter(kind=Payment.Kind.REFUND).exists()


def test_completion_requires_the_end_time_and_moves_no_money(business, resource, service, customer):
    now = timezone.now()
    booking = Booking.objects.create(
        business=business,
        resource=resource,
        service=service,
        customer=customer,
        start_at=now - timedelta(hours=1),
        end_at=now - timedelta(minutes=1),
        status=Booking.Status.CONFIRMED,
        price_kobo=0,
        deposit_kobo=0,
    )

    completed = lifecycle.mark_completed(booking)

    assert completed.status == Booking.Status.COMPLETED
    assert LedgerEntry.objects.count() == 0


def test_cannot_complete_before_the_end(business, resource, service, customer):
    now = timezone.now()
    booking = Booking.objects.create(
        business=business,
        resource=resource,
        service=service,
        customer=customer,
        start_at=now + timedelta(minutes=10),
        end_at=now + timedelta(minutes=40),
        status=Booking.Status.CONFIRMED,
        price_kobo=0,
        deposit_kobo=0,
    )

    with pytest.raises(lifecycle.InvalidTransition):
        lifecycle.mark_completed(booking)


def test_cancel_session_releases_blocker_seats_and_refunds_everyone(business, resource, day):
    group_service = make_service(business, [resource], capacity=6, duration_minutes=60, price_kobo=200_000)
    created = sessions.create_session(
        service=group_service,
        resource=resource,
        start_at=datetime.combine(day, time(17), tzinfo=TZ),
        end_at=datetime.combine(day, time(18), tzinfo=TZ),
        capacity=6,
    )

    paid_one = holds.create_hold(
        service=group_service, customer=make_user(), session_id=created.session.pk, party_size=1
    )
    paid_two = holds.create_hold(
        service=group_service, customer=make_user(), session_id=created.session.pk, party_size=2
    )
    unpaid = holds.create_hold(
        service=group_service, customer=make_user(), session_id=created.session.pk, party_size=1
    )
    mark_paid(paid_one)
    mark_paid(paid_two)

    created.session.refresh_from_db()
    assert created.session.seats_taken == 4

    results = lifecycle.cancel_session(created.session)

    created.session.refresh_from_db()
    created.blocker.refresh_from_db()
    for held in (paid_one, paid_two, unpaid):
        held.booking.refresh_from_db()

    assert created.session.is_cancelled is True
    assert created.blocker.status == Booking.Status.CANCELLED_BY_BUSINESS
    assert {result.booking.pk for result in results} == {
        paid_one.booking.pk, paid_two.booking.pk, unpaid.booking.pk
    }
    assert all(
        held.booking.status == Booking.Status.CANCELLED_BY_BUSINESS
        for held in (paid_one, paid_two, unpaid)
    )
    assert created.session.seats_taken == 0
    assert Payment.objects.filter(kind=Payment.Kind.REFUND).count() == 2
    assert not Payment.objects.filter(
        kind=Payment.Kind.REFUND, booking=unpaid.booking
    ).exists()

    # Idempotent: a second cancellation does nothing.
    assert lifecycle.cancel_session(created.session) == []


def test_expired_booking_cannot_be_cancelled(held):
    Booking.objects.filter(pk=held.booking.pk).update(
        status=Booking.Status.EXPIRED, expires_at=None
    )
    held.booking.refresh_from_db()

    with pytest.raises(lifecycle.InvalidTransition):
        lifecycle.cancel_by_customer(held.booking)


def test_blocker_rows_cannot_be_cancelled_directly(business, resource, day):
    group_service = make_service(business, [resource], capacity=6, duration_minutes=60)
    created = sessions.create_session(
        service=group_service,
        resource=resource,
        start_at=datetime.combine(day, time(17), tzinfo=TZ),
        end_at=datetime.combine(day, time(18), tzinfo=TZ),
        capacity=6,
    )

    with pytest.raises(lifecycle.InvalidTransition):
        lifecycle.cancel_by_customer(created.blocker)
