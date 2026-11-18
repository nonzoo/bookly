"""Expiry sweep tests.

The sweep has three jobs, and each is pinned here:
1. expire a lapsed unpaid hold so its slot can be rebooked;
2. release the group seats such a hold was occupying;
3. leave everything else (paid, future, confirmed, blockers) alone.

Two invariants get special attention because they are the kind of bug that
only shows up in production:
- idempotency: running the sweep twice must not release seats twice;
- non-blocking: a booking locked by another transaction (a webhook, say)
  is skipped, never waited on.
"""
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from bookings import holds, sessions, tasks
from bookings.models import Booking, ClassSession
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

    monkeypatch.setattr("bookings.paystack.initialize_transaction", fake_initialize)


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


def lapse(booking):
    """Move a hold's deadline into the past, bypassing Booking.save()."""
    Booking.objects.filter(pk=booking.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    booking.refresh_from_db()
    return booking


def test_lapsed_hold_is_expired_and_its_slot_becomes_bookable(service, customer, day):
    start_at = datetime.combine(day, time(10), tzinfo=TZ)
    held = holds.create_hold(service=service, customer=customer, start_at=start_at)
    assert held.booking.status == Booking.Status.PENDING_PAYMENT
    lapse(held.booking)

    # No booking attempt to trigger create_hold's local per-resource sweep:
    # the task alone has to notice this row, exactly like production's beat.
    expired = tasks.expire_overdue_holds()

    held.booking.refresh_from_db()
    assert expired == 1
    assert held.booking.status == Booking.Status.EXPIRED
    assert held.booking.expires_at is None

    replacement = holds.create_hold(service=service, customer=customer, start_at=start_at)
    assert replacement.booking.status == Booking.Status.PENDING_PAYMENT


def test_future_and_confirmed_bookings_are_untouched(service, customer, day):
    future = holds.create_hold(
        service=service,
        customer=customer,
        start_at=datetime.combine(day, time(10), tzinfo=TZ),
    )
    confirmed_start = datetime.combine(day, time(11), tzinfo=TZ)
    confirmed = holds.create_hold(service=service, customer=customer, start_at=confirmed_start)
    Booking.objects.filter(pk=confirmed.booking.pk).update(
        status=Booking.Status.CONFIRMED, expires_at=None
    )

    assert tasks.expire_overdue_holds() == 0

    future.booking.refresh_from_db()
    confirmed.booking.refresh_from_db()
    assert future.booking.status == Booking.Status.PENDING_PAYMENT
    assert future.booking.expires_at is not None
    assert confirmed.booking.status == Booking.Status.CONFIRMED


def test_group_hold_expiry_releases_seats_exactly_once(business, resource, day):
    group_service = make_service(
        business, [resource], capacity=6, duration_minutes=60, price_kobo=200_000
    )
    created = sessions.create_session(
        service=group_service,
        resource=resource,
        start_at=datetime.combine(day, time(17), tzinfo=TZ),
        end_at=datetime.combine(day, time(18), tzinfo=TZ),
        capacity=6,
    )
    held = holds.create_hold(
        service=group_service, customer=make_user(), session_id=created.session.pk, party_size=3
    )
    followed = holds.create_hold(
        service=group_service, customer=make_user(), session_id=created.session.pk, party_size=1
    )

    created.session.refresh_from_db()
    assert created.session.seats_taken == 4

    lapse(held.booking)
    assert tasks.expire_overdue_holds() == 1

    created.session.refresh_from_db()
    held.booking.refresh_from_db()
    assert held.booking.status == Booking.Status.EXPIRED
    assert created.session.seats_taken == 1  # only `followed` still holds its seat

    # Second run is a no-op: no double release.
    assert tasks.expire_overdue_holds() == 0
    created.session.refresh_from_db()
    assert created.session.seats_taken == 1


def test_the_session_blocker_is_never_swept(business, resource, day):
    group_service = make_service(
        business, [resource], capacity=6, duration_minutes=60, price_kobo=200_000
    )
    created = sessions.create_session(
        service=group_service,
        resource=resource,
        start_at=datetime.combine(day, time(17), tzinfo=TZ),
        end_at=datetime.combine(day, time(18), tzinfo=TZ),
        capacity=6,
    )

    assert tasks.expire_overdue_holds() == 0

    created.blocker.refresh_from_db()
    assert created.blocker.status == Booking.Status.CONFIRMED
    assert created.blocker.is_blocker is True


@pytest.mark.django_db(transaction=True)
def test_sweep_skips_a_booking_locked_elsewhere(service, customer, day):
    """skip_locked: the sweep must never wait behind a live webhook."""
    held = holds.create_hold(
        service=service,
        customer=customer,
        start_at=datetime.combine(day, time(10), tzinfo=TZ),
    )
    lapse(held.booking)

    results = {}

    def run_sweep():
        try:
            # Prove the worker connection can actually see the row, so a
            # zero result below means "skipped", not "invisible".
            results["visible"] = Booking.objects.filter(pk=held.booking.pk).count()
            results["expired"] = tasks.expire_overdue_holds()
        except Exception as exc:  # pragma: no cover - surfaced by the assertion
            results["error"] = exc
        finally:
            connection.close()

    with transaction.atomic():
        Booking.objects.select_for_update().get(pk=held.booking.pk)
        worker = threading.Thread(target=run_sweep)
        worker.start()
        worker.join(timeout=15)
        # If the sweep had blocked on the locked row, this would still be alive.
        assert not worker.is_alive(), "expiry sweep waited on a locked booking row"

    assert "error" not in results, results.get("error")
    assert results["visible"] == 1
    assert results["expired"] == 0

    held.booking.refresh_from_db()
    assert held.booking.status == Booking.Status.PENDING_PAYMENT

    # Once the lock is gone, the next tick expires it.
    assert tasks.expire_overdue_holds() == 1


def test_beat_schedule_points_at_the_registered_task():
    from config.celery import app as celery_app

    entry = settings.CELERY_BEAT_SCHEDULE["expire-overdue-holds"]
    assert entry["task"] == "bookings.tasks.expire_overdue_holds"
    assert entry["task"] in celery_app.tasks
