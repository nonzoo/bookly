"""DRF serializers for the customer API.

Validation here is SHAPE only — is the field present, is it the right
type. Business rules (working hours, capacity, deposits, cancellation
windows) deliberately stay in the service layer: the webhook, admin and
API paths all go through the same functions, so a rule can never exist in
two versions that drift apart.

The read serializer exposes the booking's snapshot fields (price_kobo /
deposit_kobo), never a live lookup of the service's current price: history
must not change when the owner edits the menu.
"""
from rest_framework import serializers

from .models import Booking, ClassSession, Resource, Service


class AvailabilityQuerySerializer(serializers.Serializer):
    date = serializers.DateField()
    # Optional: "only show me Ada". Interpreted in the BUSINESS timezone
    # by availability.py, which owns that rule.
    resource = serializers.IntegerField(required=False, min_value=1)


class HoldCreateSerializer(serializers.Serializer):
    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.all())
    # Required for one-to-one services; ignored for group ones (the
    # session carries the time). holds.create_hold enforces which.
    start_at = serializers.DateTimeField(required=False)
    resource = serializers.PrimaryKeyRelatedField(
        queryset=Resource.objects.all(), required=False, allow_null=True
    )
    session = serializers.PrimaryKeyRelatedField(
        queryset=ClassSession.objects.all(), required=False, allow_null=True
    )
    party_size = serializers.IntegerField(required=False, default=1, min_value=1)
    address = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)


class BookingSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source="service.name", read_only=True)
    resource_name = serializers.CharField(source="resource.name", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)

    class Meta:
        model = Booking
        fields = (
            "id",
            "business_name",
            "service_name",
            "resource_name",
            "session",
            "start_at",
            "end_at",
            "party_size",
            "status",
            "price_kobo",
            "deposit_kobo",
            "expires_at",
            "address",
            "notes",
            "created_at",
        )
        read_only_fields = fields
