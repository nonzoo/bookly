"""API routes for the bookings app.

The webhook is unauthenticated (signature-verified); everything else is
behind the global JWT + IsAuthenticated defaults.
"""
from django.urls import path

from . import api, views

urlpatterns = [
    path("webhooks/paystack/", views.paystack_webhook, name="paystack-webhook"),
    path(
        "services/<int:service_id>/availability/",
        api.service_availability,
        name="service-availability",
    ),
    path("bookings/", api.bookings, name="booking-list-create"),
    path("bookings/<uuid:booking_id>/cancel/", api.cancel_booking, name="booking-cancel"),
]
