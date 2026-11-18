"""API routes for the bookings app."""
from django.urls import path

from . import views

urlpatterns = [
    path("webhooks/paystack/", views.paystack_webhook, name="paystack-webhook"),
]
