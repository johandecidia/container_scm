"""Polling policy — decides when a tracking subscription should next be synced.

The interval depends on what state the shipment is actually in, because polling a
container that has not been handed to the carrier yet at the same rate as one
mid-ocean wastes the carrier's rate limit for no information:

    before the first event      slow    — nothing to see yet
    in transit                  normal  — this is where ETAs move
    after arrival               slower  — only the last mile remains
    delivered / cancelled       stop    — the subscription is completed
    carrier not configured      rare    — retrying cannot help until configured

Failures back off exponentially with jitter so a carrier outage does not turn a
fleet of subscriptions into a synchronised retry storm. A carrier's own minimum
interval (``min_poll_interval_minutes`` in the integration config) always wins.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.utils import timezone

from .models import TrackingSubscription

# Default intervals in minutes, by state.
INTERVAL_BEFORE_FIRST_EVENT = 360  # 6h — the reference is not live at the carrier yet
INTERVAL_IN_TRANSIT = 60  # 1h — ETA and position change here
INTERVAL_AFTER_ARRIVAL = 240  # 4h — discharge and gate-out remain
INTERVAL_NOT_CONFIGURED = 1440  # 24h — needs a human, not a retry

# Failure backoff: interval * 2 ** (failures - 1), capped.
BACKOFF_BASE_MINUTES = 15
MAX_BACKOFF_MINUTES = 720  # 12h
# Jitter as a fraction of the computed delay, to desynchronise retries.
JITTER_FRACTION = 0.2

# Floor applied to every interval unless the carrier config asks for more.
DEFAULT_MIN_INTERVAL_MINUTES = 15


def _jittered(minutes: float) -> float:
    """Spread a delay by up to ±JITTER_FRACTION so retries do not align."""
    span = minutes * JITTER_FRACTION
    # secrets.randbelow gives a deterministic-free integer without seeding concerns.
    offset = (secrets.randbelow(2001) / 1000.0 - 1.0) * span
    return max(1.0, minutes + offset)


def _min_interval(integration_config: dict | None) -> int:
    config = integration_config or {}
    try:
        configured = int(config.get("min_poll_interval_minutes") or 0)
    except TypeError, ValueError:
        configured = 0
    return max(configured, DEFAULT_MIN_INTERVAL_MINUTES)


def _has_arrived(subscription: TrackingSubscription) -> bool:
    """True when the carrier has reported this reference as arrived.

    Deferred to the selector so the polling cadence and the ETA intake cannot disagree
    about whether a journey is over — otherwise a standalone container could keep
    polling at the in-transit rate for the rest of its life while its ETA was
    considered answered, or the reverse.
    """
    from apps.scm.tracking.selectors import has_journey_arrived

    return has_journey_arrived(
        subscription.team,
        shipment=subscription.shipment,
        container=subscription.container,
    )


def base_interval_minutes(subscription: TrackingSubscription) -> int:
    """Return the state-based polling interval, before failure backoff."""
    if subscription.sync_interval_minutes:
        return subscription.sync_interval_minutes

    if subscription.tracking_status == TrackingSubscription.TrackingStatus.NOT_CONFIGURED:
        return INTERVAL_NOT_CONFIGURED
    if subscription.last_event_at is None:
        return INTERVAL_BEFORE_FIRST_EVENT

    return INTERVAL_AFTER_ARRIVAL if _has_arrived(subscription) else INTERVAL_IN_TRANSIT


def next_sync_at(
    subscription: TrackingSubscription,
    *,
    integration_config: dict | None = None,
    retry_after_seconds: int | None = None,
):
    """Return the datetime of the next sync for this subscription.

    ``retry_after_seconds`` (from a carrier's Retry-After header) is honoured as a
    lower bound: we never come back sooner than the carrier asked, but we also do
    not come back later than our own schedule would.
    """
    minutes = float(base_interval_minutes(subscription))

    failures = subscription.consecutive_failures
    if failures:
        backoff = BACKOFF_BASE_MINUTES * (2 ** min(failures - 1, 10))
        minutes = min(max(minutes, float(backoff)), float(MAX_BACKOFF_MINUTES))

    minutes = max(_jittered(minutes), float(_min_interval(integration_config)))

    delay = timedelta(minutes=minutes)
    if retry_after_seconds:
        delay = max(delay, timedelta(seconds=retry_after_seconds))
    return timezone.now() + delay
