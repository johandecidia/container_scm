# Webhook intake views — receive and store carrier webhook payloads.
# Views return quickly (202 Accepted) and delegate processing to Celery tasks.
import json
import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.teams.models import Team

from .carriers.registry import UnknownCarrierError, get_carrier_definition
from .models import Integration, IntegrationWebhookEvent

logger = logging.getLogger(__name__)


def _safe_headers(request: HttpRequest) -> dict:
    """Extract a safe subset of HTTP headers — strip any auth headers."""
    skip_prefixes = ("HTTP_AUTHORIZATION", "HTTP_X_API_KEY", "HTTP_X_TOKEN")
    return {k: v for k, v in request.META.items() if k.startswith("HTTP_") and not k.startswith(skip_prefixes)}


@csrf_exempt
@require_POST
def carrier_webhook(request: HttpRequest, team_slug: str, provider_code: str) -> HttpResponse:
    """Receive an inbound webhook from a carrier.

    URL: /a/<team_slug>/integrations/webhooks/<provider_code>/

    1. Validates that provider_code is registered.
    2. Resolves the team from team_slug.
    3. Stores raw payload as IntegrationWebhookEvent.
    4. Returns 202 Accepted immediately.
    5. Enqueues a Celery task to process the event asynchronously.
    """
    # Validate the provider_code against the registry.
    try:
        get_carrier_definition(provider_code)
    except UnknownCarrierError:
        logger.warning("Received webhook for unknown provider: %s", provider_code)
        return JsonResponse({"error": f"Unknown provider: {provider_code}"}, status=404)

    team = get_object_or_404(Team, slug=team_slug)

    # Parse JSON body (fall back to empty dict on malformed input).
    try:
        payload = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError, ValueError:
        payload = {"_raw": request.body.decode(errors="replace")}

    headers = _safe_headers(request)
    event_type = request.META.get("HTTP_X_EVENT_TYPE", "") or payload.get("eventType", "")
    external_event_id = request.META.get("HTTP_X_EVENT_ID", "") or payload.get("eventID", "")

    # Optionally link to an existing Integration for this team + provider.
    integration: Integration | None = Integration.objects.filter(team=team, provider_code=provider_code).first()

    webhook_event = IntegrationWebhookEvent.objects.create(
        team=team,
        integration=integration,
        provider_code=provider_code,
        event_type=event_type,
        external_event_id=external_event_id,
        headers=headers,
        payload=payload,
        status=IntegrationWebhookEvent.Status.RECEIVED,
    )

    logger.info(
        "Webhook received: provider=%s team=%s event_id=%s pk=%s",
        provider_code,
        team.slug,
        external_event_id,
        webhook_event.pk,
    )

    # Enqueue async processing (task defined in tasks.py).
    try:
        from .tasks import process_integration_webhook_event

        process_integration_webhook_event.delay(webhook_event.pk)
    except Exception:  # noqa: BLE001
        # If Celery is unavailable, the event is still stored for later processing.
        logger.exception("Could not enqueue webhook processing for event %s", webhook_event.pk)

    return JsonResponse({"received": True, "id": webhook_event.pk}, status=202)
