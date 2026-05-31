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


def sync_tracking_subscription(subscription: TrackingSubscription) -> bool:
    """Run a single sync cycle for a tracking subscription.

    1. Creates a TrackingSyncRun log entry.
    2. Calls the appropriate carrier client (placeholder in this version).
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
        # --- Carrier call (placeholder) -----------------------------------
        # In production this will call the appropriate carrier client, e.g.:
        #   client = MaerskTrackingClient()
        #   raw = client.fetch_tracking(subscription.tracking_reference, subscription.reference_type)
        # For now we produce an empty payload so the workflow runs without real APIs.
        raw_payload: dict = {}
        parsed_events: list[dict] = []

        # Store raw payload
        raw_payload_record = store_raw_payload(
            team=subscription.team,
            provider=subscription.provider,
            payload=raw_payload,
            subscription=subscription,
            parsed_successfully=True,
        )

        # Upsert normalised events
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
