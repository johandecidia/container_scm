# Tracking services — all business logic and write operations.
# Views must not contain business logic; call these functions instead.
import hashlib
import json

from django.utils import timezone

from apps.teams.models import Team

from .ingestion import build_event_fingerprint, upsert_event
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


def finish_sync_run(
    sync_run: TrackingSyncRun,
    *,
    status: str,
    error_type: str = TrackingSyncRun.ErrorType.NONE,
    error_message: str = "",
    events_created: int = 0,
    events_updated: int = 0,
    raw_payloads_created: int = 0,
    metadata: dict | None = None,
) -> TrackingSyncRun:
    """Close a sync run with an explicit status, error classification and counters."""
    sync_run.status = status
    sync_run.error_type = error_type
    sync_run.error_message = error_message
    sync_run.finished_at = timezone.now()
    sync_run.events_created = events_created
    sync_run.events_updated = events_updated
    sync_run.raw_payloads_created = raw_payloads_created
    if metadata:
        sync_run.metadata = {**(sync_run.metadata or {}), **metadata}
    sync_run.save(
        update_fields=[
            "status",
            "error_type",
            "error_message",
            "finished_at",
            "events_created",
            "events_updated",
            "raw_payloads_created",
            "metadata",
            "updated_at",
        ]
    )
    return sync_run


def finish_sync_run_success(
    sync_run: TrackingSyncRun,
    events_created: int = 0,
    events_updated: int = 0,
    raw_payloads_created: int = 0,
) -> TrackingSyncRun:
    """Mark a sync run as successful and update counters."""
    return finish_sync_run(
        sync_run,
        status=TrackingSyncRun.Status.SUCCESS,
        events_created=events_created,
        events_updated=events_updated,
        raw_payloads_created=raw_payloads_created,
    )


def finish_sync_run_failed(
    sync_run: TrackingSyncRun,
    error_message: str,
    error_type: str = TrackingSyncRun.ErrorType.UNEXPECTED,
) -> TrackingSyncRun:
    """Mark a sync run as failed."""
    return finish_sync_run(
        sync_run,
        status=TrackingSyncRun.Status.FAILED,
        error_type=error_type,
        error_message=error_message,
    )


def finish_sync_run_skipped(
    sync_run: TrackingSyncRun,
    error_type: str,
    error_message: str = "",
) -> TrackingSyncRun:
    """Mark a sync run as skipped — nothing was attempted.

    Never use this for a carrier that answered with no data; that is a success.
    """
    return finish_sync_run(
        sync_run,
        status=TrackingSyncRun.Status.SKIPPED,
        error_type=error_type,
        error_message=error_message,
    )


def mark_raw_payload_parsed(
    payload: TrackingRawPayload,
    *,
    success: bool,
    error_message: str = "",
) -> TrackingRawPayload:
    """Record whether a stored payload was actually parsed.

    Payloads are stored before parsing, so this is the only place that may claim a
    payload parsed successfully.
    """
    payload.parsed_successfully = success
    payload.error_message = error_message
    payload.save(update_fields=["parsed_successfully", "error_message", "updated_at"])
    return payload


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
    event_time_type: str = TrackingEvent.EventTimeType.UNKNOWN,
) -> tuple[TrackingEvent, bool]:
    """Create or update a tracking event from explicit field values.

    Used by ingestion sources that already work in internal terms (manual entry,
    webhooks, imports). The carrier pipeline uses
    :func:`apps.scm.tracking.ingestion.persist_normalised_event` instead.

    Deduplication uses the same fingerprint as the carrier pipeline: derived from
    the carrier event ID when there is one, otherwise from the fields that identify
    the event. Returns (event, created).
    """
    reference = ""
    if container is not None:
        reference = container.container_id
    elif subscription is not None:
        reference = subscription.tracking_reference

    fingerprint = build_event_fingerprint(
        team_id=team.pk,
        provider_code=provider.code,
        source_event_id=source_event_id,
        reference=reference,
        carrier_event_type=event_type,
        event_code=event_code,
        event_time_type=event_time_type,
        event_datetime=event_datetime,
        location_unlocode=location_unlocode,
        location_name=location_name,
    )
    defaults = {
        "event_type": event_type,
        "event_time_type": event_time_type,
        "event_code": event_code,
        "status": status,
        "description": description,
        "location_name": location_name,
        "location_unlocode": location_unlocode,
        "event_datetime": event_datetime,
        "event_timezone": event_timezone,
        "received_at": timezone.now(),
        "confidence": confidence,
        "raw_data": raw_data or {},
        "source_event_id": source_event_id,
        "shipment": shipment,
        "container": container,
        "subscription": subscription,
    }
    return upsert_event(team=team, provider=provider, fingerprint=fingerprint, defaults=defaults)


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
    *,
    skipped: bool = False,
    tracking_status: str | None = None,
    last_event_at=None,
) -> TrackingSubscription:
    """Update sync state on a subscription after a sync attempt.

    On success: resets the failure counter and returns the subscription to ACTIVE.
    On failure: increments consecutive_failures, records the error, sets FAILED.
    On skipped: nothing was attempted, so the failure counter is left alone — a
    carrier we never called must not accumulate failures — but the next poll is
    still rescheduled so the subscription does not spin.
    """
    now = timezone.now()
    fields = ["status", "updated_at"]

    if tracking_status is not None:
        subscription.tracking_status = tracking_status
        fields.append("tracking_status")
    if last_event_at is not None:
        subscription.last_event_at = last_event_at
        fields.append("last_event_at")
    if next_sync_at is not None:
        subscription.next_sync_at = next_sync_at
        fields.append("next_sync_at")

    if skipped:
        subscription.status = TrackingSubscription.Status.ACTIVE
        subscription.last_error_message = error_message
        fields.append("last_error_message")
    elif success:
        subscription.status = TrackingSubscription.Status.ACTIVE
        subscription.last_synced_at = now
        subscription.consecutive_failures = 0
        subscription.last_error_message = ""
        subscription.last_error_at = None
        fields += ["last_synced_at", "consecutive_failures", "last_error_message", "last_error_at"]
    else:
        subscription.status = TrackingSubscription.Status.FAILED
        subscription.last_error_message = error_message
        subscription.last_error_at = now
        subscription.consecutive_failures += 1
        fields += ["last_error_message", "last_error_at", "consecutive_failures"]

    subscription.save(update_fields=list(dict.fromkeys(fields)))
    return subscription
