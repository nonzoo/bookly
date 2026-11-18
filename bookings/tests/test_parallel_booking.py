"""PROOF #1: N threads, one slot, exactly one winner.

This is the test the whole schema exists for. Every thread passes the
same advisory checks (availability, working hours) — nothing in Python
can distinguish them. The PostgreSQL exclusion constraint admits exactly
one INSERT; the losers surface as SlotTaken.

Mechanics that matter:
- transaction=True: with the default pytest-django transaction wrapper,
  threads would see an empty database (uncommitted rows are invisible)
  and every insert would "succeed".
- Each worker thread runs on its own Django connection and closes it in
  `finally`.
- A barrier releases all threads at the same instant so they genuinely
  race instead of queueing behind thread-start overhead.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.db import connections

from bookings import holds
from bookings.models import Booking, Payment, Service
from bookings.tests.factories import (
    future_weekday,
    make_business,
    make_resource,
    make_service,
    make_user,
    make_working_hours,
)

pytestmark = pytest.mark.django_db(transaction=True)

TZ = ZoneInfo("Africa/Lagos")
THREADS = 8


@pytest.fixture(autouse=True)
def fake_paystack(monkeypatch):
    def fake_initialize(*, email, amount_kobo, reference, metadata=None, timeout=10):
        return {"authorization_url": f"https://paystack.test/{reference}", "reference": reference}

    monkeypatch.setattr("bookings.paystack.initialize_transaction", fake_initialize)


def test_parallel_requests_for_one_slot_have_exactly_one_winner():
    day = future_weekday(2, TZ)
    start_at = datetime.combine(day, time(10), tzinfo=TZ)

    business = make_business()
    resource = make_resource(business)
    make_working_hours(resource, weekday=day.weekday(), start=time(9), end=time(17))
    service = make_service(business, [resource], duration_minutes=30)
    customer = make_user()

    service_id, customer_id = service.pk, customer.pk
    barrier = threading.Barrier(THREADS)

    def attempt(_):
        try:
            barrier.wait(timeout=20)
            local_service = Service.objects.select_related("business").get(pk=service_id)
            local_customer = get_user_model().objects.get(pk=customer_id)
            holds.create_hold(service=local_service, customer=local_customer, start_at=start_at)
            return "won"
        except holds.SlotTaken:
            return "lost"
        except Exception as exc:  # unexpected failures must fail the test loudly
            return f"error:{type(exc).__name__}:{exc}"
        finally:
            connections["default"].close()

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(pool.map(attempt, range(THREADS)))

    assert results.count("won") == 1, results
    assert results.count("lost") == THREADS - 1, results

    # One booking, one payment — no partial writes from the losers.
    assert Booking.objects.count() == 1
    assert Payment.objects.count() == 1
    stored = Booking.objects.get()
    assert stored.status == Booking.Status.PENDING_PAYMENT
    assert stored.start_at == start_at
