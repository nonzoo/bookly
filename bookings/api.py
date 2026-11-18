"""Customer-facing JSON API — function-based DRF views.

Every view is a thin adapter: authenticate (JWT, global default), validate
shape via a serializer, call ONE service function, translate domain
exceptions to HTTP. No business rules live here — holds, lifecycle,
payments and availability already own them for the webhook and admin paths
too, and duplicating a rule in a view is how the two copies start
disagreeing.

Public (AllowAny) routes are read-only discovery: the storefront catalog
and slot availability. Creating a booking, reading it and verifying its
payment all require the owning account.

HTTP mapping (consistent across endpoints):
- unauthenticated                    401 (DRF default)
- malformed body / missing field     400 (serializer)
- InvalidBooking (business rule)     400
- SlotTaken / SessionFull            409 (real conflict; pick another time)
- PaymentInitiationFailed            502 (upstream provider failed)
- InvalidTransition                  409 (wrong state for the action)
- AmountMismatch on verify           409 (provider paid amount differs)
- Paystack verify failure            502 (upstream provider failed)
- not found / not yours              404 (no existence leak)
"""
import logging

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import availability, holds, lifecycle, payments, paystack
from .models import Booking, Business, Payment, Service
from .serializers import (
    AvailabilityQuerySerializer,
    BookingSerializer,
    HoldCreateSerializer,
    PublicBusinessSerializer,
)
from .throttles import BookingCreateThrottle, PaymentVerifyThrottle

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([AllowAny])
def business_detail(request, slug):
    """Public storefront: branding, policy and the active service menu.

    Inactive services are filtered in the prefetch, so rendering the menu
    costs one extra query instead of one per service.
    """
    business = get_object_or_404(
        Business.objects.prefetch_related(
            Prefetch(
                "services",
                queryset=Service.objects.filter(is_active=True)
                .select_related("business")
                .prefetch_related("resources")
                .order_by("name", "id"),
            )
        ),
        slug=slug,
    )
    return Response(PublicBusinessSerializer(business).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def service_availability(request, service_id):
    """Advisory slots (one-to-one) or sessions with seats left (group).

    Public on purpose: a customer must be able to see times before being
    asked to create an account. The response can be stale the instant it
    is rendered; the create endpoint is where the database has the final
    word.
    """
    service = get_object_or_404(
        Service.objects.select_related("business"), pk=service_id, is_active=True
    )
    query = AvailabilityQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    day = query.validated_data["date"]

    if service.is_group:
        return Response(
            {"mode": "sessions", "sessions": availability.sessions_for_day(service, day)}
        )
    slots = availability.slots_for_day(
        service, day, resource_id=query.validated_data.get("resource")
    )
    return Response({"mode": "slots", "slots": slots})



@api_view(["GET", "POST"])
@throttle_classes([BookingCreateThrottle])
def bookings(request):
    """GET: the caller's bookings. POST: create a hold.

    One URL, two verbs — the throttle class itself exempts GET, so listing
    stays free while creating cannot be hammered.
    """
    if request.method == "POST":
        return _create_hold(request)
    return _list_bookings(request)


def _list_bookings(request):
    bookings_qs = (
        Booking.objects.filter(customer=request.user, is_blocker=False)
        .select_related("business", "service", "resource")
        .order_by("-start_at")
    )
    status_filter = request.query_params.get("status")
    if status_filter:
        if status_filter not in Booking.Status.values:
            return Response({"detail": "Unknown status."}, status=status.HTTP_400_BAD_REQUEST)
        bookings_qs = bookings_qs.filter(status=status_filter)
    return Response(BookingSerializer(bookings_qs, many=True).data)


def _create_hold(request):
    serializer = HoldCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        result = holds.create_hold(
            service=data["service"],
            customer=request.user,
            start_at=data.get("start_at"),
            resource_id=data["resource"].pk if data.get("resource") else None,
            session_id=data["session"].pk if data.get("session") else None,
            party_size=data.get("party_size", 1),
            address=data.get("address", ""),
            notes=data.get("notes", ""),
        )
    except holds.InvalidBooking as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except (holds.SlotTaken, holds.SessionFull) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    except holds.PaymentInitiationFailed as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    return Response(
        {
            "booking": BookingSerializer(result.booking).data,
            "authorization_url": result.authorization_url,
            "payment_reference": result.payment.reference if result.payment else None,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
def cancel_booking(request, booking_id):
    """Customer-side cancellation of the caller's own booking.

    The service decides refund vs forfeit from the notice window; the view
    only reports what happened. Someone else's booking is a 404, not a
    403: the API does not confirm that the id exists.
    """
    booking = Booking.objects.filter(
        pk=booking_id, customer=request.user, is_blocker=False
    ).first()
    if booking is None:
        return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        result = lifecycle.cancel_by_customer(booking)
    except lifecycle.InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

    return Response(
        {
            "booking": BookingSerializer(result.booking).data,
            "refund_status": result.refund.status if result.refund else None,
            "forfeited": result.forfeited,
        }
    )


@api_view(["GET"])
def booking_detail(request, booking_id):
    """One booking, for the owner only.

    The payment-return page polls this (or verify_payment) until the
    webhook settles the state. Someone else's booking is a 404, not a
    403: the API never confirms that an id exists.
    """
    booking = (
        Booking.objects.filter(pk=booking_id, customer=request.user, is_blocker=False)
        .select_related("business", "service", "resource")
        .first()
    )
    if booking is None:
        return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(BookingSerializer(booking).data)


@api_view(["POST"])
@throttle_classes([PaymentVerifyThrottle])
def verify_payment(request):
    """Ask Paystack for the authoritative state of a deposit, then apply it.

    This is the recovery path for a delayed or dropped webhook: the
    customer's return page calls it, and the reconciliation task uses the
    same underlying service. It is safe to poll — once the payment is no
    longer INITIATED the provider is not called again, so a payment can
    never be applied twice.
    """
    reference = request.data.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        return Response(
            {"detail": "A payment reference is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    reference = reference.strip()

    payment = (
        Payment.objects.filter(
            reference=reference,
            kind=Payment.Kind.DEPOSIT,
            booking__customer=request.user,
        )
        .select_related("booking")
        .first()
    )
    if payment is None:
        return Response({"detail": "Payment not found."}, status=status.HTTP_404_NOT_FOUND)

    if payment.status == Payment.Status.INITIATED:
        try:
            payment = payments.verify_and_apply(payment.pk)
        except payments.PaymentNotFound:
            return Response(
                {"detail": "Payment not found."}, status=status.HTTP_404_NOT_FOUND
            )
        except paystack.PaystackError as exc:
            return Response(
                {"detail": f"Paystack could not verify the payment: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except payments.AmountMismatch as exc:
            # Real money discrepancy: the booking stays untouched and ops
            # has to look at it. The customer sees a conflict, not a 500.
            logger.error("Amount mismatch verifying %s: %s", reference, exc)
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

    booking = Booking.objects.select_related(
        "business", "service", "resource"
    ).get(pk=payment.booking_id)
    return Response(
        {
            "payment": {
                "reference": payment.reference,
                "status": payment.status,
                "amount_kobo": payment.amount_kobo,
                "paid_at": payment.paid_at,
            },
            "booking": BookingSerializer(booking).data,
        }
    )
