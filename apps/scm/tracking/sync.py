"""Tracking sync workflow.

Runs one sync cycle for a tracking subscription: fetch from the carrier, store the
raw response, normalise and persist events, then decide when to come back.

The outcome of a cycle is deliberately three-valued, because collapsing these
would hide real problems:

SUCCESS
    The carrier answered. Zero events is a valid success — it means the carrier
    has no data for this reference yet (tracking_status NO_DATA), not that
    something broke.

SKIPPED
    Nothing was attempted: the adapter is a stub, the integration is not
    configured, another run holds the lock, or the provider is not one this poller
    drives at all. A skipped run must never look like "synced, nothing found" —
    and only the skips that mean "this reference cannot be tracked as things
    stand" are allowed to change ``tracking_status``.

FAILED
    The call was attempted and failed. ``error_type`` records which kind, so an
    expired credential is distinguishable from a carrier outage, and transient
    failures back off instead of hammering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.utils import timezone

from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierError,
    CarrierInvalidResponseError,
    CarrierNoDataError,
    CarrierNotImplementedError,
    CarrierRateLimitError,
    CarrierServerError,
    CarrierTimeoutError,
    CarrierUnsupportedReferenceError,
)
from apps.scm.integrations.locks import LockNotAcquiredError, resource_lock

from .ingestion import persist_normalised_events
from .models import TrackingRawPayload, TrackingSubscription, TrackingSyncRun
from .polling import next_sync_at
from .selectors import get_due_tracking_subscriptions
from .services import (
    complete_tracking_subscription,
    create_sync_run,
    finish_sync_run,
    mark_raw_payload_parsed,
    store_raw_payload,
    update_subscription_sync_state,
)
from .sources import get_non_carrier_source

logger = logging.getLogger(__name__)

_LOCK_PREFIX = "tracking_sync_lock"
# The lock is held across HTTP calls, so allow for a slow carrier; the underlying
# advisory lock has no TTL and is released if the worker dies.
_LOCK_TTL_SECONDS = 1800

_ErrorType = TrackingSyncRun.ErrorType

# Which reference keyword each subscription reference type maps to on the client.
_REFERENCE_KWARG: dict[str, str] = {
    TrackingSubscription.ReferenceType.CONTAINER_NUMBER: "container_number",
    TrackingSubscription.ReferenceType.BILL_OF_LADING: "bill_of_lading_number",
    TrackingSubscription.ReferenceType.BOOKING_NUMBER: "booking_number",
    TrackingSubscription.ReferenceType.SHIPMENT_REFERENCE: "shipment_reference",
}

# Carrier errors that mean "nothing was attempted" rather than "the attempt failed".
_SKIP_ERRORS: dict[type[CarrierError], str] = {
    CarrierNotImplementedError: _ErrorType.NOT_IMPLEMENTED,
    CarrierConfigurationError: _ErrorType.NOT_CONFIGURED,
}

# Carrier errors that mean the attempt failed, and how to label them.
_FAILURE_ERRORS: list[tuple[type[CarrierError], str]] = [
    (CarrierAuthenticationError, _ErrorType.AUTHENTICATION),
    (CarrierRateLimitError, _ErrorType.RATE_LIMIT),
    (CarrierTimeoutError, _ErrorType.TIMEOUT),
    (CarrierServerError, _ErrorType.SERVER_ERROR),
    (CarrierInvalidResponseError, _ErrorType.INVALID_RESPONSE),
    (CarrierUnsupportedReferenceError, _ErrorType.UNSUPPORTED_REFERENCE),
]


@dataclass
class SyncOutcome:
    """What one sync cycle achieved."""

    status: str = TrackingSyncRun.Status.SUCCESS
    error_type: str = _ErrorType.NONE
    error_message: str = ""
    events_created: int = 0
    events_updated: int = 0
    events_failed: int = 0
    raw_payloads_created: int = 0
    retry_after_seconds: int | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status in (TrackingSyncRun.Status.SUCCESS, TrackingSyncRun.Status.PARTIAL_SUCCESS)

    @property
    def skipped(self) -> bool:
        return self.status == TrackingSyncRun.Status.SKIPPED

    @property
    def events_seen(self) -> int:
        return self.events_created + self.events_updated


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def sync_tracking_subscription(subscription: TrackingSubscription) -> TrackingSyncRun | None:
    """Run one sync cycle for a subscription, under a lock.

    Returns the TrackingSyncRun, or None when another run already holds the lock —
    in that case no run is recorded, because the run that holds the lock is doing
    (and logging) the work.
    """
    lock_name = f"subscription:{subscription.pk}"
    try:
        with resource_lock(lock_name, ttl=_LOCK_TTL_SECONDS, prefix=_LOCK_PREFIX):
            return _run_sync(subscription)
    except LockNotAcquiredError:
        logger.info("Sync for subscription %s skipped — already running.", subscription.pk)
        return None


def sync_due_tracking_subscriptions() -> dict:
    """Run a sync for every subscription that is due.

    Returns a summary dict. A failure in one subscription never stops the others.
    """
    due = list(get_due_tracking_subscriptions())
    successes = 0
    failures = 0
    skipped = 0

    for subscription in due:
        try:
            sync_run = sync_tracking_subscription(subscription)
        except Exception:  # noqa: BLE001 — one subscription must not stop the batch
            failures += 1
            logger.exception("Unhandled error syncing subscription %s.", subscription.pk)
            continue

        if sync_run is None or sync_run.status == TrackingSyncRun.Status.SKIPPED:
            skipped += 1
        elif sync_run.status == TrackingSyncRun.Status.FAILED:
            failures += 1
        else:
            successes += 1

    logger.info(
        "Tracking sync batch complete: %d success, %d failed, %d skipped (total %d).",
        successes,
        failures,
        skipped,
        len(due),
    )
    return {"successes": successes, "failures": failures, "skipped": skipped, "total": len(due)}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run_sync(subscription: TrackingSubscription) -> TrackingSyncRun:
    sync_run = create_sync_run(
        team=subscription.team,
        subscription=subscription,
        provider=subscription.provider,
    )
    try:
        outcome = _fetch_normalise_and_store(subscription)
    except Exception as exc:  # noqa: BLE001 — an unexpected bug must still close the run cleanly
        logger.exception("Unexpected error syncing subscription %s.", subscription.pk)
        outcome = SyncOutcome(
            status=TrackingSyncRun.Status.FAILED,
            error_type=_ErrorType.UNEXPECTED,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    apply_sync_outcome(subscription, sync_run, outcome)
    return sync_run


def _fetch_normalise_and_store(subscription: TrackingSubscription) -> SyncOutcome:
    """Do the carrier work for one subscription and report the outcome."""
    from apps.scm.integrations.carriers.factory import build_carrier_client, build_carrier_parser
    from apps.scm.integrations.carriers.registry import UnknownCarrierError

    provider_code = subscription.provider.code

    # 0. Is this poller even the right caller? A non-carrier provider is a working
    # provider whose data arrives another way, so stepping aside is the correct
    # outcome — not a fault, and specifically not NOT_CONFIGURED, which would mark
    # the subscription untrackable and stop the UI offering it at all.
    non_carrier = get_non_carrier_source(provider_code)
    if non_carrier is not None:
        return SyncOutcome(
            status=TrackingSyncRun.Status.SKIPPED,
            error_type=_ErrorType.NOT_CARRIER_POLLED,
            # Deliberately no error_message: it would be copied onto the subscription's
            # last_error_message and read as a fault. The explanation goes in metadata.
            metadata={"provider": non_carrier.name, "refresh_with": non_carrier.refresh_hint},
        )

    # 1. Resolve the adapter for this team.
    try:
        client = build_carrier_client(provider_code, team=subscription.team)
        parser = build_carrier_parser(provider_code)
    except UnknownCarrierError as exc:
        return SyncOutcome(
            status=TrackingSyncRun.Status.SKIPPED,
            error_type=_ErrorType.NOT_CONFIGURED,
            error_message=str(exc),
        )

    # 2. Work out which reference to ask by.
    reference_kwarg = _REFERENCE_KWARG.get(subscription.reference_type)
    if reference_kwarg is None:
        return SyncOutcome(
            status=TrackingSyncRun.Status.SKIPPED,
            error_type=_ErrorType.NOT_CONFIGURED,
            error_message=f"Reference type '{subscription.reference_type}' is not polled from carriers.",
        )
    if not subscription.tracking_reference.strip():
        return SyncOutcome(
            status=TrackingSyncRun.Status.FAILED,
            error_type=_ErrorType.UNSUPPORTED_REFERENCE,
            error_message="Subscription has no tracking reference.",
        )

    # 3. Fetch.
    try:
        raw_payload = client.fetch_tracking(**{reference_kwarg: subscription.tracking_reference})
    except CarrierNoDataError as exc:
        # A valid answer: the carrier does not know this reference yet.
        return SyncOutcome(
            status=TrackingSyncRun.Status.SUCCESS,
            metadata={"no_data": True, "carrier_message": str(exc)},
        )
    except CarrierError as exc:
        return _outcome_for_carrier_error(exc)

    # 4. Store the raw response before trusting it.
    raw_payload_record = store_raw_payload(
        team=subscription.team,
        provider=subscription.provider,
        payload=raw_payload if isinstance(raw_payload, dict) else {"payload": raw_payload},
        subscription=subscription,
        parsed_successfully=False,
    )

    # 5. Parse.
    try:
        normalised_events = parser.parse_tracking_events(raw_payload)
    except CarrierNotImplementedError as exc:
        return SyncOutcome(
            status=TrackingSyncRun.Status.SKIPPED,
            error_type=_ErrorType.NOT_IMPLEMENTED,
            error_message=str(exc),
            raw_payloads_created=1,
        )
    except Exception as exc:  # noqa: BLE001 — a parser bug must not lose the payload
        message = f"{type(exc).__name__}: {exc}"
        mark_raw_payload_parsed(raw_payload_record, success=False, error_message=message)
        logger.warning("Parser error for provider %s (subscription %s): %s", provider_code, subscription.pk, message)
        return SyncOutcome(
            status=TrackingSyncRun.Status.FAILED,
            error_type=_ErrorType.PARSE_ERROR,
            error_message=message,
            raw_payloads_created=1,
        )

    mark_raw_payload_parsed(raw_payload_record, success=True)

    # 6. Persist normalised events.
    return _persist_events(subscription, normalised_events, raw_payload_record)


def _persist_events(
    subscription: TrackingSubscription,
    events: list,
    raw_payload_record: TrackingRawPayload,
) -> SyncOutcome:
    """Write a parsed batch of events for a subscription and report what happened."""
    result = persist_normalised_events(
        team=subscription.team,
        provider=subscription.provider,
        events=events,
        subscription=subscription,
        shipment=subscription.shipment,
        container=subscription.container,
        raw_payload=raw_payload_record,
    )
    status = TrackingSyncRun.Status.PARTIAL_SUCCESS if result["failed"] else TrackingSyncRun.Status.SUCCESS
    return SyncOutcome(
        status=status,
        events_created=result["created"],
        events_updated=result["updated"],
        events_failed=result["failed"],
        raw_payloads_created=1,
        error_message=(f"{result['failed']} event(s) could not be stored." if result["failed"] else ""),
    )


def store_verified_carrier_result(
    subscription: TrackingSubscription,
    *,
    raw_payload: dict,
    events: list,
) -> SyncOutcome:
    """Persist a carrier response that was already fetched and parsed elsewhere.

    A manual refresh of an untracked container asks the carrier *before* there is a
    subscription to attach anything to — that is the point: a candidate carrier only
    becomes this container's tracking source once it has answered with data. By the
    time the subscription exists the response is already in hand, so this covers
    steps 4–6 of :func:`_fetch_normalise_and_store` for it, through the same writes.
    """
    raw_payload_record = store_raw_payload(
        team=subscription.team,
        provider=subscription.provider,
        payload=raw_payload if isinstance(raw_payload, dict) else {"payload": raw_payload},
        subscription=subscription,
        parsed_successfully=True,
    )
    return _persist_events(subscription, events, raw_payload_record)


def _outcome_for_carrier_error(exc: CarrierError) -> SyncOutcome:
    """Classify a typed carrier error into a sync outcome."""
    for error_class, error_type in _SKIP_ERRORS.items():
        if isinstance(exc, error_class):
            return SyncOutcome(
                status=TrackingSyncRun.Status.SKIPPED,
                error_type=error_type,
                error_message=str(exc),
            )

    for error_class, error_type in _FAILURE_ERRORS:
        if isinstance(exc, error_class):
            return SyncOutcome(
                status=TrackingSyncRun.Status.FAILED,
                error_type=error_type,
                error_message=str(exc),
                retry_after_seconds=getattr(exc, "retry_after", None),
            )

    return SyncOutcome(
        status=TrackingSyncRun.Status.FAILED,
        error_type=_ErrorType.UNEXPECTED,
        error_message=str(exc),
    )


def _tracking_status_for(subscription: TrackingSubscription, outcome: SyncOutcome) -> str:
    """Decide what the carrier is currently telling us about this reference."""
    statuses = TrackingSubscription.TrackingStatus
    if outcome.skipped:
        # Only a skip that means "this reference cannot be tracked as things stand"
        # changes the status. Every other skip — a lock held, a provider this poller
        # does not drive — leaves whatever the last real answer was standing.
        if outcome.error_type in (_ErrorType.NOT_IMPLEMENTED, _ErrorType.NOT_CONFIGURED):
            return statuses.NOT_CONFIGURED
        return subscription.tracking_status
    if not outcome.succeeded:
        return statuses.ERROR
    if outcome.events_seen or subscription.last_event_at is not None:
        return statuses.TRACKING
    return statuses.NO_DATA


def tracking_status_from_run(subscription: TrackingSubscription, run: TrackingSyncRun) -> str:
    """Return the tracking status a run that *already happened* implies.

    The same decision :func:`apply_sync_outcome` makes, reached by reading a stored
    ``TrackingSyncRun`` back into a :class:`SyncOutcome` instead of by performing a new
    sync. Exists so state repair can ask "what should this subscription say, given what
    is already on record" without inventing a sync result to ask it with — see
    :mod:`apps.scm.tracking.repair`. Nothing is written, and the run is not modified.
    """
    return _tracking_status_for(
        subscription,
        SyncOutcome(
            status=run.status,
            error_type=run.error_type,
            error_message=run.error_message,
            events_created=run.events_created,
            events_updated=run.events_updated,
            raw_payloads_created=run.raw_payloads_created,
        ),
    )


def apply_sync_outcome(
    subscription: TrackingSubscription,
    sync_run: TrackingSyncRun,
    outcome: SyncOutcome,
) -> None:
    """Close the sync run and move the subscription to its new state.

    Public because the manual refresh finishes a run it started itself, once a probe
    has proved the carrier has data — the state transition must be the same one a
    scheduled sync makes.
    """
    finish_sync_run(
        sync_run,
        status=outcome.status,
        error_type=outcome.error_type,
        error_message=outcome.error_message,
        events_created=outcome.events_created,
        events_updated=outcome.events_updated,
        raw_payloads_created=outcome.raw_payloads_created,
        metadata=outcome.metadata,
    )

    tracking_status = _tracking_status_for(subscription, outcome)
    # Set the state before computing the next poll, so the interval reflects it.
    subscription.tracking_status = tracking_status
    if outcome.events_seen:
        subscription.last_event_at = timezone.now()

    # Move the shipment's milestones first: the polling interval depends on whether
    # the box has arrived, and deriving that after scheduling would leave every
    # arrival being polled at the in-transit rate for one more cycle.
    if outcome.succeeded and outcome.events_seen:
        _apply_events_to_shipment(subscription)

    integration_config = _integration_config(subscription)
    scheduled_at = next_sync_at(
        subscription,
        integration_config=integration_config,
        retry_after_seconds=outcome.retry_after_seconds,
    )

    update_subscription_sync_state(
        subscription,
        success=outcome.succeeded,
        skipped=outcome.skipped,
        error_message=outcome.error_message,
        next_sync_at=scheduled_at,
        tracking_status=tracking_status,
        last_event_at=subscription.last_event_at,
    )

    if outcome.succeeded:
        _complete_if_terminal(subscription)

    logger.info(
        "Sync %s for subscription %s: %d created, %d updated (%s).",
        outcome.status,
        subscription.pk,
        outcome.events_created,
        outcome.events_updated,
        outcome.error_type or "no error",
    )


def _integration_config(subscription: TrackingSubscription) -> dict:
    """Return the team's carrier integration config, or {} when not configured."""
    from apps.scm.integrations.carriers.factory import get_carrier_integration

    integration = get_carrier_integration(subscription.team, subscription.provider.code)
    return (integration.config or {}) if integration else {}


def _apply_events_to_shipment(subscription: TrackingSubscription) -> None:
    """Let the new events move the shipment's milestones, status and ETA.

    Failing here must not fail the sync: the events are already stored, and the
    derivation is deterministic, so it can be re-run.
    """
    from apps.scm.shipments.transport_status import apply_tracking_to_shipment

    shipment = subscription.shipment
    if shipment is None:
        return
    try:
        apply_tracking_to_shipment(shipment, container=subscription.container)
    except Exception:  # noqa: BLE001 — stored events must survive a derivation bug
        logger.exception("Could not apply tracking to shipment %s.", shipment.pk)


def _complete_if_terminal(subscription: TrackingSubscription) -> None:
    """Stop watching once there is nothing left to learn.

    The shipment's status is the terminal signal wherever there is a shipment — it is
    the same judgement the rest of the system works from. Only a container tracked on
    its own falls back to its own delivery event, because otherwise nothing would ever
    stop polling it.
    """
    reason = _terminal_reason(subscription)
    if not reason:
        return

    complete_tracking_subscription(subscription)
    logger.info("Subscription %s completed — %s.", subscription.pk, reason)


def _terminal_reason(subscription: TrackingSubscription) -> str:
    """Return why this watch is finished, or "" while it still has work to do."""
    from apps.scm.shipments.models import Shipment
    from apps.scm.tracking.models import TrackingEvent

    shipment = subscription.shipment
    if shipment is not None:
        if shipment.status in (Shipment.Status.DELIVERED, Shipment.Status.CANCELLED):
            return f"shipment {shipment.pk} is {shipment.status}"
        return ""

    if subscription.container_id is None:
        return ""
    delivered = TrackingEvent.objects.filter(
        team_id=subscription.team_id,
        container_id=subscription.container_id,
        event_time_type=TrackingEvent.EventTimeType.ACTUAL,
        event_type=TrackingEvent.EventType.DELIVERED,
    ).exists()
    return "the carrier reported the container delivered" if delivered else ""


def store_error_payload(
    subscription: TrackingSubscription,
    *,
    error_message: str,
    payload: dict | None = None,
) -> TrackingRawPayload:
    """Record an error response for later inspection.

    Used by carrier clients that receive a body alongside an error status and want
    it preserved; the payload is stored as ERROR_RESPONSE and never marked parsed.
    """
    return store_raw_payload(
        team=subscription.team,
        provider=subscription.provider,
        payload=payload or {},
        payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
        subscription=subscription,
        parsed_successfully=False,
        error_message=error_message,
    )
