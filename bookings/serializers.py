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

from . import money
from .models import Booking, Business, ClassSession, Resource, Service


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


class PublicResourceSerializer(serializers.ModelSerializer):
    """Who/what the customer can pick ("any barber", or Ada specifically)."""

    class Meta:
        model = Resource
        fields = ("id", "name", "kind")
        read_only_fields = fields


class PublicServiceSerializer(serializers.ModelSerializer):
    """A menu item on the public storefront.

    price_kobo and deposit_kobo are PER HEAD for group services
    (capacity > 1), exactly as money.py computes them; is_group tells the
    frontend which convention to display. The serializer never invents
    totals — checkout always re-prices server-side from the same helpers.
    """

    deposit_kobo = serializers.SerializerMethodField()
    is_group = serializers.BooleanField(read_only=True)
    resources = PublicResourceSerializer(many=True, read_only=True)

    class Meta:
        model = Service
        fields = (
            "id",
            "name",
            "duration_minutes",
            "buffer_minutes",
            "price_kobo",
            "deposit_kobo",
            "capacity",
            "is_group",
            "requires_address",
            "resources",
        )
        read_only_fields = fields

    def get_deposit_kobo(self, service):
        # One head is the unit the storefront quotes. The real deposit is
        # recomputed from party_size when the hold is created.
        return money.deposit_kobo(service, 1)


class PublicBusinessSerializer(serializers.ModelSerializer):
    """The customer-facing storefront: branding, policy and open services."""

    services = PublicServiceSerializer(many=True, read_only=True)

    class Meta:
        model = Business
        fields = (
            "id",
            "name",
            "slug",
            "category",
            "address",
            "timezone",
            "description",
            "phone",
            "logo_url",
            "brand_color",
            "deposit_percent",
            "free_cancellation_hours",
            "hold_minutes",
            "services",
        )
        read_only_fields = fields


class BookingSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source="service.name", read_only=True)
    resource_name = serializers.CharField(source="resource.name", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)
    business_slug = serializers.CharField(source="business.slug", read_only=True)

    class Meta:
        model = Booking
        fields = (
            "id",
            "business_name",
            "business_slug",
            "service_name",
            "resource_name",
            "session",
            "start_at",
            "end_at",
            "party_size",
            "status",
            # The snapshot the customer agreed to — not next week's policy.
            "free_cancellation_hours",
            "price_kobo",
            "deposit_kobo",
            "expires_at",
            "address",
            "notes",
            "created_at",
        )
        read_only_fields = fields
