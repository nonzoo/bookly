"""
Multi-industry booking + deposits domain models (Django 5.2, PostgreSQL).

Layering rules for this app:

- models.py owns domain state AND the hard guarantees (the exclusion
  constraint here, the seat CHECK on ClassSession). Service modules
  (holds, webhooks, lifecycle) lean on these guarantees but never
  replace or bypass them.
- All money is integer kobo. No floats, ever.
- Pricing rules live in bookings/money.py — the single source of truth.

Migration note: BtreeGistExtension() is the first operation of the first
migration. The exclusion constraint pairs (resource EQUAL) with
(time_range OVERLAPS); plain GiST has no equality operator class for the
scalar resource column — btree_gist supplies it.

Two booking modes:
1. One-to-one (Service.capacity == 1): barber, dentist, table, pitch, room.
   Bookings are exclusive: the DB exclusion constraint blocks any overlap
   on the same resource.
2. Group sessions (Service.capacity > 1): yoga, classes. Seats live on
   ClassSession (CHECK-guarded); customer bookings are NON-exclusive, so
   many can share the session's time. The resource itself is blocked by
   an "is_blocker" Booking row created with the session, so classes are
   covered by the same exclusion constraint as everything else.
"""
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from psycopg.types.range import TimestamptzRange  # psycopg 3: tstzrange values


class Business(models.Model):
    """The tenant. Owns policy; everything else scopes through it."""

    class Category(models.TextChoices):
        BEAUTY = "beauty", "Beauty & Grooming"
        HEALTH = "health", "Health & Wellness"
        FITNESS = "fitness", "Fitness"
        AUTO = "auto", "Auto"
        HOME = "home", "Home Services"
        DINING = "dining", "Dining & Leisure"
        VENUES = "venues", "Sports & Venues"
        PROFESSIONAL = "professional", "Professional"
        PETS = "pets", "Pets"
        OTHER = "other", "Other"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="businesses")
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    address = models.CharField(max_length=255, blank=True)
    timezone = models.CharField(max_length=64, default="Africa/Lagos")

    # Policy anchors — availability, holds and refunds all read from here.
    deposit_percent = models.PositiveSmallIntegerField(default=20)           # 0-100
    free_cancellation_hours = models.PositiveSmallIntegerField(default=24)   # refund window
    hold_minutes = models.PositiveSmallIntegerField(default=15)              # unpaid slot hold

    # Reserved for a future direct-settlement mode. v1 keeps deposit money
    # platform-side: the platform holds it and pays out via Transfers.
    paystack_subaccount_code = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["category"])]
        constraints = [
            # PositiveSmallIntegerField already enforces >= 0 at the DB level.
            models.CheckConstraint(
                condition=Q(deposit_percent__lte=100),
                name="business_deposit_percent_max_100",
            ),
        ]

    def __str__(self):
        return self.name


class Resource(models.Model):
    """Anything bookable: a person, a table, a pitch, a room, a bay.

    One model instead of Staff, so tables/pitches/rooms get the same
    exclusion-constraint guarantee people get, for free.
    """

    class Kind(models.TextChoices):
        PERSON = "person"      # barber, doctor, tutor, mechanic
        TABLE = "table"
        PITCH = "pitch"        # pitches and courts
        ROOM = "room"          # studios, meeting rooms, event halls
        BAY = "bay"            # car wash / workshop bays

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="resources")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PERSON)
    name = models.CharField(max_length=120)
    max_party_size = models.PositiveSmallIntegerField(default=1)  # table seats, room capacity
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.business})"


class Service(models.Model):
    """A bookable offering. capacity splits the two booking modes."""

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="services")
    name = models.CharField(max_length=120)
    duration_minutes = models.PositiveSmallIntegerField()
    buffer_minutes = models.PositiveSmallIntegerField(default=0)   # cleanup / travel after the job
    # Per booking if capacity == 1; PER HEAD if capacity > 1 (money.py).
    price_kobo = models.PositiveIntegerField()
    deposit_kobo_override = models.PositiveIntegerField(null=True, blank=True)  # same per-head convention
    capacity = models.PositiveSmallIntegerField(default=1)         # 1 = one-to-one, >1 = group session
    requires_address = models.BooleanField(default=False)          # home services
    is_active = models.BooleanField(default=True)
    resources = models.ManyToManyField(Resource, related_name="services")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(capacity__gte=1), name="service_capacity_min_1"),
            models.CheckConstraint(condition=Q(duration_minutes__gte=1), name="service_duration_min_1"),
        ]

    @property
    def is_group(self):
        return self.capacity > 1

    def __str__(self):
        return f"{self.name} ({self.business})"


class WorkingHours(models.Model):
    """When a resource can take bookings, in the business's timezone.

    Multiple rows per weekday are legal (split shifts); the availability
    service unions them.
    """

    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="working_hours")
    weekday = models.PositiveSmallIntegerField()  # 0=Mon ... 6=Sun
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(end_time__gt=F("start_time")), name="wh_end_after_start"),
            models.CheckConstraint(condition=Q(weekday__lte=6), name="wh_weekday_0_to_6"),
        ]


class TimeOff(models.Model):
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="time_off")
    period = DateTimeRangeField()
    reason = models.CharField(max_length=200, blank=True)


class ClassSession(models.Model):
    """A scheduled instance of a group service (e.g. Yoga, Tue 7am).

    seats_taken is the only mutable capacity state in the system. It may
    only change through reserve_seats()/release_seats(), so every caller
    goes through the same atomic primitive.
    """

    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="sessions")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="sessions")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    capacity = models.PositiveSmallIntegerField()
    seats_taken = models.PositiveSmallIntegerField(default=0)
    is_cancelled = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["service", "start_at"])]
        constraints = [
            # The backstop: no code path can oversell a class.
            models.CheckConstraint(condition=Q(seats_taken__lte=F("capacity")), name="session_not_oversold"),
            models.CheckConstraint(condition=Q(end_at__gt=F("start_at")), name="session_end_after_start"),
        ]

    @classmethod
    def reserve_seats(cls, session_id, seats):
        """Atomically grab `seats` seats. Returns True if reserved.

        One conditional UPDATE: Postgres takes the row lock for the UPDATE
        itself and re-evaluates the WHERE clause under that lock, so
        concurrent reservers serialize and overselling is impossible
        without any explicit select_for_update.
        """
        updated = cls.objects.filter(
            pk=session_id, is_cancelled=False, seats_taken__lte=F("capacity") - seats
        ).update(seats_taken=F("seats_taken") + seats)
        return updated == 1

    @classmethod
    def release_seats(cls, session_id, seats):
        """Give `seats` seats back (booking cancelled or hold expired).

        The gte filter turns a double release into a no-op instead of a
        negative seat count.
        """
        cls.objects.filter(pk=session_id, seats_taken__gte=seats).update(seats_taken=F("seats_taken") - seats)


class Booking(models.Model):
    """Every occupied interval is a row in this table: customer bookings
    AND the blocker rows that hold a resource for a class session. One
    table, one exclusion constraint, one invariant."""

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment"
        CONFIRMED = "confirmed"
        COMPLETED = "completed"
        CANCELLED_BY_CUSTOMER = "cancelled_by_customer"
        CANCELLED_BY_BUSINESS = "cancelled_by_business"
        NO_SHOW = "no_show"
        EXPIRED = "expired"

    ACTIVE_STATUSES = ["pending_payment", "confirmed"]  # block the slot / hold seats

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.PROTECT, related_name="bookings")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="bookings")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="bookings")
    session = models.ForeignKey(ClassSession, null=True, blank=True, on_delete=models.PROTECT, related_name="bookings")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bookings")

    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    # [start_at, end_at + service buffer). Half-open bounds keep
    # back-to-back bookings legal while still reserving the cleanup/travel
    # window after each job.
    time_range = DateTimeRangeField(editable=False)
    is_exclusive = models.BooleanField(editable=False)  # blocker OR capacity-1 service

    # Blocker rows hold a resource for a ClassSession. An explicit flag,
    # not derived, because save() computes is_exclusive from service
    # capacity — which would be False for a group service and the blocker
    # would silently block nothing.
    is_blocker = models.BooleanField(default=False, editable=False)

    party_size = models.PositiveSmallIntegerField(default=1)
    address = models.CharField(max_length=255, blank=True)   # home services
    notes = models.CharField(max_length=500, blank=True)

    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING_PAYMENT)
    price_kobo = models.PositiveIntegerField()      # snapshot at booking time
    deposit_kobo = models.PositiveIntegerField()    # snapshot at booking time
    expires_at = models.DateTimeField(null=True, blank=True)  # unpaid hold deadline

    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["resource", "start_at"]),
            models.Index(fields=["status", "expires_at"]),   # expiry sweep
            models.Index(fields=["customer", "start_at"]),   # "my bookings"
        ]
        constraints = [
            models.CheckConstraint(condition=Q(end_at__gt=F("start_at")), name="booking_end_after_start"),
            models.CheckConstraint(condition=Q(party_size__gte=1), name="booking_party_min_1"),
            # The core guarantee: one resource cannot host two overlapping
            # exclusive bookings. The condition keeps the GiST index small
            # and lets cancelled/expired rows release the slot.
            ExclusionConstraint(
                name="no_overlapping_active_bookings",
                expressions=[
                    ("resource", RangeOperators.EQUAL),
                    ("time_range", RangeOperators.OVERLAPS),
                ],
                condition=Q(status__in=["pending_payment", "confirmed"], is_exclusive=True),
            ),
        ]

    def clean(self):
        # Read straight off self.service: is_exclusive is assigned in
        # save(), which runs after clean(), so it is unreliable here.
        if self.service.requires_address and not self.address:
            raise ValidationError("This service needs an address.")
        if self.service.capacity == 1 and self.party_size > self.resource.max_party_size:
            raise ValidationError("Party size exceeds what this resource can hold.")
        if self.service.is_group and not self.session_id:
            raise ValidationError("Group services must be booked against a session.")

    def save(self, *args, **kwargs):
        """Recomputes derived fields: time_range, is_exclusive, expires_at.

        Do not call save(update_fields=[...]) with a subset of fields —
        derived fields would be computed but not persisted. Service code
        always does full saves.
        """
        self.is_exclusive = self.is_blocker or self.service.capacity == 1
        buffer = timedelta(minutes=self.service.buffer_minutes)
        self.time_range = TimestamptzRange(self.start_at, self.end_at + buffer)  # default bounds [)
        if self.status == self.Status.PENDING_PAYMENT:
            if not self.expires_at:  # set once, at hold creation
                self.expires_at = timezone.now() + timedelta(minutes=self.business.hold_minutes)
        elif self.expires_at:
            # Later states no longer need the deadline; clearing keeps the
            # (status, expires_at) index tight for the expiry sweep.
            self.expires_at = None
        super().save(*args, **kwargs)  # note: bulk_create bypasses all of this

    def __str__(self):
        return f"{self.service} · {self.start_at:%Y-%m-%d %H:%M} · {self.status}"


class Payment(models.Model):
    """Money with Paystack. kind=DEPOSIT rows are collections; kind=REFUND
    rows exist to track Paystack's own refund lifecycle
    (refund.created / refund.processed / refund.failed), which has its
    own references and webhooks."""

    class Kind(models.TextChoices):
        DEPOSIT = "deposit"
        REFUND = "refund"

    class Status(models.TextChoices):
        INITIATED = "initiated"
        SUCCESS = "success"
        FAILED = "failed"
        REFUNDED = "refunded"

    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    reference = models.CharField(max_length=100, unique=True)  # our reference sent to Paystack
    amount_kobo = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INITIATED)
    paystack_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)


class WebhookEvent(models.Model):
    """Every inbound webhook is stored first, then processed once."""

    provider = models.CharField(max_length=20, default="paystack")
    event_key = models.CharField(max_length=200)   # e.g. f"{event}:{reference}"
    event_type = models.CharField(max_length=60)
    payload = models.JSONField()
    signature_valid = models.BooleanField()
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        constraints = [
            # Duplicate delivery hits this constraint -> ignore, return 200.
            models.UniqueConstraint(fields=["provider", "event_key"], name="uniq_webhook_event"),
        ]


class LedgerEntry(models.Model):
    """Append-only. Business balance = sum(amount_kobo) for that business.

    Money model (locked design): the PLATFORM holds deposits.
      deposit_received +D   credit on payment success (held, earmarked)
      refund            -D  when refund.processed arrives
      deposit_forfeited  0  audit marker: the deposit became irrevocably
                            the business's (late cancel / no-show)
      payout            -X  Paystack Transfer to the business
    """

    class Type(models.TextChoices):
        DEPOSIT_RECEIVED = "deposit_received"
        REFUND = "refund"
        DEPOSIT_FORFEITED = "deposit_forfeited"
        PAYOUT = "payout"

    business = models.ForeignKey(Business, on_delete=models.PROTECT, related_name="ledger")
    booking = models.ForeignKey(Booking, null=True, on_delete=models.PROTECT)
    payment = models.ForeignKey(Payment, null=True, on_delete=models.PROTECT)
    entry_type = models.CharField(max_length=20, choices=Type.choices)
    amount_kobo = models.IntegerField()  # signed: credits positive, debits negative
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # One ledger entry of each type per payment, so a retried
            # webhook cannot double-credit. Final backstop behind the
            # webhook dedup constraint.
            models.UniqueConstraint(
                fields=["payment", "entry_type"],
                condition=Q(payment__isnull=False),
                name="uniq_ledger_entry_per_payment_type",
            ),
        ]
