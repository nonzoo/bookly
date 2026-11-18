"""Hand-rolled test builders.

Deliberately no factory_boy dependency: for a schema this small, explicit
helpers are clearer and keep the dependency list minimal.

Dates in tests are always computed relative to "now" (`future_weekday`)
so the suite never rots into the past.
"""
import uuid
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from bookings.models import Business, Resource, Service, WorkingHours


def make_user():
    suffix = uuid.uuid4().hex[:8]
    return get_user_model().objects.create_user(
        username=f"user-{suffix}",
        email=f"user-{suffix}@example.com",
        password="test-pass-123",
    )


def make_business(owner=None, **overrides):
    owner = owner or make_user()
    defaults = {
        "owner": owner,
        "name": "Test Business",
        "slug": f"biz-{uuid.uuid4().hex[:8]}",
        "timezone": "Africa/Lagos",
        "deposit_percent": 20,
        "free_cancellation_hours": 24,
        "hold_minutes": 15,
    }
    defaults.update(overrides)
    return Business.objects.create(**defaults)


def make_resource(business, **overrides):
    defaults = {"business": business, "name": "Resource", "kind": Resource.Kind.PERSON}
    defaults.update(overrides)
    return Resource.objects.create(**defaults)


def make_service(business, resources, **overrides):
    defaults = {
        "business": business,
        "name": "Service",
        "duration_minutes": 30,
        "price_kobo": 500_000,  # ₦5,000
        "capacity": 1,
    }
    defaults.update(overrides)
    service = Service.objects.create(**defaults)
    service.resources.set(resources)
    return service


def make_working_hours(resource, weekday, start, end):
    return WorkingHours.objects.create(resource=resource, weekday=weekday, start_time=start, end_time=end)


def future_weekday(weekday, tz) -> date:
    """The next occurrence of `weekday` strictly in the future, in `tz`.

    Guarantees availability windows and sessions built on this date are
    never in the past, however long this repo lives.
    """
    today = timezone.now().astimezone(tz).date()
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta or 7)
