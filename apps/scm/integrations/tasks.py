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


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def sync_business_central_purchase_orders_task(self, integration_id: int, trigger_type: str = "scheduled") -> None:
    """Run a Business Central purchase-order sync for a single integration.

    Permanent problems (config, or a sync already running) are logged and skipped;
    transient Business Central errors are retried with backoff.
    """
    from .business_systems.business_central.exceptions import (
        BusinessCentralConfigurationError,
        BusinessCentralError,
        BusinessCentralSyncInProgressError,
    )
    from .business_systems.business_central.sync import sync_purchase_orders_from_business_central
    from .models import Integration

    try:
        integration = Integration.objects.get(pk=integration_id, is_active=True)
    except Integration.DoesNotExist:
        logger.info(
            "sync_business_central_purchase_orders_task: integration %s not found/inactive — skipping.", integration_id
        )
        return

    try:
        run = sync_purchase_orders_from_business_central(integration, trigger_type=trigger_type)
        logger.info(
            "BC PO sync task: integration=%s %s (created=%d updated=%d unchanged=%d failed=%d)",
            integration_id,
            run.status,
            run.records_created,
            run.records_updated,
            run.records_unchanged,
            run.records_failed,
        )
    except (BusinessCentralConfigurationError, BusinessCentralSyncInProgressError) as exc:
        logger.info("BC PO sync task: integration=%s skipped: %s", integration_id, exc)
    except BusinessCentralError as exc:
        logger.warning("BC PO sync task: integration=%s transient failure: %s", integration_id, exc)
        raise self.retry(exc=exc) from exc


@shared_task
def test_business_central_connection_task(integration_id: int) -> dict:
    """Test a Business Central integration's connection off the request cycle.

    Updates the integration's health fields (last_tested_at + success/error) and
    returns a sanitised {"success", "message"}. Never exposes credentials.
    """
    from django.utils import timezone

    from .business_systems.business_central.client import BusinessCentralClient
    from .business_systems.business_central.exceptions import BusinessCentralError
    from .models import Integration
    from .services import mark_integration_error, mark_integration_success

    try:
        integration = Integration.objects.get(pk=integration_id)
    except Integration.DoesNotExist:
        logger.warning("test_business_central_connection_task: integration %s not found.", integration_id)
        return {"success": False, "message": "Integration not found"}

    integration.last_tested_at = timezone.now()
    integration.save(update_fields=["last_tested_at", "updated_at"])
    try:
        result = BusinessCentralClient(integration=integration).test_connection()
        mark_integration_success(integration)
        return result
    except BusinessCentralError as exc:
        message = f"{type(exc).__name__}: {exc}"
        mark_integration_error(integration, message)
        return {"success": False, "message": message}


@shared_task
def sync_enabled_business_central_integrations_task() -> dict:
    """Dispatcher: queue a PO sync for every BC integration that is due.

    Runs on a fixed Celery Beat interval; the due check (interval, failure backoff,
    in-progress, enabled/active) decides which integrations are actually queued.
    One task is enqueued per integration, keeping teams isolated.
    """
    from .services import get_due_business_central_integrations

    due = get_due_business_central_integrations()
    for integration in due:
        sync_business_central_purchase_orders_task.delay(integration.id, "scheduled")
    logger.info("BC dispatcher: queued %d integration(s)", len(due))
    return {"queued": len(due)}


@shared_task
def discover_containers_for_open_shipments_task(team_id: int) -> dict:
    """Discover containers for all open shipments that lack containers.

    Targets shipments that:
      - belong to the given team
      - are not in DELIVERED or CANCELLED status
      - have no containers linked
      - have at least one discovery reference (carrier_booking_reference,
        bill_of_lading_number, or reference)

    Returns a summary dict with total counts across all shipments processed.
    """
    from django.db.models import Q

    from apps.scm.shipments.models import Shipment

    from .carriers.discovery_service import discover_containers_for_shipment

    shipments = (
        Shipment.objects.filter(team_id=team_id)
        .exclude(status__in=[Shipment.Status.DELIVERED, Shipment.Status.CANCELLED])
        # Containers hang off the ShipmentContainer through model (related name
        # shipment_containers); there is no direct `containers` relation.
        .filter(shipment_containers__isnull=True)
        .filter(
            Q(carrier_booking_reference__gt="") | Q(bill_of_lading_number__gt="") | Q(reference__gt=""),
        )
        .distinct()
    )

    totals = {
        "shipments_processed": 0,
        "shipments_skipped": 0,
        "discovered_count": 0,
        "containers_created": 0,
        "containers_linked": 0,
        "subscriptions_created": 0,
        "errors": [],
    }

    for shipment in shipments:
        summary = discover_containers_for_shipment(shipment)
        if summary.get("skipped"):
            totals["shipments_skipped"] += 1
            continue

        totals["shipments_processed"] += 1
        totals["discovered_count"] += summary["discovered_count"]
        totals["containers_created"] += summary["containers_created"]
        totals["containers_linked"] += summary["containers_linked"]
        totals["subscriptions_created"] += summary["subscriptions_created"]
        totals["errors"].extend(summary["errors"])

    logger.info(
        "discover_containers_for_open_shipments_task team=%s: processed=%s discovered=%s created=%s linked=%s",
        team_id,
        totals["shipments_processed"],
        totals["discovered_count"],
        totals["containers_created"],
        totals["containers_linked"],
    )
    return totals
