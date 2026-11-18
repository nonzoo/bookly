"""Customer API tests.

Two things are being pinned here:
1. the wiring — each endpoint calls the service layer and maps its domain
   exceptions to the documented status codes;
2. the guards — authentication, ownership (404, not 403), and per-view
   throttling that applies to POSTs without limiting reads.

Throttle tests replace the rates rather than firing 11 real requests, and
clear Django's cache so history never leaks between tests.
"""
from datetime import datetime, time, timedelta
import uuid
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from bookings import holds, lifecycle, paystack, sessions
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
def clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    def fake_initialize(*, email, amount_kobo, reference, metadata=None, timeout=10):
        return {
            "authorization_url": f"https://paystack.test/{reference}",
            "reference": reference,
        }

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
def client_for(customer):
    client = APIClient()
    client.force_authenticate(user=customer)
    return client


def dt(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


def parse(value):
    """Parse an ISO datetime from either renderer (Z or +01:00)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def create_payload(service, day, hour=10, **overrides):
    payload = {
        "service": service.pk,
        "start_at": dt(day, hour).isoformat(),
        "party_size": 1,
    }
    payload.update(overrides)
    return payload


def test_endpoints_require_authentication(service, day):
    client = APIClient()
    assert client.get(reverse("booking-list-create")).status_code == 401
    assert client.post(reverse("booking-list-create"), create_payload(service, day)).status_code == 401
    assert client.get(reverse("booking-detail", args=[uuid.uuid4()])).status_code == 401
    assert client.post(reverse("payment-verify"), {"reference": "x"}).status_code == 401


def test_catalog_and_availability_are_readable_without_an_account(service, day):
    """Discovery is public; only creating and reading a booking needs auth."""
    client = APIClient()

    availability = client.get(
        reverse("service-availability", args=[service.pk]), {"date": day.isoformat()}
    )
    assert availability.status_code == 200

    catalog = client.get(reverse("business-detail", args=[service.business.slug]))
    assert catalog.status_code == 200


def test_storefront_exposes_branding_policy_and_active_services(business, resource, service):
    business.description = "Braids, cuts and colour in Lekki."
    business.phone = "+2348000000000"
    business.logo_url = "https://cdn.example.com/logo.png"
    business.brand_color = "#123456"
    business.save()
    make_service(business, [resource], name="Aardvark (retired)", is_active=False)

    response = APIClient().get(reverse("business-detail", args=[business.slug]))

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == business.name
    assert body["brand_color"] == "#123456"
    assert body["free_cancellation_hours"] == 24
    assert body["hold_minutes"] == 15
    assert [item["name"] for item in body["services"]] == ["Service"]
    assert body["services"][0]["deposit_kobo"] == 100_000  # 20% of ₦5,000
    assert body["services"][0]["resources"][0]["name"] == "Ada"

    assert APIClient().get(reverse("business-detail", args=["no-such-shop"])).status_code == 404


def test_availability_returns_grid_slots(client_for, service, day):
    response = client_for.get(
        reverse("service-availability", args=[service.pk]), {"date": day.isoformat()}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "slots"
    starts = [parse(slot["start_at"]) for slot in body["slots"]]
    assert dt(day, 9) in starts
    assert dt(day, 19, 30) in starts
    assert all(slot["resource_id"] == service.resources.get().pk for slot in body["slots"])


def test_availability_returns_group_sessions_with_seats_left(client_for, business, resource, day):
    group_service = make_service(
        business, [resource], capacity=6, duration_minutes=60, price_kobo=200_000
    )
    created = sessions.create_session(
        service=group_service,
        resource=resource,
        start_at=dt(day, 17),
        end_at=dt(day, 18),
        capacity=6,
    )
    holds.create_hold(
        service=group_service, customer=make_user(), session_id=created.session.pk, party_size=2
    )

    response = client_for.get(
        reverse("service-availability", args=[group_service.pk]), {"date": day.isoformat()}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "sessions"
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_id"] == created.session.pk
    assert body["sessions"][0]["seats_left"] == 4


def test_create_hold_returns_booking_and_payment_url(client_for, service, day):
    response = client_for.post(reverse("booking-list-create"), create_payload(service, day))

    assert response.status_code == 201
    body = response.json()
    booking = Booking.objects.get(pk=body["booking"]["id"])
    assert booking.customer_id == client_for.handler._force_user.pk
    assert booking.status == Booking.Status.PENDING_PAYMENT
    assert body["booking"]["price_kobo"] == 500_000
    assert body["payment_reference"] == booking.payments.get().reference
    assert body["authorization_url"] == f"https://paystack.test/{booking.payments.get().reference}"


def test_overlapping_create_is_a_conflict(client_for, service, customer, day):
    assert client_for.post(reverse("booking-list-create"), create_payload(service, day)).status_code == 201
    second = client_for.post(reverse("booking-list-create"), create_payload(service, day))

    assert second.status_code == 409
    assert "taken" in second.json()["detail"].lower()


def test_outside_working_hours_is_a_bad_request(client_for, resource, service, day):
    response = client_for.post(
        reverse("booking-list-create"),
        create_payload(service, day, hour=3, resource=resource.pk),
    )

    assert response.status_code == 400
    assert "working hours" in response.json()["detail"].lower()


def test_group_booking_requires_a_session(client_for, business, resource):
    group_service = make_service(business, [resource], capacity=6, duration_minutes=60)

    response = client_for.post(
        reverse("booking-list-create"), {"service": group_service.pk, "party_size": 1}
    )

    assert response.status_code == 400
    assert "session" in response.json()["detail"].lower()


def test_my_bookings_are_only_mine_and_never_blockers(client_for, customer, service, business, resource, day):
    mine = holds.create_hold(
        service=service, customer=customer, start_at=dt(day, 10)
    )
    holds.create_hold(service=service, customer=make_user(), start_at=dt(day, 11))

    group_service = make_service(business, [resource], capacity=6, duration_minutes=60)
    sessions.create_session(
        service=group_service, resource=resource, start_at=dt(day, 17), end_at=dt(day, 18), capacity=6
    )

    response = client_for.get(reverse("booking-list-create"))

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()}
    assert ids == {str(mine.booking.pk)}

    filtered = client_for.get(
        reverse("booking-list-create"), {"status": Booking.Status.PENDING_PAYMENT}
    )
    assert filtered.json()[0]["id"] == str(mine.booking.pk)

    unknown = client_for.get(reverse("booking-list-create"), {"status": "nonsense"})
    assert unknown.status_code == 400


def test_cancelling_an_unpaid_hold_frees_it(client_for, customer, service, day):
    held = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))

    response = client_for.post(reverse("booking-cancel", args=[held.booking.pk]))

    assert response.status_code == 200
    body = response.json()
    assert body["booking"]["status"] == Booking.Status.CANCELLED_BY_CUSTOMER
    assert body["refund_status"] is None
    assert body["forfeited"] is False

    replacement = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    assert replacement.booking.status == Booking.Status.PENDING_PAYMENT


def test_free_cancellation_reports_the_refund(client_for, customer, business, service, day):
    business.free_cancellation_hours = 0
    business.save()
    held = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    Payment.objects.filter(pk=held.payment.pk).update(status=Payment.Status.SUCCESS)

    response = client_for.post(reverse("booking-cancel", args=[held.booking.pk]))

    assert response.status_code == 200
    assert response.json()["refund_status"] == Payment.Status.INITIATED
    assert response.json()["forfeited"] is False


def test_cancelling_someone_elses_booking_is_not_found(client_for, service, day):
    other = make_user()
    held = holds.create_hold(service=service, customer=other, start_at=dt(day, 10))

    response = client_for.post(reverse("booking-cancel", args=[held.booking.pk]))

    assert response.status_code == 404
    held.booking.refresh_from_db()
    assert held.booking.status == Booking.Status.PENDING_PAYMENT


def test_cancelling_a_terminal_booking_is_a_conflict(client_for, customer, service, day):
    held = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    Booking.objects.filter(pk=held.booking.pk).update(status=Booking.Status.COMPLETED)

    response = client_for.post(reverse("booking-cancel", args=[held.booking.pk]))

    assert response.status_code == 409


def test_booking_detail_is_owner_only(client_for, customer, service, day):
    mine = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    other = holds.create_hold(service=service, customer=make_user(), start_at=dt(day, 11))

    response = client_for.get(reverse("booking-detail", args=[mine.booking.pk]))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(mine.booking.pk)
    assert body["free_cancellation_hours"] == 24
    assert body["business_slug"] == mine.booking.business.slug

    # Someone else's id is indistinguishable from an id that does not exist.
    assert client_for.get(reverse("booking-detail", args=[other.booking.pk])).status_code == 404


@pytest.fixture
def provider_verify(monkeypatch):
    """Patch Paystack's verify call. Set .response or .error per test."""
    calls = []

    def fake_verify(reference, timeout=10):
        calls.append(reference)
        if fake_verify.error is not None:
            raise fake_verify.error
        return fake_verify.response

    fake_verify.calls = calls
    fake_verify.response = {"status": "pending"}
    fake_verify.error = None
    monkeypatch.setattr("bookings.paystack.verify_transaction", fake_verify)
    return fake_verify


def test_verify_applies_a_successful_charge(client_for, customer, service, day, provider_verify):
    held = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    provider_verify.response = {"status": "success", "amount": held.payment.amount_kobo}

    response = client_for.post(
        reverse("payment-verify"), {"reference": held.payment.reference}, format="json"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["payment"]["status"] == Payment.Status.SUCCESS
    assert body["booking"]["status"] == Booking.Status.CONFIRMED

    held.booking.refresh_from_db()
    held.payment.refresh_from_db()
    assert held.booking.status == Booking.Status.CONFIRMED
    assert held.payment.status == Payment.Status.SUCCESS
    assert provider_verify.calls == [held.payment.reference]


def test_verify_is_idempotent_and_stops_calling_the_provider(
    client_for, customer, service, day, provider_verify
):
    held = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    provider_verify.response = {"status": "success", "amount": held.payment.amount_kobo}
    payload = {"reference": held.payment.reference}

    first = client_for.post(reverse("payment-verify"), payload, format="json")
    second = client_for.post(reverse("payment-verify"), payload, format="json")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["payment"]["status"] == Payment.Status.SUCCESS
    # A settled payment is answered from the database, not from Paystack.
    assert provider_verify.calls == [held.payment.reference]


def test_verify_restores_an_expired_hold_while_the_slot_is_free(
    client_for, customer, service, day, provider_verify
):
    held = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    # Expiry freed the slot: the exclusion constraint only covers active rows.
    Booking.objects.filter(pk=held.booking.pk).update(
        status=Booking.Status.EXPIRED, expires_at=None
    )
    provider_verify.response = {"status": "success", "amount": held.payment.amount_kobo}

    response = client_for.post(
        reverse("payment-verify"), {"reference": held.payment.reference}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["booking"]["status"] == Booking.Status.CONFIRMED


def test_verify_rejects_an_amount_that_differs_from_the_charge(
    client_for, customer, service, day, provider_verify
):
    held = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    provider_verify.response = {
        "status": "success",
        "amount": held.payment.amount_kobo + 1,
    }

    response = client_for.post(
        reverse("payment-verify"), {"reference": held.payment.reference}, format="json"
    )

    assert response.status_code == 409
    assert "amount mismatch" in response.json()["detail"].lower()
    held.booking.refresh_from_db()
    held.payment.refresh_from_db()
    assert held.payment.status == Payment.Status.INITIATED
    assert held.booking.status == Booking.Status.PENDING_PAYMENT


def test_verify_cannot_touch_someone_elses_payment(client_for, service, day, provider_verify):
    other = holds.create_hold(service=service, customer=make_user(), start_at=dt(day, 10))

    response = client_for.post(
        reverse("payment-verify"), {"reference": other.payment.reference}, format="json"
    )

    assert response.status_code == 404
    assert provider_verify.calls == []
    other.payment.refresh_from_db()
    assert other.payment.status == Payment.Status.INITIATED


def test_verify_reports_a_provider_outage_as_bad_gateway(
    client_for, customer, service, day, provider_verify
):
    held = holds.create_hold(service=service, customer=customer, start_at=dt(day, 10))
    provider_verify.error = paystack.PaystackError("provider down")

    response = client_for.post(
        reverse("payment-verify"), {"reference": held.payment.reference}, format="json"
    )

    assert response.status_code == 502


def test_verify_requires_a_reference(client_for, provider_verify):
    response = client_for.post(reverse("payment-verify"), {}, format="json")

    assert response.status_code == 400
    assert provider_verify.calls == []


@override_settings(PAYSTACK_CALLBACK_URL="https://app.example.com/pay/return")
def test_deposit_initialization_forwards_the_callback_url(
    client_for, service, day, monkeypatch
):
    captured = {}

    def fake_initialize(**kwargs):
        captured.update(kwargs)
        return {
            "authorization_url": f"https://paystack.test/{kwargs['reference']}",
            "reference": kwargs["reference"],
        }

    monkeypatch.setattr("bookings.paystack.initialize_transaction", fake_initialize)

    response = client_for.post(reverse("booking-list-create"), create_payload(service, day))

    assert response.status_code == 201
    assert captured["callback_url"] == "https://app.example.com/pay/return"
    assert captured["metadata"]["booking_id"] == response.json()["booking"]["id"]


@pytest.fixture
def tight_throttles(monkeypatch):
    # SimpleRateThrottle.THROTTLE_RATES is bound to api_settings' dict at
    # import time, so override_settings(REST_FRAMEWORK=...) is too late.
    # Patching the dict is both the honest mechanism and auto-restored.
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setitem(SimpleRateThrottle.THROTTLE_RATES, "booking_create", "1/min")
    monkeypatch.setitem(SimpleRateThrottle.THROTTLE_RATES, "webhook", "1/min")


def test_booking_create_is_throttled_but_listing_is_not(client_for, service, day, tight_throttles):
    first = client_for.post(reverse("booking-list-create"), create_payload(service, day))
    second = client_for.post(reverse("booking-list-create"), create_payload(service, day, hour=11))

    assert first.status_code == 201
    assert second.status_code == 429

    # Reads never touch that bucket, however many POSTs were refused.
    assert client_for.get(reverse("booking-list-create")).status_code == 200


def test_webhook_is_throttled_per_ip(client, tight_throttles):
    url = reverse("paystack-webhook")
    junk = b"not-json"

    first = client.post(url, data=junk, content_type="application/json")
    second = client.post(url, data=junk, content_type="application/json")

    assert first.status_code == 400
    assert second.status_code == 429


def test_webhook_rejects_get(client):
    assert client.get(reverse("paystack-webhook")).status_code == 405
