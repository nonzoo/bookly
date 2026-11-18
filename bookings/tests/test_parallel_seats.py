"""PROOF #2: concurrent seat grabs never oversell a class.

Same mechanics as test_parallel_booking (real transactions, per-thread
connections, barrier start). The serialization point is different: all
threads lock the same session row (FOR UPDATE), release any lapsed
holds, then the conditional seat UPDATE decides — exactly `capacity`
threads can win, the rest get SessionFull.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.db import connections

from bookings import holds
from bookings.models import Booking, ClassSession, Service
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
CAPACITY = 3


@pytest.fixture(autouse=True)
def fake_paystack(monkeypatch):
    def fake_initialize(*, email, amount_kobo, reference, metadata=None, timeout=10):
        return {"authorization_url": f"https://paystack.test/{reference}", "reference": reference}

    monkeypatch.setattr("bookings.paystack.initialize_transaction", fake_initialize)


def test_parallel_seat_grabs_never_oversell():
    day = future_weekday(2, TZ)

    business = make_business()
    resource = make_resource(business)
    make_working_hours(resource, weekday=day.weekday(), start=time(9), end=time(20))
    service = make_service(business, [resource], capacity=8, duration_minutes=60, price_kobo=200_000)
    session = ClassSession.objects.create(
        service=service,
        resource=resource,
        start_at=datetime.combine(day, time(17), tzinfo=TZ),
        end_at=datetime.combine(day, time(18), tzinfo=TZ),
        capacity=CAPACITY,
    )
    customer = make_user()

    service_id, session_id, customer_id = service.pk, session.pk, customer.pk
    barrier = threading.Barrier(THREADS)

    def attempt(_):
        try:
            barrier.wait(timeout=20)
            local_service = Service.objects.select_related("business").get(pk=service_id)
            local_customer = get_user_model().objects.get(pk=customer_id)
            holds.create_hold(
                service=local_service, customer=local_customer, session_id=session_id, party_size=1
            )
            return "seated"
        except holds.SessionFull:
            return "full"
        except Exception as exc:
            return f"error:{type(exc).__name__}:{exc}"
        finally:
            connections["default"].close()

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(pool.map(attempt, range(THREADS)))

    assert results.count("seated") == CAPACITY, results
    assert results.count("full") == THREADS - CAPACITY, results

    session.refresh_from_db()
    assert session.seats_taken == CAPACITY  # never above, never below
    assert Booking.objects.count() == CAPACITY
