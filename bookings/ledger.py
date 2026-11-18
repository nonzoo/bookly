"""Ledger entries — the append-only money truth.

Every entry names the payment it belongs to. The partial unique
constraint (payment, entry_type) makes each transition idempotent: a
retried webhook can call these helpers repeatedly and still produce one
row.

Signs (integer kobo):
  deposit_received  +D  money arrived and is held by the platform
  refund            -D  refund completed; the held money left again
  deposit_forfeited  0  audit marker; deposit becomes the business's
  payout            -X  Paystack Transfer to the business (later)
"""
from .models import LedgerEntry


def _record(payment, booking, entry_type, amount_kobo):
    entry, _created = LedgerEntry.objects.get_or_create(
        payment=payment,
        entry_type=entry_type,
        defaults={
            "business_id": booking.business_id,
            "booking": booking,
            "amount_kobo": amount_kobo,
        },
    )
    return entry


def credit_deposit(payment, booking):
    """Deposit received: credit the business's held balance."""
    return _record(payment, booking, LedgerEntry.Type.DEPOSIT_RECEIVED, payment.amount_kobo)


def record_refund(refund_payment, booking):
    """Refund completed: debit the held balance by the refunded amount."""
    return _record(refund_payment, booking, LedgerEntry.Type.REFUND, -refund_payment.amount_kobo)


def record_forfeit(deposit_payment, booking):
    """Zero-kobo marker: the deposit is now the business's, not refundable.

    No balance change — the credit from deposit_received already moved
    the money. This row records the policy decision for audits.
    """
    return _record(deposit_payment, booking, LedgerEntry.Type.DEPOSIT_FORFEITED, 0)
