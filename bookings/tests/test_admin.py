"""The admin is an ops surface, not a second write path.

The dangerous button it used to expose was flipping ClassSession.is_cancelled,
which silently left the blocker live and the seats taken. These tests pin
the replacement: the state fields are read-only, and the bulk action runs
lifecycle.cancel_session (blocker, seats, refunds and all).
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.urls import reverse

from bookings import holds, sessions
from bookings.models import Booking, ClassSession, Payment
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
def room(business):
    return make_resource(business, name="Studio", max_party_size=10)


@pytest.fixture
def group_service(business, room):
    return make_service(
        business, [room], capacity=6, duration_minutes=60, price_kobo=200_000
    )


@pytest.fixture
def session(group_service, room, day):
    return sessions.create_session(
        service=group_service,
        resource=room,
        start_at=datetime.combine(day, time(17), tzinfo=TZ),
        end_at=datetime.combine(day, time(18), tzinfo=TZ),
        capacity=6,
    )


def test_seat_and_cancel_fields_are_read_only_in_admin():
    model_admin = django_admin.site._registry[ClassSession]

    assert "seats_taken" in model_admin.readonly_fields
    assert "is_cancelled" in model_admin.readonly_fields


def test_admin_cancel_action_releases_everything(client, session):
    held = holds.create_hold(
        service=session.session.service,
        customer=make_user(),
        session_id=session.session.pk,
        party_size=2,
    )
    Payment.objects.filter(pk=held.payment.pk).update(status=Payment.Status.SUCCESS)

    session.session.refresh_from_db()
    assert session.session.seats_taken == 2

    admin_user = get_user_model().objects.create_superuser(
        username="ops", email="ops@example.com", password="test-pass-123"
    )
    client.force_login(admin_user)

    response = client.post(
        reverse("admin:bookings_classsession_changelist"),
        {
            "action": "cancel_sessions",
            "_selected_action": [str(session.session.pk)],
            "select_across": "0",
        },
    )

    assert response.status_code == 302
    session.session.refresh_from_db()
    session.blocker.refresh_from_db()
    held.booking.refresh_from_db()

    assert session.session.is_cancelled is True
    assert session.blocker.status == Booking.Status.CANCELLED_BY_BUSINESS
    assert held.booking.status == Booking.Status.CANCELLED_BY_BUSINESS
    assert session.session.seats_taken == 0
    assert Payment.objects.filter(kind=Payment.Kind.REFUND, booking=held.booking).exists()
