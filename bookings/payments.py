"""Payment records and provider calls.

Split of responsibilities:
- build_deposit_payment() creates the Payment row. It is called INSIDE
  the hold transaction, because the row belongs to the hold atomically.
- request_deposit() performs the Paystack network call. It must run
  AFTER the hold transaction commits — remote I/O never happens while
  database locks are held.

This module does not decide business consequences (expire the booking,
release seats, mark the payment failed). That belongs to the caller
(holds._request_deposit), which knows what created the payment.
"""
import uuid

from . import paystack
from .models import Payment


def _reference() -> str:
    # Unique by construction (uuid4); Payment.reference's unique
    # constraint is the backstop if entropy ever fails.
    return f"bk_{uuid.uuid4().hex}"


def build_deposit_payment(booking) -> Payment:
    """Create the deposit Payment row for a booking. DB-only, no network."""
    return Payment.objects.create(
        booking=booking,
        kind=Payment.Kind.DEPOSIT,
        reference=_reference(),
        amount_kobo=booking.deposit_kobo,
    )


def request_deposit(payment) -> dict:
    """Initialize the deposit with Paystack and store the provider response.

    Raises paystack.PaystackError on any provider failure; the caller
    decides how to unwind the booking.
    """
    data = paystack.initialize_transaction(
        email=payment.booking.customer.email,
        amount_kobo=payment.amount_kobo,
        reference=payment.reference,
        metadata={"booking_id": str(payment.booking_id)},
    )
    payment.paystack_response = data
    payment.save(update_fields=["paystack_response"])
    return data
