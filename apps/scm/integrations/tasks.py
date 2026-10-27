import logging
from dataclasses import asdict
from typing import Any

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


def run_business_central_resource_sync_task(task, integration_id: int, resource_type: str, trigger_type: str) -> None:
    """Shared body of the Business Central sync tasks (``task`` is the bound Celery task).

    Permanent problems (config, unimplemented/disabled resource, or a sync already
    running) are logged and skipped; transient Business Central errors are retried
    with backoff. A *scheduled* run is re-checked when it executes — integration
    opt-in, resource implemented and enabled — so a task queued before any of
    those was withdrawn does nothing.
    """
    from .business_systems.business_central.exceptions import (
        BusinessCentralConfigurationError,
        BusinessCentralError,
        BusinessCentralSyncInProgressError,
    )
    from .business_systems.business_central.resources import get_sync_function, is_resource_schedulable
    from .models import Integration

    try:
        integration = Integration.objects.get(pk=integration_id, is_active=True)
    except Integration.DoesNotExist:
        logger.info("BC %s sync task: integration %s not found/inactive — skipping.", resource_type, integration_id)
        return

    if trigger_type == "scheduled" and not is_resource_schedulable(integration, resource_type):
        logger.info(
            "BC %s sync task: integration=%s scheduled sync not enabled — skipping.", resource_type, integration_id
        )
        return

    sync = get_sync_function(resource_type)
    if sync is None:
        logger.info("BC %s sync task: no sync implementation — skipping.", resource_type)
        return

    try:
        run = sync(integration, trigger_type=trigger_type)
        logger.info(
            "BC %s sync task: integration=%s %s (created=%d updated=%d unchanged=%d failed=%d)",
            resource_type,
            integration_id,
            run.status,
            run.records_created,
            run.records_updated,
            run.records_unchanged,
            run.records_failed,
        )
        _queue_receipt_processing_after(integration, resource_type)
        _queue_receipt_serial_linking_after(integration, resource_type)
    except (BusinessCentralConfigurationError, BusinessCentralSyncInProgressError) as exc:
        logger.info("BC %s sync task: integration=%s skipped: %s", resource_type, integration_id, exc)
    except BusinessCentralError as exc:
        logger.warning("BC %s sync task: integration=%s transient failure: %s", resource_type, integration_id, exc)
        raise task.retry(exc=exc) from exc


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def sync_business_central_resource_task(
    self, integration_id: int, resource_type: str, trigger_type: str = "scheduled"
) -> None:
    """Run a Business Central sync of ``resource_type`` for a single integration."""
    run_business_central_resource_sync_task(self, integration_id, resource_type, trigger_type)


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def sync_business_central_purchase_orders_task(self, integration_id: int, trigger_type: str = "scheduled") -> None:
    """Run a Business Central purchase-order sync for a single integration.

    Kept as its own task (name unchanged) for purchase orders; it shares its body
    with :func:`sync_business_central_resource_task`.
    """
    from .models import IntegrationSyncRun

    run_business_central_resource_sync_task(
        self, integration_id, IntegrationSyncRun.ResourceType.PURCHASE_ORDERS, trigger_type
    )


def queue_business_central_resource_sync(integration_id: int, resource_type: str, trigger_type: str) -> None:
    """Queue one resource sync; purchase orders keep their dedicated task."""
    from .models import IntegrationSyncRun

    if resource_type == IntegrationSyncRun.ResourceType.PURCHASE_ORDERS:
        sync_business_central_purchase_orders_task.delay(integration_id, trigger_type)
    else:
        sync_business_central_resource_task.delay(integration_id, resource_type, trigger_type)


@shared_task
def match_business_central_item_ledger_entries_task(integration_id: int) -> dict:
    """Link the integration's item ledger entries to existing containers (operation A).

    Never creates or changes a container and never calls Business Central. A pass
    already running for the integration is skipped, not queued twice.
    """
    from .business_systems.business_central.container_links import match_item_ledger_entries
    from .business_systems.business_central.exceptions import BusinessCentralSyncInProgressError
    from .models import Integration

    try:
        integration = Integration.objects.get(pk=integration_id, provider_code="business_central")
    except Integration.DoesNotExist:
        logger.info("BC item ledger matching: integration %s not found — skipping.", integration_id)
        return {"skipped": True}
    try:
        result = match_item_ledger_entries(integration)
    except BusinessCentralSyncInProgressError:
        logger.info("BC item ledger matching: integration %s already running — skipping.", integration_id)
        return {"skipped": True}
    return {"checked": result.checked, "changed": result.changed, "by_status": dict(result.by_status)}


def _queue_receipt_processing_after(integration, resource_type: str) -> None:
    """New receipts or ledger entries may complete a receipt: queue a processing pass.

    Only when the purchase-receipt resource is enabled. The sync itself never
    receives; processing is its own task with its own lock.
    """
    from .business_systems.business_central.resources import get_resource_settings
    from .models import IntegrationSyncRun

    ResourceType = IntegrationSyncRun.ResourceType
    if resource_type not in (ResourceType.PURCHASE_RECEIPTS, ResourceType.ITEM_LEDGER_ENTRIES):
        return
    if get_resource_settings(integration, ResourceType.PURCHASE_RECEIPTS).runnable:
        process_business_central_purchase_receipts_task.delay(integration.pk)


def _queue_receipt_serial_linking_after(integration, resource_type: str) -> None:
    """New order lines may give already-posted receipt serials their line: link them.

    Receipt processing links on its own pass; a purchase-order sync queues a linking
    pass because the lines a receipt points at can arrive after the receipt did.
    """
    from .models import IntegrationSyncRun

    if resource_type == IntegrationSyncRun.ResourceType.PURCHASE_ORDERS:
        link_business_central_receipt_serials_task.delay(integration.pk)


@shared_task
def link_business_central_receipt_serials_task(integration_id: int) -> dict:
    """Link posted-receipt serials to their purchase order lines (``ContainerAcquisition``).

    Only for containers that already exist; never creates a container, a movement or
    a receive, and never calls Business Central.
    """
    from .business_systems.business_central.receipt_serial_lines import link_posted_receipt_serials
    from .models import Integration

    try:
        integration = Integration.objects.get(pk=integration_id, provider_code="business_central", is_active=True)
    except Integration.DoesNotExist:
        logger.info("BC receipt serial linking: integration %s not found/inactive — skipping.", integration_id)
        return {"skipped": True}
    return asdict(link_posted_receipt_serials(integration))


@shared_task
def process_business_central_purchase_receipts_task(integration_id: int) -> dict:
    """Resolve the integration's posted purchase receipts and receive what qualifies.

    Receives only through locations configured for automatic receive, and only
    receipts posted after activation. A pass already running is skipped.
    """
    from .business_systems.business_central.exceptions import BusinessCentralSyncInProgressError
    from .business_systems.business_central.receipt_receive import process_purchase_receipts
    from .business_systems.business_central.receipt_serial_lines import link_posted_receipt_serials
    from .models import Integration

    try:
        integration = Integration.objects.get(pk=integration_id, provider_code="business_central", is_active=True)
    except Integration.DoesNotExist:
        logger.info("BC receipt processing: integration %s not found/inactive — skipping.", integration_id)
        return {"skipped": True}
    try:
        result = process_purchase_receipts(integration)
    except BusinessCentralSyncInProgressError:
        logger.info("BC receipt processing: integration %s already running — skipping.", integration_id)
        return {"skipped": True}
    # Commercial evidence only: links existing containers to their order lines, after
    # (and independent of) the receive decisions above.
    links = link_posted_receipt_serials(integration)
    return {
        "checked": result.checked,
        "received": len(result.received),
        "by_reason": dict(result.by_reason),
        "serial_links": asdict(links),
    }


@shared_task
def test_business_central_connection_task(integration_id: int) -> dict:
    """Test a Business Central integration's connection off the request cycle.

    Updates the integration's health fields (last_tested_at + success/error) and
    returns a sanitised {"success", "message"}. Never exposes credentials.
    """
    from .business_systems.business_central.configuration import test_business_central_connection
    from .models import Integration

    try:
        integration = Integration.objects.get(pk=integration_id)
    except Integration.DoesNotExist:
        logger.warning("test_business_central_connection_task: integration %s not found.", integration_id)
        return {"success": False, "message": "Integration not found"}

    return test_business_central_connection(integration)


@shared_task
def sync_enabled_business_central_integrations_task() -> dict:
    """Dispatcher: queue a sync for every BC integration resource that is due.

    Runs on a fixed Celery Beat interval; the due check (interval, failure backoff,
    in-progress, enabled/active) decides what is actually queued. One task is
    enqueued per integration and resource, keeping teams and resources isolated.

    Only integrations with ``scheduled_sync_enabled: true`` in their config are
    considered (opt-in; default off), and only resources that are implemented and
    enabled (see ``business_central.resources``). Set
    ``SCM_BUSINESS_CENTRAL_DISPATCH_ENABLED=False`` to pause scheduled Business
    Central syncing globally without removing the schedule or the implementation.
    Manual and per-integration syncs still work.
    """
    from django.conf import settings

    from .services import get_due_business_central_syncs

    if not getattr(settings, "SCM_BUSINESS_CENTRAL_DISPATCH_ENABLED", True):
        logger.info("BC dispatcher: disabled by configuration — nothing queued.")
        return {"queued": 0, "disabled": True}

    due = get_due_business_central_syncs()
    for integration, resource_type in due:
        queue_business_central_resource_sync(integration.id, resource_type, "scheduled")
    logger.info("BC dispatcher: queued %d resource sync(s)", len(due))
    return {"queued": len(due), "disabled": False}


@shared_task
def dispatch_shipment_container_discovery_task() -> dict:
    """Dispatcher: queue container discovery for each team with open shipments.

    One task per team keeps teams isolated. Only teams that actually have a
    candidate shipment are queued, so an idle team costs nothing.
    """
    from django.db.models import Q

    from apps.scm.shipments.models import Shipment

    team_ids = list(
        Shipment.objects.exclude(status__in=[Shipment.Status.DELIVERED, Shipment.Status.CANCELLED])
        .filter(shipment_containers__isnull=True)
        .filter(Q(carrier_booking_reference__gt="") | Q(bill_of_lading_number__gt="") | Q(reference__gt=""))
        .exclude(carrier="")
        # order_by() clears Shipment's default ordering: the ordering column would
        # end up in the SELECT list and defeat DISTINCT, queueing a team once per
        # candidate shipment.
        .order_by()
        .values_list("team_id", flat=True)
        .distinct()
    )
    for team_id in team_ids:
        discover_containers_for_open_shipments_task.delay(team_id)

    logger.info("Shipment discovery dispatcher: queued %d team(s).", len(team_ids))
    return {"queued": len(team_ids)}


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

    totals: dict[str, Any] = {
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
