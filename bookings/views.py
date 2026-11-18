"""HTTP surface for the bookings app.

Only the webhook is exposed so far. It is deliberately a plain Django
view: the endpoint is unauthenticated (Paystack cannot hold a JWT) and
DRF's default IsAuthenticated permission must not apply. The service
layer does the real work; the view only translates HTTP to/from it.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import webhooks


@csrf_exempt
@require_POST
def paystack_webhook(request):
    try:
        result = webhooks.receive(request.body, request.headers)
    except webhooks.InvalidWebhook as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=400)

    return JsonResponse(
        {"status": "ok" if result.http_status == 200 else "error"},
        status=result.http_status,
    )
