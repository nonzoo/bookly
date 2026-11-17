"""Money math — integer kobo only, pure functions.

Kept ORM-free (no queries, no transaction state) so the pricing rules are
unit-testable without a database. This module is the single source of
truth for the locked pricing decision:

- one-to-one services (capacity == 1): price_kobo is the whole booking.
- group services (capacity > 1): price_kobo is PER PERSON; the booking
  total is price_kobo * party_size, and deposit_kobo_override follows the
  same per-head convention, so a class owner can say "2000 naira deposit
  per head".
- a computed deposit of 0 kobo means "no payment needed" — the hold
  service confirms such bookings immediately, with no Paystack round
  trip.

Snapshots: the hold service copies the results onto the Booking row at
creation time (price_kobo / deposit_kobo), so later price edits can never
rewrite history.
"""


def booking_total_kobo(service, party_size: int) -> int:
    """Total price for one booking of `service` with `party_size` people."""
    if service.is_group:
        return service.price_kobo * party_size
    return service.price_kobo


def deposit_kobo(service, party_size: int) -> int:
    """Deposit due at booking time. Integer floor division on purpose:
    the platform never rounds a deposit up by a fraction of a kobo."""
    if service.deposit_kobo_override is not None:
        heads = party_size if service.is_group else 1
        return service.deposit_kobo_override * heads
    total = booking_total_kobo(service, party_size)
    return total * service.business.deposit_percent // 100
