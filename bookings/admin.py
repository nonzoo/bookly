"""Django admin for every model. Doubles as the owner-facing ops surface
for the dashboard stretch item, and as the fastest debugging tool while
the service layer is being built."""
from django.contrib import admin

from . import lifecycle
from .models import (
    Booking,
    Business,
    ClassSession,
    LedgerEntry,
    Payment,
    Resource,
    Service,
    TimeOff,
    WebhookEvent,
    WorkingHours,
)


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "category", "owner", "deposit_percent", "hold_minutes")
    search_fields = ("name", "slug")


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "kind", "max_party_size", "is_active")
    list_filter = ("kind", "business")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "duration_minutes", "price_kobo", "capacity", "is_active")
    list_filter = ("business",)


@admin.register(WorkingHours)
class WorkingHoursAdmin(admin.ModelAdmin):
    list_display = ("resource", "weekday", "start_time", "end_time")


@admin.register(TimeOff)
class TimeOffAdmin(admin.ModelAdmin):
    list_display = ("resource", "period", "reason")


@admin.register(ClassSession)
class ClassSessionAdmin(admin.ModelAdmin):
    list_display = ("service", "resource", "start_at", "capacity", "seats_taken", "is_cancelled")
    list_filter = ("service", "is_cancelled")
    # Both fields are owned by the service layer: seats move only through
    # reserve/release, and cancellation must also release seats, cancel
    # customer bookings and refund deposits.
    readonly_fields = ("seats_taken", "is_cancelled")
    actions = ["cancel_sessions"]

    @admin.action(description="Cancel sessions (release seats, refund deposits)")
    def cancel_sessions(self, request, queryset):
        cancelled = 0
        for session in queryset:
            # lifecycle.cancel_session re-locks the row, so a session
            # cancelled concurrently is simply skipped here.
            if lifecycle.cancel_session(session):
                cancelled += 1
        self.message_user(
            request,
            f"Cancelled {cancelled} session(s); already-cancelled sessions were skipped.",
        )


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "business", "resource", "service", "start_at", "status", "is_blocker")
    list_filter = ("status", "business")
    search_fields = ("customer__email",)
    readonly_fields = ("time_range", "is_exclusive", "is_blocker")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "booking", "kind", "amount_kobo", "status", "paid_at")
    search_fields = ("reference",)


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "provider", "event_key", "signature_valid", "processed_at")
    search_fields = ("event_key",)


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("business", "entry_type", "amount_kobo", "booking", "payment", "created_at")
    list_filter = ("entry_type",)
