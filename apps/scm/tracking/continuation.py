"""Looking for the carrier that covers the part of a journey nobody explained.

Discovery answers "who can tell us about this box" for a container nothing tracks
yet. This module answers the later, narrower question: a container *is* tracked, its
sources have been refreshed, and they still do not account for where the box has
actually been seen.

    CMA CGM subscription exists
    latest CMA observation      Born
    physical observation        Gothenburg, later
    → a gap
    → ask the team's other carriers whether one of them moved it

What comes back is added, never substituted:

    CMA CGM      → kept, with all of its events
    Maersk       → NOT_FOUND, sweep continues
    COSCO        → FOUND → a second subscription, its own events, one journey

Four rules the sweep is built around.

**A gap is the trigger, not silence.** :func:`~apps.scm.tracking.gaps.detect_tracking_gap`
only reports a contradiction between two sources, so there is always a real,
unexplained movement behind a sweep. Nothing here re-derives that judgement.

**Nothing existing is given up.** No subscription is cancelled, no event is deleted
or rewritten, and ``Shipment.carrier`` is not touched. A container ends up with as
many verified sources as have proved themselves.

**Found once is not found forever.** The stop condition is the journey being
explained, not a carrier having once been found. When a new gap opens later, a new
sweep may run — which is what makes a three-leg journey possible.

**Carrier calls are rationed.** A sweep asks each candidate at most once, skips the
sources that were just polled, holds the same per-container lock discovery holds,
and will not start again for the same container inside a cooldown. A gap persists
until something explains it, so without that last rule every refresh of a container
sitting in a depot would sweep every carrier the team has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.utils import timezone

from apps.scm.integrations.locks import LockNotAcquiredError, resource_lock

from .gaps import detect_tracking_gap
from .journey import get_container_journey
from .manual_refresh import (
    CONTAINER_DISCOVERY_LOCK_PREFIX,
    CONTAINER_DISCOVERY_LOCK_TTL_SECONDS,
    store_discovered_carrier_source,
)
from .models import TrackingSubscription, TrackingSyncRun
from .selectors import get_verified_container_subscriptions

if TYPE_CHECKING:
    from apps.scm.containers.models import Container
    from apps.teams.models import Team

    from .gaps import TrackingGap
    from .journey import ContainerJourney

logger = logging.getLogger(__name__)

# How long a container waits between continuation sweeps. A gap stays open until
# something explains it, so this — not the gap — is what keeps a refresh from
# sweeping every carrier every time somebody opens the page.
CONTINUATION_COOLDOWN_SECONDS = 3600

# A source polled this recently has just been asked and has not explained the gap.
# Asking it again in the same breath spends a call on an answer we already have.
RECENTLY_CHECKED_MINUTES = 30

_COOLDOWN_PREFIX = "tracking_continuation_cooldown"

# Why a sweep did not happen, or what it found. Values, not messages: the caller
# decides what to tell anybody.
NO_GAP = "no_gap"
COOLDOWN = "cooldown"
IN_PROGRESS = "in_progress"
NOTHING_TO_ASK = "nothing_to_ask"
NOT_FOUND = "not_found"
FOUND = "found"
NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class ContinuationOutcome:
    """What one continuation sweep did, and why."""

    reason: str
    gap: TrackingGap | None = None
    carrier_code: str = ""
    carrier_name: str = ""
    subscription: TrackingSubscription | None = None
    sync_run: TrackingSyncRun | None = None
    events_created: int = 0
    events_updated: int = 0
    # The carriers actually reached by this sweep, for a "we checked these" line.
    carriers_checked: tuple[str, ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        """True when a further tracking source was added for the container."""
        return self.reason == FOUND

    @property
    def swept(self) -> bool:
        """True when carriers were actually asked."""
        return self.reason in (FOUND, NOT_FOUND, NOT_CONFIGURED)

    @property
    def events_seen(self) -> int:
        return self.events_created + self.events_updated


def discover_journey_continuation(
    *,
    team: Team,
    container: Container,
    journey: ContainerJourney | None = None,
    clients: dict | None = None,
    ignore_cooldown: bool = False,
) -> ContinuationOutcome:
    """Look for a carrier that explains this container's unexplained segment.

    Runs in the caller's thread and never raises: every carrier failure is classified
    by the probe, and the outcome always describes what happened. The caller decides
    the HTTP budget — wrap this in
    :func:`~apps.scm.integrations.carriers.http.interactive_carrier_requests` when
    somebody is waiting for it.

    ``journey`` may be passed by a caller that has just built one; it is only used to
    find the gap, and the sweep re-reads nothing from it. ``clients`` injects carrier
    adapters by provider code for testing.
    """
    journey = journey if journey is not None else get_container_journey(team, container)
    gap = detect_tracking_gap(journey)
    if gap is None:
        return ContinuationOutcome(reason=NO_GAP)

    if not (ignore_cooldown or _claim_cooldown(container)):
        logger.info(
            "Continuation discovery for container %s skipped — swept within the last %d seconds.",
            container.pk,
            CONTINUATION_COOLDOWN_SECONDS,
        )
        return ContinuationOutcome(reason=COOLDOWN, gap=gap)

    # The same lock discovery takes, for the same reason: two sweeps for one
    # container would ask every carrier twice and both believe they are the first to
    # create a subscription.
    try:
        with resource_lock(
            f"container:{container.pk}",
            ttl=CONTAINER_DISCOVERY_LOCK_TTL_SECONDS,
            prefix=CONTAINER_DISCOVERY_LOCK_PREFIX,
        ):
            return _sweep_for_continuation(team=team, container=container, gap=gap, clients=clients)
    except LockNotAcquiredError:
        logger.info("Continuation discovery for container %s skipped — a sweep is already running.", container.pk)
        return ContinuationOutcome(reason=IN_PROGRESS, gap=gap)


def get_recently_checked_carrier_codes(team: Team, container: Container, *, now=None) -> frozenset[str]:
    """Return the provider codes just polled for this container and found wanting.

    These are left out of the sweep. They are this container's own sources, asked
    moments ago by the refresh that led here: they had their chance to explain the
    gap and did not, so asking them again inside the same minute buys nothing and
    costs a call against the team's rate limit.

    A source that has *not* been polled recently stays in the sweep. It may know
    something now that it did not last week, and excluding it permanently would be
    the "found once, never look again" rule this feature exists to avoid.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=RECENTLY_CHECKED_MINUTES)
    return frozenset(
        subscription.provider.code
        for subscription in get_verified_container_subscriptions(team, container)
        if subscription.provider_id and subscription.last_synced_at and subscription.last_synced_at >= cutoff
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _sweep_for_continuation(*, team, container, gap: TrackingGap, clients: dict | None) -> ContinuationOutcome:
    """Ask the remaining carriers, and record the first that answers with data."""
    from apps.scm.integrations.carriers.carrier_discovery import discover_carrier_for_container

    excluded = get_recently_checked_carrier_codes(team, container)
    outcome = discover_carrier_for_container(
        team=team,
        container_number=container.container_id,
        # No preferred carrier here. The signals that order a first sweep — the
        # shipment's carrier, the carrier a planned container named — point at the
        # source that already failed to explain this segment.
        clients=clients,
        exclude_carrier_codes=excluded,
    )
    checked = tuple(outcome.carrier_names(outcome.answered))

    if not outcome.attempts:
        logger.info(
            "Continuation discovery for container %s asked nobody: %d source(s) excluded as just checked.",
            container.pk,
            len(excluded),
        )
        return ContinuationOutcome(reason=NOTHING_TO_ASK, gap=gap)

    if not outcome.found:
        # The gap stays open, and stays visible. Nothing about the container changes:
        # no source is withdrawn because another carrier had nothing to say about it.
        logger.info(
            "Continuation discovery for container %s found nothing. %d asked, %d without data, %d failed.",
            container.pk,
            len(outcome.answered),
            len(outcome.not_found),
            len(outcome.errored),
        )
        return ContinuationOutcome(reason=NOT_FOUND, gap=gap, carriers_checked=checked)

    subscription, sync_run = store_discovered_carrier_source(team=team, container=container, outcome=outcome)
    if subscription is None or sync_run is None:
        return ContinuationOutcome(
            reason=NOT_CONFIGURED,
            gap=gap,
            carrier_code=outcome.carrier_code,
            carrier_name=outcome.carrier_name,
            carriers_checked=checked,
        )

    logger.info(
        "Continuation discovery for container %s: %s explains %s → %s (%d events). Existing sources kept: %s.",
        container.pk,
        outcome.carrier_code,
        gap.from_location or "?",
        gap.to_location or "?",
        sync_run.events_created + sync_run.events_updated,
        ", ".join(
            subscription.provider.code
            for subscription in get_verified_container_subscriptions(team, container)
            if subscription.provider_id
        ),
    )
    return ContinuationOutcome(
        reason=FOUND,
        gap=gap,
        carrier_code=outcome.carrier_code,
        carrier_name=outcome.carrier_name,
        subscription=subscription,
        sync_run=sync_run,
        events_created=sync_run.events_created,
        events_updated=sync_run.events_updated,
        carriers_checked=checked,
    )


def _claim_cooldown(container) -> bool:
    """Take this container's sweep slot for the cooldown window, or report it taken.

    One ``cache.add`` does both, so two workers arriving together cannot both decide
    they are within their rights to sweep. When the cache is unavailable the sweep is
    allowed: the lock still prevents two at once, and a rate limit is a better place
    to lose a call than a feature is to lose its trigger.
    """
    key = f"{_COOLDOWN_PREFIX}:container:{container.pk}"
    try:
        return bool(cache.add(key, "1", CONTINUATION_COOLDOWN_SECONDS))
    except Exception as exc:  # noqa: BLE001 — a cache outage must not disable discovery
        logger.warning("Continuation cooldown unavailable (%s); sweeping anyway.", type(exc).__name__)
        return True
