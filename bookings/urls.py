"""API routes for the bookings app.

The webhook is unauthenticated (signature-verified), and the catalog plus
availability are public so a customer can browse before signing in.
Everything that touches a booking or its money requires the owning
account.
"""
from django.urls import path

from . import api, views

urlpatterns = [
    path("webhooks/paystack/", views.paystack_webhook, name="paystack-webhook"),
    path("businesses/<slug:slug>/", api.business_detail, name="business-detail"),
    path(
        "services/<int:service_id>/availability/",
        api.service_availability,
        name="service-availability",
    ),
    path("bookings/", api.bookings, name="booking-list-create"),
    path("bookings/<uuid:booking_id>/", api.booking_detail, name="booking-detail"),
    path("bookings/<uuid:booking_id>/cancel/", api.cancel_booking, name="booking-cancel"),
    path("payments/verify/", api.verify_payment, name="payment-verify"),
]
