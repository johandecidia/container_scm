# Tracking sync workflow.
# Orchestrates fetching, storing, and normalising tracking data for a subscription.
import logging

from django.utils import timezone

from .models import TrackingSubscription
from .selectors import get_due_tracking_subscriptions
from .services import (
    create_sync_run,
    finish_sync_run_failed,
    finish_sync_run_success,
    store_raw_payload,
    update_subscription_sync_state,
    upsert_tracking_event,
)
from .statuses import TrackingEventType

logger = logging.getLogger(__name__)

# Default interval between syncs (in minutes).  Carriers will override this.
DEFAULT_SYNC_INTERVAL_MINUTES = 60


def _get_next_sync_at():
    """Return the default next sync datetime."""
    return timezone.now() + timezone.timedelta(minutes=DEFAULT_SYNC_INTERVAL_MINUTES)


def _get_carrier_client_and_parser(provider_code: str):
    """Look up the carrier client and parser from the registry.

    Returns (client_instance, parser_instance) or raises if the carrier is unknown.
    Raises UnknownCarrierError for unknown provider codes.
    """
    from apps.scm.integrations.carriers.registry import get_carrier_definition

    definition = get_carrier_definition(provider_code)
    client = definition.client_class()
    parser = definition.parser_class()
    return client, parser


def sync_tracking_subscription(subscription: TrackingSubscription) -> bool:
    """Run a single sync cycle for a tracking subscription.

    1. Creates a TrackingSyncRun log entry.
    2. Calls the appropriate carrier client via the carrier registry.
    3. Stores raw payload.
    4. Parses and upserts normalised events.
    5. Updates subscription sync state.
    6. Returns True on success, False on failure.
    """
    sync_run = create_sync_run(
        team=subscription.team,
        subscription=subscription,
        provider=subscription.provider,
    )

    try:
        # Resolve carrier client and parser from the registry.
        try:
            client, parser = _get_carrier_client_and_parser(subscription.provider.code)
        except Exception as exc:  # noqa: BLE001
            # Registry miss or carrier not yet implemented — log and continue with empty payload.
            logger.warning(
                "Could not resolve carrier adapter for provider %s (subscription %s): %s",
                subscription.provider.code,
                subscription.pk,
                exc,
            )
            client = None
            parser = None

        # Fetch raw payload from carrier (placeholder if client not yet implemented).
        raw_payload: dict = {}
        if client is not None:
            try:
                raw_payload = client.fetch_tracking(
                    container_number=subscription.tracking_reference
                    if subscription.reference_type == TrackingSubscription.ReferenceType.CONTAINER_NUMBER
                    else None,
                    bill_of_lading_number=subscription.tracking_reference
                    if subscription.reference_type == TrackingSubscription.ReferenceType.BILL_OF_LADING
                    else None,
                    booking_number=subscription.tracking_reference
                    if subscription.reference_type == TrackingSubscription.ReferenceType.BOOKING_NUMBER
                    else None,
                )
            except NotImplementedError:
                logger.debug(
                    "Carrier %s fetch_tracking not implemented yet — using empty payload.",
                    subscription.provider.code,
                )
                raw_payload = {}

        # Store raw payload.
        raw_payload_record = store_raw_payload(
            team=subscription.team,
            provider=subscription.provider,
            payload=raw_payload,
            subscription=subscription,
            parsed_successfully=True,
        )

        # Parse and upsert normalised events.
        parsed_events: list[dict] = []
        if parser is not None and raw_payload:
            try:
                parsed_events = parser.parse_tracking_events(raw_payload)
            except NotImplementedError:
                logger.debug("Carrier %s parser not implemented yet.", subscription.provider.code)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Parser error for provider %s (subscription %s): %s",
                    subscription.provider.code,
                    subscription.pk,
                    exc,
                )

        events_created = 0
        events_updated = 0
        for event_data in parsed_events:
            _event, created = upsert_tracking_event(
                team=subscription.team,
                provider=subscription.provider,
                subscription=subscription,
                shipment=subscription.shipment,
                container=subscription.container,
                event_type=event_data.get("event_type", TrackingEventType.UNKNOWN),
                event_datetime=event_data.get("event_datetime"),
                source_event_id=event_data.get("source_event_id", ""),
                event_code=event_data.get("event_code", ""),
                status=event_data.get("status", ""),
                description=event_data.get("description", ""),
                location_name=event_data.get("location_name", ""),
                location_unlocode=event_data.get("location_unlocode", ""),
                event_timezone=event_data.get("event_timezone", ""),
                confidence=event_data.get("confidence", 100),
                raw_data=event_data.get("raw_data", {}),
            )
            if created:
                events_created += 1
            else:
                events_updated += 1

        finish_sync_run_success(
            sync_run,
            events_created=events_created,
            events_updated=events_updated,
            raw_payloads_created=1 if raw_payload_record else 0,
        )
        update_subscription_sync_state(subscription, success=True, next_sync_at=_get_next_sync_at())
        logger.info(
            "Sync success for subscription %s: %d created, %d updated.",
            subscription.pk,
            events_created,
            events_updated,
        )
        return True

    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        logger.exception("Sync failed for subscription %s: %s", subscription.pk, error_message)
        finish_sync_run_failed(sync_run, error_message=error_message)
        update_subscription_sync_state(subscription, success=False, error_message=error_message)
        return False


def sync_due_tracking_subscriptions() -> dict:
    """Run sync for all subscriptions that are due.

    Returns a summary dict with counts of successes and failures.
    """
    due = list(get_due_tracking_subscriptions())
    successes = 0
    failures = 0
    skipped = 0

    for subscription in due:
        if subscription.status == TrackingSubscription.Status.SYNCING:
            # Already in progress, skip to avoid concurrent sync.
            skipped += 1
            continue
        result = sync_tracking_subscription(subscription)
        if result:
            successes += 1
        else:
            failures += 1

    logger.info(
        "Sync run complete: %d success, %d failed, %d skipped (total %d).",
        successes,
        failures,
        skipped,
        len(due),
    )
    return {"successes": successes, "failures": failures, "skipped": skipped, "total": len(due)}
