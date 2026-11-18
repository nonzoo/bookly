"""Paystack webhook endpoint — function-based DRF view.

DRF is used here for two reasons:

1. Throttling. The route is unauthenticated by definition, so JWT and
   IsAuthenticated are switched off explicitly and an IP-scoped rate limit
   is applied instead (the same settings-driven machinery as the rest of
   the API).
2. One HTTP translation layer. The view turns `InvalidWebhook` and the
   service result into status codes; everything else is webhooks.receive.

The signature is computed over the EXACT bytes Paystack signed, so the
view reads `request.body` on the underlying HttpRequest. DRF's parsed
`.data` would consume the stream — the body is never touched as data.
"""
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.response import Response

from . import webhooks
from .throttles import WebhookThrottle


@api_view(["POST"])
@authentication_classes([])   # Paystack has no JWT
@permission_classes([])       # ... and no Django user
@throttle_classes([WebhookThrottle])  # 300/min per IP, from settings
def paystack_webhook(request):
    try:
        result = webhooks.receive(request.body, request.headers)
    except webhooks.InvalidWebhook as exc:
        return Response(
            {"status": "error", "message": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(
        {"status": "ok" if result.http_status == 200 else "error"},
        status=result.http_status,
    )
