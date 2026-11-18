"""Pure pricing tests — no database, no Django setup beyond models.

Per-head for groups, flat for one-to-one, override follows the same
convention. These are the rules the hold service snapshots, so they get
their own fast suite.
"""
from bookings import money
from bookings.models import Business, Service


def make_service(capacity, price_kobo=500_000, override=None, deposit_percent=20):
    business = Business(name="Test", deposit_percent=deposit_percent)
    return Service(
        business=business,
        name="Service",
        duration_minutes=30,
        price_kobo=price_kobo,
        capacity=capacity,
        deposit_kobo_override=override,
    )


def test_one_to_one_is_flat_regardless_of_party_size():
    service = make_service(capacity=1)
    assert money.booking_total_kobo(service, 3) == 500_000
    assert money.deposit_kobo(service, 3) == 100_000  # 20%


def test_group_prices_per_head():
    service = make_service(capacity=10)
    assert money.booking_total_kobo(service, 3) == 1_500_000
    assert money.deposit_kobo(service, 3) == 300_000


def test_override_is_per_head_for_groups_and_flat_for_one_to_one():
    group = make_service(capacity=10, override=40_000)
    assert money.deposit_kobo(group, 3) == 120_000  # per head

    one_to_one = make_service(capacity=1, override=40_000)
    assert money.deposit_kobo(one_to_one, 3) == 40_000  # whole booking


def test_percentage_truncates_instead_of_rounding_up():
    service = make_service(capacity=1, price_kobo=999)  # 20% of 999 = 199.8
    assert money.deposit_kobo(service, 1) == 199
