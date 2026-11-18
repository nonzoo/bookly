"""Webhook pipeline tests.

These cover the security and money invariants, not just routing:
- raw-byte signature verification (legacy and timestamped schemes)
- store-first dedup under the unique constraint
- exactly-once ledger credit
- late payment after hold expiry: restore or refund
- refund completion moving the ledger debit
"""
import hashlib
import hmac
import json
import time as clock
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse

from bookings import holds, ledger, refunds, sessions, webhooks
from bookings.models import Booking, LedgerEntry, Payment, WebhookEvent
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
SECRET = "sk_test_secret"
WEBHOOK_SECRET = "whsec_test_secret"


@pytest.fixture(autouse=True)
def paystack_settings(settings):
    settings.PAYSTACK_SECRET_KEY = SECRET
    settings.PAYSTACK_WEBHOOK_SECRET = ""


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    calls = {"initialize": [], "refund": []}

    def fake_initialize(*, email, amount_kobo, reference, metadata=None, timeout=10):
        calls["initialize"].append(reference)
        return {"authorization_url": f"https://paystack.test/{reference}", "reference": reference}

    def fake_refund(*, transaction_reference, amount_kobo, reference, timeout=10):
        calls["refund"].append(
            {"transaction_reference": transaction_reference, "amount_kobo": amount_kobo, "reference": reference}
        )
        return {"status": "pending", "reference": reference, "amount": str(amount_kobo)}

    monkeypatch.setattr("bookings.paystack.initialize_transaction", fake_initialize)
    monkeypatch.setattr("bookings.paystack.create_refund", fake_refund)
    return calls


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
def held(service, customer, day):
    start_at = datetime.combine(day, time(10), tzinfo=TZ)
    return holds.create_hold(service=service, customer=customer, start_at=start_at)


def sign(body: bytes, secret: str = SECRET) -> dict:
    return {
        "x-paystack-signature": hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
    }


def deliver(payload, *, secret=SECRET, extra_headers=None):
    body = json.dumps(payload).encode("utf-8")
    headers = sign(body, secret)
    if extra_headers:
        headers.update(extra_headers)
    return webhooks.receive(body, headers), body


def charge_payload(payment, *, event="charge.success", **data_overrides):
    data = {
        "reference": payment.reference,
        "amount": payment.amount_kobo,
        "status": "success" if event == "charge.success" else "failed",
    }
    data.update(data_overrides)
    return {"event": event, "data": data}


def test_charge_success_confirms_booking_and_credits_ledger(held):
    payment = held.payment
    result, _ = deliver(charge_payload(payment))

    assert result.http_status == 200
    held.booking.refresh_from_db()
    payment.refresh_from_db()

    assert held.booking.status == Booking.Status.CONFIRMED
    assert held.booking.expires_at is None
    assert payment.status == Payment.Status.SUCCESS
    assert payment.paid_at is not None

    entry = LedgerEntry.objects.get(payment=payment, entry_type=LedgerEntry.Type.DEPOSIT_RECEIVED)
    assert entry.amount_kobo == payment.amount_kobo
    assert entry.business_id == held.booking.business_id


def test_duplicate_delivery_credits_once(held):
    payload = charge_payload(held.payment)

    first, _ = deliver(payload)
    second, _ = deliver(payload)

    assert (first.http_status, second.http_status) == (200, 200)
    assert WebhookEvent.objects.count() == 1
    assert LedgerEntry.objects.filter(entry_type=LedgerEntry.Type.DEPOSIT_RECEIVED).count() == 1


def test_invalid_signature_is_stored_but_never_processed(held):
    result, _ = deliver(charge_payload(held.payment), secret="wrong-secret")

    assert result.http_status == 401
    event = WebhookEvent.objects.get()
    assert event.signature_valid is False
    assert event.processed_at is None
    assert "Invalid signature" in event.error

    held.booking.refresh_from_db()
    held.payment.refresh_from_db()
    assert held.booking.status == Booking.Status.PENDING_PAYMENT
    assert held.payment.status == Payment.Status.INITIATED
    assert LedgerEntry.objects.count() == 0


def test_timestamped_signature_scheme_accepts_fresh_and_rejects_stale(settings, held):
    settings.PAYSTACK_WEBHOOK_SECRET = WEBHOOK_SECRET
    payload = charge_payload(held.payment)
    body = json.dumps(payload).encode("utf-8")

    now = int(clock.time())
    signature = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), f"{now}.".encode("ascii") + body, hashlib.sha512
    ).hexdigest()
    fresh = {"Paystack-Signature": f"t={now},v1=whk_test:{signature}"}

    result = webhooks.receive(body, fresh)
    assert result.http_status == 200
    assert result.event.signature_valid is True

    # New event, but signed 400 seconds ago: replay window closed.
    other_payload = charge_payload(held.payment, reference="another-ref")
    other_body = json.dumps(other_payload).encode("utf-8")
    stale = now - 400
    stale_signature = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), f"{stale}.".encode("ascii") + other_body, hashlib.sha512
    ).hexdigest()
    stale_result = webhooks.receive(
        other_body, {"Paystack-Signature": f"t={stale},v1=whk_test:{stale_signature}"}
    )
    assert stale_result.http_status == 401


def test_amount_mismatch_is_acknowledged_as_permanent(held):
    result, _ = deliver(charge_payload(held.payment, amount=held.payment.amount_kobo + 1))

    assert result.http_status == 200
    result.event.refresh_from_db()
    assert result.event.processed_at is not None
    assert "Amount mismatch" in result.event.error

    held.payment.refresh_from_db()
    held.booking.refresh_from_db()
    assert held.payment.status == Payment.Status.INITIATED
    assert held.booking.status == Booking.Status.PENDING_PAYMENT
    assert LedgerEntry.objects.count() == 0


def test_unknown_charge_reference_is_retryable():
    payload = {
        "event": "charge.success",
        "data": {"reference": "does-not-exist", "amount": 100, "status": "success"},
    }
    result, _ = deliver(payload)

    assert result.http_status == 500
    event = WebhookEvent.objects.get()
    assert event.processed_at is None
    assert "No deposit payment" in event.error


def test_unknown_event_type_is_acknowledged():
    result, _ = deliver({"event": "subscription.create", "data": {"id": 99}})

    assert result.http_status == 200
    event = WebhookEvent.objects.get()
    assert event.processed_at is not None
    assert event.error == "ignored: subscription.create"


def test_charge_failed_expires_group_booking_and_releases_seats(business, resource, day):
    group_service = make_service(business, [resource], capacity=6, duration_minutes=60, price_kobo=200_000)
    created = sessions.create_session(
        service=group_service,
        resource=resource,
        start_at=datetime.combine(day, time(17), tzinfo=TZ),
        end_at=datetime.combine(day, time(18), tzinfo=TZ),
        capacity=6,
    )
    hold = holds.create_hold(
        service=group_service, customer=make_user(), session_id=created.session.pk, party_size=2
    )
    created.session.refresh_from_db()
    assert created.session.seats_taken == 2

    result, _ = deliver(charge_payload(hold.payment, event="charge.failed"))

    assert result.http_status == 200
    hold.booking.refresh_from_db()
    hold.payment.refresh_from_db()
    created.session.refresh_from_db()
    assert hold.booking.status == Booking.Status.EXPIRED
    assert hold.payment.status == Payment.Status.FAILED
    assert created.session.seats_taken == 0


def test_late_payment_restores_expired_slot_when_still_free(held):
    Booking.objects.filter(pk=held.booking.pk).update(
        status=Booking.Status.EXPIRED, expires_at=None
    )

    result, _ = deliver(charge_payload(held.payment))

    assert result.http_status == 200
    held.booking.refresh_from_db()
    held.payment.refresh_from_db()
    assert held.booking.status == Booking.Status.CONFIRMED
    assert held.payment.status == Payment.Status.SUCCESS
    assert LedgerEntry.objects.filter(payment=held.payment, entry_type=LedgerEntry.Type.DEPOSIT_RECEIVED).exists()
    assert not Payment.objects.filter(kind=Payment.Kind.REFUND).exists()


def test_late_payment_refunds_when_slot_was_taken(held, business, resource, service, customer):
    Booking.objects.filter(pk=held.booking.pk).update(
        status=Booking.Status.EXPIRED, expires_at=None
    )
    # Someone else now owns the slot.
    Booking.objects.create(
        business=business,
        resource=resource,
        service=service,
        customer=customer,
        start_at=held.booking.start_at,
        end_at=held.booking.end_at,
        status=Booking.Status.CONFIRMED,
        price_kobo=0,
        deposit_kobo=0,
    )

    result, _ = deliver(charge_payload(held.payment))

    assert result.http_status == 200
    held.booking.refresh_from_db()
    held.payment.refresh_from_db()
    refund = Payment.objects.get(kind=Payment.Kind.REFUND)

    assert held.booking.status == Booking.Status.EXPIRED
    assert held.payment.status == Payment.Status.SUCCESS
    assert refund.status == Payment.Status.INITIATED
    assert refund.amount_kobo == held.payment.amount_kobo
    # Deposit is credited on receipt; the refund debit waits for processed.
    assert LedgerEntry.objects.filter(
        payment=held.payment, entry_type=LedgerEntry.Type.DEPOSIT_RECEIVED
    ).exists()
    assert not LedgerEntry.objects.filter(entry_type=LedgerEntry.Type.REFUND).exists()


def make_successful_deposit(held):
    Payment.objects.filter(pk=held.payment.pk).update(status=Payment.Status.SUCCESS)
    held.payment.refresh_from_db()
    ledger.credit_deposit(held.payment, held.booking)
    return refunds.build_refund_payment(held.booking)


def test_refund_processed_marks_deposit_refunded_and_debits_ledger(held):
    refund = make_successful_deposit(held)
    payload = {
        "event": "refund.processed",
        "data": {
            "refund_reference": refund.reference,
            "transaction_reference": held.payment.reference,
            "amount": str(refund.amount_kobo),
            "status": "processed",
        },
    }

    result, _ = deliver(payload)

    assert result.http_status == 200
    refund.refresh_from_db()
    held.payment.refresh_from_db()
    assert refund.status == Payment.Status.SUCCESS
    assert held.payment.status == Payment.Status.REFUNDED

    entry = LedgerEntry.objects.get(payment=refund, entry_type=LedgerEntry.Type.REFUND)
    assert entry.amount_kobo == -refund.amount_kobo

    # Re-delivery must not double-debit.
    deliver(payload)
    assert LedgerEntry.objects.filter(entry_type=LedgerEntry.Type.REFUND).count() == 1


def test_refund_failed_leaves_the_deposit_held(held):
    refund = make_successful_deposit(held)
    payload = {
        "event": "refund.failed",
        "data": {
            "refund_reference": refund.reference,
            "transaction_reference": held.payment.reference,
            "amount": str(refund.amount_kobo),
            "status": "failed",
        },
    }

    result, _ = deliver(payload)

    assert result.http_status == 200
    refund.refresh_from_db()
    held.payment.refresh_from_db()
    assert refund.status == Payment.Status.FAILED
    assert held.payment.status == Payment.Status.SUCCESS  # still held
    assert not LedgerEntry.objects.filter(entry_type=LedgerEntry.Type.REFUND).exists()


def test_http_endpoint_verifies_signature(client, held):
    payload = charge_payload(held.payment)
    body = json.dumps(payload).encode("utf-8")
    url = reverse("paystack-webhook")

    bad = client.post(url, data=body, content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE="00" * 64)
    assert bad.status_code == 401

    good = client.post(
        url,
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=sign(body)["x-paystack-signature"],
    )
    assert good.status_code == 200
    held.booking.refresh_from_db()
    assert held.booking.status == Booking.Status.CONFIRMED
