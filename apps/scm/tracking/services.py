# Tracking services — all business logic and write operations.
# Views must not contain business logic; call these functions instead.
import hashlib
import json

from django.db import transaction
from django.utils import timezone

from apps.teams.models import Team

from .models import TrackingEvent, TrackingProvider, TrackingRawPayload, TrackingSubscription, TrackingSyncRun


def create_tracking_subscription(
    team: Team,
    provider: TrackingProvider,
    tracking_reference: str,
    reference_type: str = TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
    shipment=None,
    container=None,
) -> TrackingSubscription:
    """Create a new tracking subscription for a team."""
    return TrackingSubscription.objects.create(
        team=team,
        provider=provider,
        tracking_reference=tracking_reference,
        reference_type=reference_type,
        shipment=shipment,
        container=container,
        status=TrackingSubscription.Status.ACTIVE,
    )


def pause_tracking_subscription(subscription: TrackingSubscription) -> TrackingSubscription:
    """Pause an active tracking subscription."""
    subscription.status = TrackingSubscription.Status.PAUSED
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


def resume_tracking_subscription(subscription: TrackingSubscription) -> TrackingSubscription:
    """Resume a paused tracking subscription."""
    subscription.status = TrackingSubscription.Status.ACTIVE
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


def complete_tracking_subscription(subscription: TrackingSubscription) -> TrackingSubscription:
    """Mark a tracking subscription as completed."""
    subscription.status = TrackingSubscription.Status.COMPLETED
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


def cancel_tracking_subscription(subscription: TrackingSubscription) -> TrackingSubscription:
    """Cancel a tracking subscription."""
    subscription.status = TrackingSubscription.Status.CANCELLED
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


def store_raw_payload(
    team: Team,
    provider: TrackingProvider,
    payload: dict,
    payload_type: str = TrackingRawPayload.PayloadType.API_RESPONSE,
    subscription: TrackingSubscription | None = None,
    parsed_successfully: bool = False,
    error_message: str = "",
) -> TrackingRawPayload:
    """Store a raw payload from an external tracking source."""
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return TrackingRawPayload.objects.create(
        team=team,
        provider=provider,
        subscription=subscription,
        payload_type=payload_type,
        payload_json=payload,
        payload_hash=payload_hash,
        received_at=timezone.now(),
        parsed_successfully=parsed_successfully,
        error_message=error_message,
    )


def create_sync_run(
    team: Team,
    subscription: TrackingSubscription,
    provider: TrackingProvider,
) -> TrackingSyncRun:
    """Create a new sync run record and mark the subscription as syncing."""
    sync_run = TrackingSyncRun.objects.create(
        team=team,
        subscription=subscription,
        provider=provider,
        status=TrackingSyncRun.Status.STARTED,
        started_at=timezone.now(),
    )
    subscription.status = TrackingSubscription.Status.SYNCING
    subscription.save(update_fields=["status", "updated_at"])
    return sync_run


def finish_sync_run_success(
    sync_run: TrackingSyncRun,
    events_created: int = 0,
    events_updated: int = 0,
    raw_payloads_created: int = 0,
) -> TrackingSyncRun:
    """Mark a sync run as successful and update counters."""
    sync_run.status = TrackingSyncRun.Status.SUCCESS
    sync_run.finished_at = timezone.now()
    sync_run.events_created = events_created
    sync_run.events_updated = events_updated
    sync_run.raw_payloads_created = raw_payloads_created
    sync_run.save(
        update_fields=[
            "status",
            "finished_at",
            "events_created",
            "events_updated",
            "raw_payloads_created",
            "updated_at",
        ]
    )
    return sync_run


def finish_sync_run_failed(
    sync_run: TrackingSyncRun,
    error_message: str,
) -> TrackingSyncRun:
    """Mark a sync run as failed."""
    sync_run.status = TrackingSyncRun.Status.FAILED
    sync_run.finished_at = timezone.now()
    sync_run.error_message = error_message
    sync_run.save(update_fields=["status", "finished_at", "error_message", "updated_at"])
    return sync_run


@transaction.atomic
def upsert_tracking_event(
    team: Team,
    provider: TrackingProvider,
    event_type: str,
    event_datetime,
    subscription: TrackingSubscription | None = None,
    shipment=None,
    container=None,
    source_event_id: str = "",
    event_code: str = "",
    status: str = "",
    description: str = "",
    location_name: str = "",
    location_unlocode: str = "",
    event_timezone: str = "",
    confidence: int = 100,
    raw_data: dict | None = None,
) -> tuple[TrackingEvent, bool]:
    """Create or update a tracking event, avoiding duplicates.

    Deduplication strategy:
    1. If source_event_id is set, use (team, provider, source_event_id) as unique key.
    2. Otherwise fall back to (team, provider, subscription, event_type, event_datetime).

    Returns (event, created) tuple.
    """
    defaults = {
        "event_code": event_code,
        "status": status,
        "description": description,
        "location_name": location_name,
        "location_unlocode": location_unlocode,
        "event_datetime": event_datetime,
        "event_timezone": event_timezone,
        "confidence": confidence,
        "raw_data": raw_data or {},
        "shipment": shipment,
        "container": container,
        "subscription": subscription,
        "event_type": event_type,
    }

    if source_event_id:
        event, created = TrackingEvent.objects.update_or_create(
            team=team,
            provider=provider,
            source_event_id=source_event_id,
            defaults=defaults,
        )
    else:
        event, created = TrackingEvent.objects.update_or_create(
            team=team,
            provider=provider,
            subscription=subscription,
            event_type=event_type,
            event_datetime=event_datetime,
            defaults=defaults,
        )
    return event, created


def deduplicate_tracking_event(
    team: Team,
    provider: TrackingProvider,
    source_event_id: str = "",
    event_type: str = "",
    event_datetime=None,
    subscription: TrackingSubscription | None = None,
) -> bool:
    """Return True if a matching event already exists (would be a duplicate)."""
    if source_event_id:
        return TrackingEvent.objects.filter(team=team, provider=provider, source_event_id=source_event_id).exists()
    return TrackingEvent.objects.filter(
        team=team,
        provider=provider,
        subscription=subscription,
        event_type=event_type,
        event_datetime=event_datetime,
    ).exists()


def update_subscription_sync_state(
    subscription: TrackingSubscription,
    success: bool,
    error_message: str = "",
    next_sync_at=None,
) -> TrackingSubscription:
    """Update sync state fields on a subscription after a sync attempt.

    On success: resets failure counters, sets status back to ACTIVE.
    On failure: increments consecutive_failures, records error details, sets status to FAILED.
    """
    now = timezone.now()
    if success:
        subscription.status = TrackingSubscription.Status.ACTIVE
        subscription.last_synced_at = now
        subscription.next_sync_at = next_sync_at
        subscription.consecutive_failures = 0
        subscription.last_error_message = ""
        subscription.last_error_at = None
        subscription.save(
            update_fields=[
                "status",
                "last_synced_at",
                "next_sync_at",
                "consecutive_failures",
                "last_error_message",
                "last_error_at",
                "updated_at",
            ]
        )
    else:
        subscription.status = TrackingSubscription.Status.FAILED
        subscription.last_error_message = error_message
        subscription.last_error_at = now
        subscription.consecutive_failures += 1
        subscription.save(
            update_fields=[
                "status",
                "last_error_message",
                "last_error_at",
                "consecutive_failures",
                "updated_at",
            ]
        )
    return subscription
