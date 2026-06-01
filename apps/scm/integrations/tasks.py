import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_integration_webhook_event(self, webhook_event_id: int) -> None:
    """Process an inbound webhook event asynchronously.

    Marks the event as processing, delegates to the carrier/system handler,
    then marks it as processed or failed.
    """
    from django.utils import timezone

    from .models import IntegrationWebhookEvent

    try:
        event = IntegrationWebhookEvent.objects.get(pk=webhook_event_id)
    except IntegrationWebhookEvent.DoesNotExist:
        logger.warning("WebhookEvent %s not found — skipping.", webhook_event_id)
        return

    event.status = IntegrationWebhookEvent.Status.PROCESSING
    event.save(update_fields=["status", "updated_at"])

    try:
        # TODO: dispatch to carrier/business-system-specific processor
        # e.g. carrier_webhook_processor.process(event)
        logger.info(
            "WebhookEvent %s (provider=%s) processing — no processor registered yet.",
            event.pk,
            event.provider_code,
        )
        event.status = IntegrationWebhookEvent.Status.PROCESSED
        event.processed_at = timezone.now()
        event.save(update_fields=["status", "processed_at", "updated_at"])

    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        logger.exception("WebhookEvent %s processing failed: %s", event.pk, error_message)
        event.status = IntegrationWebhookEvent.Status.FAILED
        event.error_message = error_message
        event.save(update_fields=["status", "error_message", "updated_at"])
        raise self.retry(exc=exc) from exc


@shared_task
def test_integration_connection_task(integration_id: int) -> dict:
    """Async wrapper around test_integration_connection.

    Useful for running connection tests without blocking the request cycle.
    """
    from .models import Integration
    from .services import test_integration_connection

    try:
        integration = Integration.objects.get(pk=integration_id)
    except Integration.DoesNotExist:
        logger.warning("Integration %s not found — skipping connection test.", integration_id)
        return {"success": False, "message": "Integration not found"}

    return test_integration_connection(integration)


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def retry_failed_integration_request(self, integration_id: int) -> None:
    """Retry the last failed API request for an integration.

    TODO: implement once request retry logic is defined per carrier.
    """
    from .models import Integration

    try:
        integration = Integration.objects.get(pk=integration_id)
    except Integration.DoesNotExist:
        logger.warning("Integration %s not found — skipping retry.", integration_id)
        return

    logger.info(
        "retry_failed_integration_request: integration %s (provider=%s) — not yet implemented.",
        integration.pk,
        integration.provider_code,
    )
    # TODO: implement per-carrier retry logic
