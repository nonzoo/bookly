"""Per-view throttle classes.

DRF's ScopedRateThrottle discovers its rate by reading a `throttle_scope`
attribute off the view object. That is an APIView-subclass idiom; on a
function-based view the attribute never reaches the wrapped view instance.
A tiny SimpleRateThrottle subclass with a fixed `scope` expresses the same
thing declaratively, and the rates still live in settings
(DEFAULT_THROTTLE_RATES) as the single place to tune them.
"""
from rest_framework.throttling import SimpleRateThrottle


class BookingCreateThrottle(SimpleRateThrottle):
    """Per-user cap on creating holds — reading bookings is free.

    The check lives here rather than in the view so the rule cannot drift
    from the URL: any future method added to the endpoint is unthrottled
    unless it is POST.
    """

    scope = "booking_create"

    def allow_request(self, request, view):
        if request.method != "POST":
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        # Per user when signed in (the endpoint requires auth anyway);
        # the IP fallback keeps the bucket defined for edge cases.
        if request.user and request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class WebhookThrottle(SimpleRateThrottle):
    """Loose per-IP cap on the unauthenticated webhook route."""

    scope = "webhook"

    def get_cache_key(self, request, view):
        # Always the IP: there is no user on this route, and "None" must
        # never be the key (that would share one global bucket per worker).
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class PaymentVerifyThrottle(SimpleRateThrottle):
    """Per-user cap on payment verification.

    The return page may poll, and every poll while the payment is still
    INITIATED is a Paystack HTTP call. This keeps that budget bounded
    without blocking the (free) booking read.
    """

    scope = "payment_verify"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
